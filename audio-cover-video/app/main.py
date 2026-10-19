"""이미지 + 오디오 → 동영상 변환 웹서비스 (FastAPI)."""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .converter import AUDIO_EXTS, IMAGE_EXTS
from .jobs import JobManager

STATIC_DIR = Path(__file__).parent / "static"
CHUNK = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.max_upload = _env_int("MAX_UPLOAD_MB", 1024) * 1024 * 1024
    app.state.jobs = JobManager(
        data_dir=Path(os.environ.get("DATA_DIR", "/data")) / "jobs",
        workers=_env_int("MAX_CONCURRENT_JOBS", 1),
        ttl_hours=_env_int("JOB_TTL_HOURS", 6),
    )
    app.state.jobs.cleanup()
    yield


app = FastAPI(title="Audio Cover Video", lifespan=lifespan)


def _safe_name(name: str, fallback: str) -> str:
    stem = Path(name or "").stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .")
    return (stem or fallback)[:80]


async def _save_upload(up: UploadFile, dst: Path, limit: int) -> int:
    size = 0
    with dst.open("wb") as f:
        while chunk := await up.read(CHUNK):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"파일이 너무 큽니다. (최대 {limit // (1024 * 1024)} MB)")
            f.write(chunk)
    if size == 0:
        raise HTTPException(400, "빈 파일입니다.")
    return size


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request, image: UploadFile = File(...), audio: UploadFile = File(...)):
    jobs: JobManager = request.app.state.jobs
    img_ext = Path(image.filename or "").suffix.lower()
    aud_ext = Path(audio.filename or "").suffix.lower()
    if img_ext not in IMAGE_EXTS and not (image.content_type or "").startswith("image/"):
        raise HTTPException(400, f"지원하지 않는 이미지 형식입니다: {img_ext or image.content_type}")
    if aud_ext not in AUDIO_EXTS and not (audio.content_type or "").startswith(("audio/", "video/")):
        raise HTTPException(400, f"지원하지 않는 오디오 형식입니다: {aud_ext or audio.content_type}")

    jobs.cleanup()
    job_id, d = jobs.new_job_dir()
    img_path = d / f"image{img_ext or '.bin'}"
    aud_path = d / f"audio{aud_ext or '.bin'}"
    try:
        await _save_upload(image, img_path, request.app.state.max_upload)
        await _save_upload(audio, aud_path, request.app.state.max_upload)
    except HTTPException:
        for p in (img_path, aud_path):
            p.unlink(missing_ok=True)
        d.rmdir()
        raise

    download_name = _safe_name(audio.filename or "", "video") + ".mp4"
    job = jobs.submit(job_id, img_path, aud_path, download_name)
    return job.public()


@app.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다. (만료되었을 수 있습니다)")
    return job.public()


@app.get("/api/jobs/{job_id}/download")
async def download(request: Request, job_id: str, inline: int = 0):
    jobs: JobManager = request.app.state.jobs
    job = jobs.get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(404, "완료된 작업이 아닙니다.")
    path = jobs.output_path(job_id)
    if not path.exists():
        raise HTTPException(404, "결과 파일이 없습니다. (만료되었을 수 있습니다)")
    disposition = "inline" if inline else "attachment"
    return FileResponse(path, media_type="video/mp4", filename=job.download_name,
                        content_disposition_type=disposition)


@app.exception_handler(HTTPException)
async def http_exc(_: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

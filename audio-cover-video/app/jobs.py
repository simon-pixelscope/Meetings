"""변환 작업 큐. NAS CPU 를 고려해 기본적으로 한 번에 하나씩 처리한다."""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .converter import ConvertError, convert


@dataclass
class Job:
    id: str
    download_name: str
    status: str = "queued"      # queued | processing | done | error
    progress: float = 0.0
    message: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        d = asdict(self)
        d["progress"] = round(self.progress, 3)
        return d


class JobManager:
    def __init__(self, data_dir: Path, workers: int = 1, ttl_hours: float = 6):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_hours * 3600
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="convert")

    # -- 경로 -------------------------------------------------------------
    def new_job_dir(self) -> tuple[str, Path]:
        job_id = uuid.uuid4().hex[:12]
        d = self.data_dir / job_id
        d.mkdir(parents=True, exist_ok=False)
        return job_id, d

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / job_id

    def output_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output.mp4"

    # -- 조회/등록 ----------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def submit(self, job_id: str, image: Path, audio: Path, download_name: str) -> Job:
        job = Job(id=job_id, download_name=download_name)
        with self._lock:
            self._jobs[job_id] = job
        self._pool.submit(self._run, job, image, audio)
        return job

    # -- 실행 ---------------------------------------------------------------
    def _run(self, job: Job, image: Path, audio: Path) -> None:
        job.status = "processing"

        def on_progress(p: float) -> None:
            job.progress = max(job.progress, p)  # ffmpeg 보고 순서가 흔들려도 뒤로 가지 않게

        try:
            meta = convert(image, audio, self.output_path(job.id), on_progress=on_progress)
            job.duration = meta["duration"]
            job.width = meta["width"]
            job.height = meta["height"]
            job.status = "done"
            job.progress = 1.0
        except ConvertError as e:
            job.status = "error"
            job.message = str(e)
        except Exception as e:  # 예기치 못한 오류도 사용자에게 상태로 전달
            job.status = "error"
            job.message = f"내부 오류: {type(e).__name__}: {e}"
        finally:
            # 입력 파일은 결과만 남기고 정리
            for p in (image, audio, self.job_dir(job.id) / "cover.png"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- 정리 ---------------------------------------------------------------
    def cleanup(self) -> int:
        """TTL 이 지난 작업 디렉터리를 삭제하고 삭제 개수를 돌려준다."""
        now = time.time()
        removed = 0
        with self._lock:
            expired = [j.id for j in self._jobs.values()
                       if now - j.created_at > self.ttl and j.status in ("done", "error")]
            for jid in expired:
                self._jobs.pop(jid, None)
        for jid in expired:
            shutil.rmtree(self.job_dir(jid), ignore_errors=True)
            removed += 1
        # 재시작 등으로 메모리에 없는 디렉터리도 mtime 기준으로 정리
        for d in self.data_dir.iterdir():
            if d.is_dir() and d.name not in self._jobs and now - d.stat().st_mtime > self.ttl:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed

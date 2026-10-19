import io
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.converter import compose_frame


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_image(w=1203, h=801, exif_rotate=False) -> bytes:
    im = Image.new("RGB", (w, h), (30, 120, 200))
    buf = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: 90° CW → 결과는 세로
        im.save(buf, "JPEG", exif=exif.tobytes())
    else:
        im.save(buf, "PNG")
    return buf.getvalue()


def make_audio(tmp_path, ext: str, seconds: float = 2.5) -> bytes:
    p = tmp_path / f"a{ext}"
    codec = ["-c:a", "aac"] if ext in (".m4a", ".mp4") else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    *codec, str(p)], check=True)
    return p.read_bytes()


def probe(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def run_job(client, image: bytes, image_name: str, audio: bytes, audio_name: str) -> tuple[dict, bytes]:
    r = client.post("/api/jobs", files={"image": (image_name, image), "audio": (audio_name, audio)})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    for _ in range(300):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    d = client.get(f"/api/jobs/{job_id}/download")
    assert d.status_code == 200
    assert d.headers["content-type"] == "video/mp4"
    return j, d.content


def assert_iphone_compatible(tmp_path, data: bytes, expect_duration: float):
    out = tmp_path / "out.mp4"
    out.write_bytes(data)
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p" and v["profile"] == "High"
    assert v["level"] == 42
    assert (v["width"], v["height"]) == (1920, 1080)
    assert v["r_frame_rate"] == "60/1" and v["avg_frame_rate"] == "60/1"
    assert a["codec_name"] == "aac"
    assert abs(float(info["format"]["duration"]) - expect_duration) < 0.3
    # faststart: moov 가 mdat 보다 앞에 있어야 스트리밍/사진앱 저장이 매끄럽다
    assert data.find(b"moov") < data.find(b"mdat")
    return v


def test_png_plus_wav(client, tmp_path):
    job, data = run_job(client, make_image(), "cover.png", make_audio(tmp_path, ".wav"), "track.wav")
    assert_iphone_compatible(tmp_path, data, 2.5)
    assert job["width"] == 1920 and job["height"] == 1080
    assert job["download_name"] == "track.mp4"


def test_exif_rotated_jpeg_plus_m4a(client, tmp_path):
    job, data = run_job(client, make_image(exif_rotate=True), "IMG_0001.jpg", make_audio(tmp_path, ".m4a"), "memo.m4a")
    assert_iphone_compatible(tmp_path, data, 2.5)  # 세로 사진도 1920×1080 안에 들어감


def test_large_image_downscaled_and_mp4_audio_source(client, tmp_path):
    job, data = run_job(client, make_image(4000, 3000), "big.png", make_audio(tmp_path, ".mp4"), "clip.mp4")
    assert_iphone_compatible(tmp_path, data, 2.5)


def test_rejects_bad_inputs(client, tmp_path):
    r = client.post("/api/jobs", files={"image": ("x.txt", b"hi"), "audio": ("a.wav", make_audio(tmp_path, ".wav"))})
    assert r.status_code == 400
    r = client.post("/api/jobs", files={"image": ("c.png", make_image()), "audio": ("a.wav", b"not audio at all")})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert j["status"] == "error" and j["message"]
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_compose_frame_fits_portrait_with_blurred_background(tmp_path):
    src = tmp_path / "portrait.png"
    Image.new("RGB", (600, 800), (0, 255, 0)).save(src)
    dst = tmp_path / "frame.png"
    assert compose_frame(src, dst) == (1920, 1080)
    with Image.open(dst) as fr:
        assert fr.size == (1920, 1080)
        assert fr.getpixel((960, 540)) == (0, 255, 0)          # 전경: 원본 그대로
        edge = fr.getpixel((20, 540))                           # 좌우 빈 영역: 어둡게 처리된 흐림 배경
        assert edge[1] < 200 and edge[0] == 0 and edge[2] == 0
        assert fr.getpixel((960 - 405 + 2, 540)) == (0, 255, 0)  # 전경 폭 810 = 1080*600/800
        assert fr.getpixel((960 - 405 - 3, 540))[1] < 200


def test_compose_frame_exif_rotation(tmp_path):
    src = tmp_path / "rot.jpg"
    src.write_bytes(make_image(800, 600, exif_rotate=True))  # 회전 후 600×800 세로
    dst = tmp_path / "frame.png"
    compose_frame(src, dst)
    with Image.open(dst) as fr:
        c = fr.getpixel((960, 540)); e = fr.getpixel((20, 540))
        assert abs(c[0] - 30) < 6 and abs(c[2] - 200) < 6     # 가운데는 원본색 (JPEG 오차 허용)
        assert e[2] < c[2] - 40                                # 좌우는 어두운 배경 → 세로로 회전됨

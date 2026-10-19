"""이미지 + 오디오 → mp4 변환 로직 (ffmpeg / ffprobe 래퍼)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

MAX_EDGE = 1920          # 긴 변 기준 최대 해상도
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUDIO_EXTS = {".mp4", ".m4a", ".wav", ".mp3", ".aac", ".flac", ".ogg", ".oga", ".opus", ".aif", ".aiff", ".caf", ".mov", ".m4v"}


class ConvertError(Exception):
    """사용자에게 그대로 보여줄 수 있는 변환 오류."""


def probe_audio(path: Path) -> dict:
    """첫 번째 오디오 스트림의 코덱과 길이(초)를 돌려준다."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise ConvertError("오디오 파일을 읽을 수 없습니다: " + (e.stderr or "").strip()[:200])
    data = json.loads(out or "{}")
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise ConvertError("오디오 트랙이 없는 파일입니다.")
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ConvertError("오디오 길이를 확인할 수 없습니다.")
    return {
        "codec": stream.get("codec_name"),
        "duration": duration,
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def normalize_image(src: Path, dst: Path, max_edge: int = MAX_EDGE) -> tuple[int, int]:
    """EXIF 회전 적용, RGB 변환, 긴 변 제한, 짝수 크기 보정 후 PNG 로 저장."""
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, max_edge / max(w, h))
            if scale < 1.0:
                im = im.resize((max(2, round(w * scale)), max(2, round(h * scale))), Image.LANCZOS)
            w, h = im.size
            w -= w % 2
            h -= h % 2
            if w < 2 or h < 2:
                raise ConvertError("이미지가 너무 작습니다.")
            if (w, h) != im.size:
                im = im.crop((0, 0, w, h))
            im.save(dst, "PNG")
            return im.size
    except ConvertError:
        raise
    except Exception as e:  # Pillow 가 못 여는 파일 등
        raise ConvertError(f"이미지 파일을 열 수 없습니다: {type(e).__name__}")


def build_ffmpeg_cmd(image: Path, audio: Path, out: Path, duration: float, audio_codec: str | None, fps: int) -> list[str]:
    """아이폰 사진앱 호환(H.264 High + AAC, yuv420p, faststart) mp4 생성 명령."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", str(image),
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
    ]
    if audio_codec == "aac":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd.append(str(out))
    return cmd


def convert(image: Path, audio: Path, out: Path, fps: int = 24,
            on_progress: Callable[[float], None] | None = None) -> dict:
    """변환을 수행하고 결과 메타데이터(길이, 해상도)를 돌려준다."""
    info = probe_audio(audio)
    norm = out.parent / "cover.png"
    width, height = normalize_image(image, norm)
    cmd = build_ffmpeg_cmd(norm, audio, out, info["duration"], info["codec"], fps)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us=") and on_progress:
            try:
                sec = int(line.split("=", 1)[1]) / 1_000_000
                on_progress(min(1.0, sec / info["duration"]))
            except ValueError:
                pass
    stderr = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise ConvertError("ffmpeg 변환 실패: " + stderr.strip()[-300:])
    if on_progress:
        on_progress(1.0)
    return {"duration": info["duration"], "width": width, "height": height}

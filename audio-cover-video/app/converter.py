"""이미지 + 오디오 → mp4 변환 로직 (ffmpeg / ffprobe 래퍼)."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Callable

import pillow_heif
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

pillow_heif.register_heif_opener()

OUT_W, OUT_H = 1920, 1080   # 출력 해상도 고정 (1080p)
FPS = 60                    # 출력 프레임레이트 고정 (p60)
BG_BLUR_RADIUS = 40
BG_BRIGHTNESS = 0.6
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


def compose_frame(src: Path, dst: Path) -> tuple[int, int]:
    """1920×1080 프레임 합성: 비율 유지로 가운데 배치, 빈 영역은 같은 이미지의 흐림 배경.

    EXIF 회전을 반영하고 RGB 로 변환한 뒤 PNG 로 저장한다.
    """
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            if w < 2 or h < 2:
                raise ConvertError("이미지가 너무 작습니다.")

            # 배경: 화면을 꽉 채우도록 확대 후 가운데 크롭, 흐림 + 어둡게
            bg_scale = max(OUT_W / w, OUT_H / h)
            bg = im.resize((max(OUT_W, round(w * bg_scale)), max(OUT_H, round(h * bg_scale))), Image.BILINEAR)
            left, top = (bg.width - OUT_W) // 2, (bg.height - OUT_H) // 2
            bg = bg.crop((left, top, left + OUT_W, top + OUT_H))
            bg = bg.filter(ImageFilter.GaussianBlur(BG_BLUR_RADIUS))
            bg = ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS)

            # 전경: 비율 유지로 화면 안에 맞춤 (작은 이미지는 확대)
            fg_scale = min(OUT_W / w, OUT_H / h)
            fw, fh = max(1, round(w * fg_scale)), max(1, round(h * fg_scale))
            fg = im.resize((fw, fh), Image.LANCZOS)
            bg.paste(fg, ((OUT_W - fw) // 2, (OUT_H - fh) // 2))

            bg.save(dst, "PNG")
            return bg.size
    except ConvertError:
        raise
    except Exception as e:  # Pillow 가 못 여는 파일 등
        raise ConvertError(f"이미지 파일을 열 수 없습니다: {type(e).__name__}")


SEGMENT_SEC = 5  # 한 번만 인코딩하는 정지 화면 세그먼트 길이 (= 키프레임 간격)


def build_segment_cmd(image: Path, seg: Path) -> list[str]:
    """정지 화면 세그먼트 1개(1080p60, H.264 High@4.2, yuv420p)를 인코딩하는 명령."""
    return [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", str(image),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-g", str(FPS * SEGMENT_SEC), "-t", str(SEGMENT_SEC), "-an",
        str(seg),
    ]


def build_concat_cmd(concat_list: Path, audio: Path, out: Path, duration: float, audio_codec: str | None) -> list[str]:
    """세그먼트를 복제(스트림 복사)해 오디오 길이만큼 이어 붙이고 오디오를 얹는 명령.

    비디오는 재인코딩하지 않으므로 오디오 길이에 거의 무관하게 빠르다.
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
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


def _run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ConvertError(f"{what} 실패: " + r.stderr.strip()[-300:])


def convert(image: Path, audio: Path, out: Path,
            on_progress: Callable[[float], None] | None = None) -> dict:
    """변환을 수행하고 결과 메타데이터(길이, 해상도)를 돌려준다."""
    report = on_progress or (lambda p: None)
    info = probe_audio(audio)
    work = out.parent
    frame = work / "cover.png"
    width, height = compose_frame(image, frame)
    report(0.05)

    # 1) 정지 화면 세그먼트 한 개만 실제 인코딩
    seg = work / "segment.mp4"
    _run(build_segment_cmd(frame, seg), "ffmpeg 인코딩")
    report(0.3)

    # 2) 세그먼트를 필요한 횟수만큼 나열해 스트림 복사로 이어 붙임
    repeats = max(1, math.ceil(info["duration"] / SEGMENT_SEC))
    concat_list = work / "concat.txt"
    concat_list.write_text(f"file '{seg.as_posix()}'\n" * repeats, encoding="utf-8")

    proc = subprocess.Popen(build_concat_cmd(concat_list, audio, out, info["duration"], info["codec"]),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us="):
            try:
                sec = int(line.split("=", 1)[1]) / 1_000_000
                report(0.3 + 0.7 * min(1.0, sec / info["duration"]))
            except ValueError:
                pass
    stderr = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise ConvertError("ffmpeg 합치기 실패: " + stderr.strip()[-300:])

    for p in (seg, concat_list):
        p.unlink(missing_ok=True)
    report(1.0)
    return {"duration": info["duration"], "width": width, "height": height}

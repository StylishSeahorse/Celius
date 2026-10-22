"""Audio and video: what's in the file (duration, codecs, size) and a still for the chat.

ffprobe and ffmpeg are native code reading untrusted files, so they run like the PDF
renderer: a separate process with memory, CPU and time limits, no environment, one thread,
and only the `file` protocol (no network, no playlists pointing elsewhere). A failure, or
ffmpeg not being installed, costs the metadata and the still, never the upload.
"""

import json
import logging
import resource
import shutil
import subprocess
from pathlib import Path

from app.services.files.previews import _webp

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
MEMORY_BYTES = 1024 * 1024 * 1024
CPU_SECONDS = 20
MAX_PROBE_OUTPUT = 256 * 1024
MAX_STILL_BYTES = 20 * 1024 * 1024
SAFE_INPUT = ["-protocol_whitelist", "file", "-threads", "1"]


def _limits() -> None:  # in the child, before exec
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _run(args: list[str]) -> bytes | None:
    # Resolved here, with the service's PATH: the child gets a minimal environment, and
    # ffmpeg may live outside /usr/bin (a static build in /usr/local/bin, Homebrew…).
    program = shutil.which(args[0])
    if program is None:
        return None
    args = [program, *args[1:]]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            preexec_fn=_limits,
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("%s timed out", Path(args[0]).name)
        return None
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def available() -> bool:
    return shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None


def probe(path: Path) -> dict:
    """Duration, container and codecs; {} when the file can't be read."""
    out = _run(
        [
            "ffprobe",
            *SAFE_INPUT,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            f"file:{path}",
        ]
    )
    if not out or len(out) > MAX_PROBE_OUTPUT:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    # A cover picture in an audio file is a "video" stream too; it isn't moving picture.
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    meta: dict = {}
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        meta["duration_seconds"] = round(duration, 2)
    if audio:
        meta["audio_codec"] = str(audio.get("codec_name") or "")[:40]
        if audio.get("channels"):
            meta["channels"] = int(audio["channels"])
    if video:
        meta["video_codec"] = str(video.get("codec_name") or "")[:40]
        if video.get("width") and video.get("height"):
            meta["width"], meta["height"] = int(video["width"]), int(video["height"])
    return meta


def still(path: Path, *, at_seconds: float | None) -> bytes | None:
    """A frame of the video at `at_seconds`, or an audio file's cover picture (None)."""
    seek = ["-ss", f"{at_seconds:.2f}"] if at_seconds else []
    out = _run(
        [
            "ffmpeg",
            *SAFE_INPUT,
            "-v",
            "error",
            *seek,
            "-i",
            f"file:{path}",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1024,iw)':-2",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "pipe:1",
        ]
    )
    if not out or len(out) > MAX_STILL_BYTES:
        return None
    try:
        return _webp(out)
    except Exception:  # noqa: BLE001 - whatever ffmpeg produced, no still
        return None


def media_info(path: Path, *, video: bool) -> tuple[dict, bytes | None]:
    if not available():
        return {}, None
    meta = probe(path)
    at = None
    if video and meta.get("video_codec"):
        # A second in (or the middle of a short clip): the first frame is often black.
        at = min(1.0, meta.get("duration_seconds", 0) / 2) or None
    return meta, still(path, at_seconds=at)


def describe(meta: dict | None) -> str:
    """'3:24, mp3' / '1:02:10, 1920×1080 h264 + aac' for the model and the UI."""
    meta = meta or {}
    parts: list[str] = []
    if seconds := meta.get("duration_seconds"):
        whole = int(round(seconds))
        hours, rest = divmod(whole, 3600)
        minutes, secs = divmod(rest, 60)
        parts.append(f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}")
    codecs = []
    if meta.get("video_codec"):
        size = f"{meta['width']}×{meta['height']} " if meta.get("width") else ""
        codecs.append(f"{size}{meta['video_codec']}")
    if meta.get("audio_codec"):
        codecs.append(meta["audio_codec"])
    if codecs:
        parts.append(" + ".join(codecs))
    return ", ".join(parts)

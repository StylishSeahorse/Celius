"""Preview images for documents (U-10): the first page of a PDF, the preview picture an
Office file carries.

PDF pages are rendered by pdfium (the engine Chrome uses for untrusted PDFs) in a separate
process with memory and CPU limits, so a hostile file can at worst fail its own preview.
Office files are not rendered (that would need LibreOffice); most saved by Word,
PowerPoint or LibreOffice include `docProps/thumbnail.*`, which is decoded and re-encoded
here, never passed through as is.
"""

import io
import logging
import resource
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

PREVIEW_SIZE = (512, 512)
RENDER_TIMEOUT_SECONDS = 30
RENDER_MEMORY_BYTES = 768 * 1024 * 1024
RENDER_CPU_SECONDS = 20
MAX_EMBEDDED_PREVIEW_BYTES = 5 * 1024 * 1024
MAX_PREVIEW_PIXELS = 40_000_000
RENDERER = Path(__file__).with_name("render_pdf.py")
OFFICE_PREVIEWS = ("docProps/thumbnail.jpeg", "docProps/thumbnail.jpg", "docProps/thumbnail.png")


def _webp(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > MAX_PREVIEW_PIXELS:
            raise ValueError("preview image too large")
        image.thumbnail(PREVIEW_SIZE)
        frame = image.convert("RGB")
    out = io.BytesIO()
    frame.save(out, "WEBP", quality=80)
    return out.getvalue()


def _limits() -> None:  # in the child, before exec
    resource.setrlimit(resource.RLIMIT_AS, (RENDER_MEMORY_BYTES, RENDER_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (RENDER_CPU_SECONDS, RENDER_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def pdf_preview(path: Path) -> bytes | None:
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(RENDERER), str(path)],
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            preexec_fn=_limits,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("PDF preview timed out")
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return _webp(result.stdout)
    except Exception:  # noqa: BLE001 - whatever the renderer produced, no preview
        return None


def office_preview(path: Path) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = {info.filename: info for info in archive.infolist()}
            for name in OFFICE_PREVIEWS:
                info = names.get(name)
                if info and info.file_size <= MAX_EMBEDDED_PREVIEW_BYTES:
                    return _webp(archive.read(info))
    except Exception:  # noqa: BLE001 - a bad preview never fails the upload
        return None
    return None

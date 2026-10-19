"""Read the content of stored files: text for the model, thumbnails and metadata for the UI.

Runs in a worker thread. Every reader is bounded (characters, pages, rows, pixels), so a
hostile or huge file costs bounded time and memory.
"""

import io
from dataclasses import dataclass, field
from pathlib import Path

from app.models import FileKind
from app.services.files.archives import ArchiveLimits, inspect_zip
from app.services.files.sniff import FileRejected

MAX_PDF_PAGES = 500
MAX_SHEET_ROWS = 2000
THUMBNAIL_SIZE = (512, 512)


@dataclass
class Extracted:
    text: str | None = None
    truncated: bool = False
    meta: dict = field(default_factory=dict)
    thumbnail: bytes | None = None


class ExtractionFailed(RuntimeError):
    pass


class _Budget:
    def __init__(self, limit: int):
        self.limit = limit
        self.parts: list[str] = []
        self.used = 0
        self.truncated = False

    def add(self, text: str) -> bool:
        """Append text; False once the budget is exhausted."""
        if self.truncated:
            return False
        room = self.limit - self.used
        if len(text) > room:
            self.parts.append(text[:room])
            self.used = self.limit
            self.truncated = True
            return False
        self.parts.append(text)
        self.used += len(text)
        return True

    def result(self) -> str:
        return "".join(self.parts)


def extract(
    path: Path, kind: FileKind, *, max_chars: int, limits: ArchiveLimits, max_pixels: int
) -> Extracted:
    try:
        if kind == FileKind.text:
            return _text(path, max_chars)
        if kind == FileKind.image:
            return _image(path, max_pixels)
        if kind == FileKind.pdf:
            return _pdf(path, max_chars)
        if kind == FileKind.archive:
            report = inspect_zip(path, limits, text_chars=max_chars)
            return Extracted(
                text=report.text or None,
                truncated=report.text_truncated,
                meta={
                    "entries": report.entries[:1000],
                    "entry_count": len(report.entries),
                    "total_bytes": report.total_bytes,
                    "skipped_encrypted": report.skipped_encrypted,
                },
            )
        # Office formats are ZIPs: validate the container before any parser opens it.
        inspect_zip(path, limits)
        if kind == FileKind.document:
            return _docx(path, max_chars)
        if kind == FileKind.spreadsheet:
            return _xlsx(path, max_chars)
        if kind == FileKind.presentation:
            return _pptx(path, max_chars)
    except (ExtractionFailed, FileRejected):
        raise
    except Exception as exc:  # noqa: BLE001 - parsers raise many types on bad input
        raise ExtractionFailed(f"Couldn't read the file: {type(exc).__name__}") from exc
    return Extracted()


def _text(path: Path, max_chars: int) -> Extracted:
    with path.open("rb") as handle:
        data = handle.read(max_chars * 4 + 4)  # UTF-8 is at most 4 bytes per character
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars or path.stat().st_size > len(data)
    return Extracted(
        text=text[:max_chars], truncated=truncated, meta={"lines": text.count("\n") + 1}
    )


def _image(path: Path, max_pixels: int) -> Extracted:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = max_pixels  # larger images raise DecompressionBombError
    with Image.open(path) as image:
        width, height = image.size
        if width * height > max_pixels:
            raise ExtractionFailed("The image is too large to process")
        fmt = image.format
        frame = ImageOps.exif_transpose(image)
        frame.thumbnail(THUMBNAIL_SIZE)
        if frame.mode not in ("RGB", "RGBA"):
            frame = frame.convert("RGBA")
        out = io.BytesIO()
        frame.save(out, "WEBP", quality=80)
    return Extracted(
        meta={"width": width, "height": height, "format": fmt}, thumbnail=out.getvalue()
    )


def _pdf(path: Path, max_chars: int) -> Extracted:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ExtractionFailed("The PDF is password-protected")
    budget = _Budget(max_chars)
    pages = len(reader.pages)
    for number, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        if not budget.add(f"--- Page {number} ---\n{page.extract_text() or ''}\n"):
            break
    if pages > MAX_PDF_PAGES:
        budget.truncated = True
    return Extracted(text=budget.result(), truncated=budget.truncated, meta={"pages": pages})


def _docx(path: Path, max_chars: int) -> Extracted:
    import docx

    document = docx.Document(str(path))
    budget = _Budget(max_chars)
    for paragraph in document.paragraphs:
        if not budget.add(paragraph.text + "\n"):
            break
    for table in document.tables:
        for row in table.rows:
            if not budget.add(" | ".join(cell.text for cell in row.cells) + "\n"):
                break
    return Extracted(
        text=budget.result(),
        truncated=budget.truncated,
        meta={"paragraphs": len(document.paragraphs), "tables": len(document.tables)},
    )


def _xlsx(path: Path, max_chars: int) -> Extracted:
    from openpyxl import load_workbook

    # A file handle, not a path: stored blobs are named by hash and openpyxl insists on
    # an .xlsx extension when given a path.
    handle = path.open("rb")
    workbook = load_workbook(handle, read_only=True, data_only=True)
    budget = _Budget(max_chars)
    sheets = []
    try:
        for sheet in workbook.worksheets:
            sheets.append(sheet.title)
            budget.add(f"--- Sheet: {sheet.title} ---\n")
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_SHEET_ROWS:
                    budget.truncated = True
                    break
                line = ",".join("" if v is None else str(v) for v in row).rstrip(",")
                if line and not budget.add(line + "\n"):
                    break
            if budget.truncated and budget.used >= budget.limit:
                break
    finally:
        workbook.close()
        handle.close()
    return Extracted(text=budget.result(), truncated=budget.truncated, meta={"sheets": sheets})


def _pptx(path: Path, max_chars: int) -> Extracted:
    from pptx import Presentation

    presentation = Presentation(str(path))
    budget = _Budget(max_chars)
    for number, slide in enumerate(presentation.slides, start=1):
        texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
        if not budget.add(f"--- Slide {number} ---\n" + "\n".join(t for t in texts if t) + "\n"):
            break
    return Extracted(
        text=budget.result(), truncated=budget.truncated, meta={"slides": len(presentation.slides)}
    )


def vision_image(path: Path, max_side: int) -> tuple[str, bytes]:
    """A downscaled copy for vision models: (media type, bytes)."""
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        frame = ImageOps.exif_transpose(image)
        frame.thumbnail((max_side, max_side))
        out = io.BytesIO()
        if frame.mode in ("RGBA", "LA", "P"):
            frame.convert("RGBA").save(out, "PNG")
            return "image/png", out.getvalue()
        frame.convert("RGB").save(out, "JPEG", quality=85)
        return "image/jpeg", out.getvalue()

"""Phase 9 (U-10): preview images for PDFs and Office files."""

import io
import zipfile

from PIL import Image

from app.services.files import previews
from tests import files_fixtures as make
from tests.test_files import uploaded


def _with_preview(office: bytes, image: bytes) -> bytes:
    """Replace the file's preview picture (or add one) with `image`."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(office)) as source, zipfile.ZipFile(out, "w") as target:
        replaced = False
        for item in source.infolist():
            if item.filename.startswith("docProps/thumbnail"):
                target.writestr(item, image)
                replaced = True
            else:
                target.writestr(item, source.read(item))
        if not replaced:
            target.writestr("docProps/thumbnail.png", image)
    return out.getvalue()


async def _thumbnail(client, headers, file_id) -> Image.Image:
    response = await client.get(f"/api/v1/files/{file_id}/thumbnail", headers=headers)
    assert response.status_code == 200, response.text
    image = Image.open(io.BytesIO(response.content))
    assert image.format == "WEBP" and max(image.size) <= 512
    return image


async def test_a_pdf_gets_a_first_page_preview(client, auth_headers, sessionmaker, settings):
    file = await uploaded(client, auth_headers, sessionmaker, settings, "a.pdf", make.pdf())
    assert file["status"] == "ready" and file["has_thumbnail"] is True
    page = await _thumbnail(client, auth_headers, file["id"])
    assert page.width == 512  # the fixture's page is wider than tall
    low, high = page.convert("L").getextrema()
    assert low < high  # something was drawn, not a blank page


async def test_office_files_use_their_embedded_preview(
    client, auth_headers, sessionmaker, settings
):
    red = make.png(300, 200, color=(220, 20, 20))
    docx = _with_preview(make.docx(), red)
    file = await uploaded(client, auth_headers, sessionmaker, settings, "letter.docx", docx)
    assert file["has_thumbnail"] is True
    image = (await _thumbnail(client, auth_headers, file["id"])).convert("RGB")
    assert image.size == (300, 200)
    r, g, b = image.getpixel((150, 100))
    assert r > 180 and g < 60 and b < 60

    plain = await uploaded(
        client, auth_headers, sessionmaker, settings, "sheet.xlsx", make.xlsx()
    )  # saved without a preview picture
    assert plain["status"] == "ready" and plain["has_thumbnail"] is False


async def test_a_broken_preview_never_fails_the_upload(
    client, auth_headers, sessionmaker, settings
):
    docx = _with_preview(make.docx(), b"not an image at all")
    file = await uploaded(client, auth_headers, sessionmaker, settings, "odd.docx", docx)
    assert file["status"] == "ready" and file["has_thumbnail"] is False
    assert (
        "Hello from Word"
        in (await client.get(f"/api/v1/files/{file['id']}/text", headers=auth_headers)).json()[
            "text"
        ]
    )


def test_the_renderer_is_limited(tmp_path, monkeypatch):
    """A PDF that makes the renderer blow its limits yields no preview, not a crash."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make.pdf())
    assert previews.pdf_preview(pdf) is not None
    monkeypatch.setattr(previews, "RENDER_MEMORY_BYTES", 32 * 1024 * 1024)  # too little
    assert previews.pdf_preview(pdf) is None
    garbage = tmp_path / "bad.pdf"
    garbage.write_bytes(b"%PDF-1.4\n" + b"\x00" * 100)
    monkeypatch.undo()
    assert previews.pdf_preview(garbage) is None


def test_previews_can_be_switched_off(tmp_path):
    from app.models import FileKind
    from app.services.files.archives import ArchiveLimits
    from app.services.files.extract import extract

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make.pdf())
    limits = ArchiveLimits(max_entries=100, max_total_bytes=10**7, max_ratio=100)
    on = extract(pdf, FileKind.pdf, max_chars=1000, limits=limits, max_pixels=10**7)
    off = extract(
        pdf, FileKind.pdf, max_chars=1000, limits=limits, max_pixels=10**7, previews=False
    )
    assert on.thumbnail and off.thumbnail is None and off.text == on.text

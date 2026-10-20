"""Render the first page of a PDF to PNG on stdout. Run as a limited subprocess by
previews.pdf_preview; never imported by the app itself."""

import sys


def main(path: str) -> None:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    try:
        page = document[0]
        width, height = page.get_size()
        # Enough pixels for a crisp 512 px preview, no more.
        scale = min(3.0, 1024 / max(width, height, 1))
        image = page.render(scale=scale).to_pil()
        image.save(sys.stdout.buffer, "PNG")
    finally:
        document.close()


if __name__ == "__main__":
    main(sys.argv[1])

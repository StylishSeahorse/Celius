# ADR 0007: File uploads, storage and processing

Status: accepted · 2026-10-19

## Decisions

### Three-step uploads with the quota reserved up front
`POST /files/uploads` declares the name and size and reserves that size against the
user's quota in one conditional `UPDATE … WHERE storage_bytes + size <= limit`. That
statement is atomic in PostgreSQL (row lock) and SQLite (write lock), so concurrent uploads
can't jointly overshoot the quota. `PUT …/content` streams the body into a quarantine
directory, hashing as it goes and refusing more bytes than were declared.
`POST …/complete` verifies the upload and stores it. Each step is safe to retry.
Abandoned sessions expire and release their reservation.

### The bytes decide the type
The stored media type is never what the client declared. It comes from the file's
signature (PNG/JPEG/GIF/WebP, `%PDF-`, ZIP), and must agree with the extension. A ZIP
named `.png` or a binary named `.txt` is rejected. Unknown binary types are refused,
not guessed. Text means valid UTF-8 without NUL bytes.

### Archives are hostile until proven otherwise
Before anything opens a ZIP, the archive is checked. This includes .docx, .xlsx and
.pptx, which are ZIPs too. The checks: entry count; unsafe names (absolute paths,
`..`, drive letters, backslashes); symbolic links; total declared size; and the
per-entry compression ratio, which catches decompression bombs. Headers can lie, so
reads are also capped by the number of bytes actually decompressed.

### Content-addressed storage
Bytes live at `blobs/<sha256[:2]>/<sha256>`, so identical content is stored once. The
blob is deleted when the last live file referencing it is deleted. Quota is charged per
file, not per blob, so deleting a file always frees its full size.

### Processing in the worker
Text extraction (text, PDF, DOCX, XLSX, PPTX, text inside ZIPs) and thumbnails run as
durable `jobs`, with the same claim and lease model as runs (ADR 0005). Every reader is
bounded by characters, pages, rows or pixels. A file that can't be read stays stored
and downloadable, with `status=failed` and a reason.

### Serving
Downloads go through an authenticated endpoint that checks ownership on every request.
There are no public URLs. Every download sends `X-Content-Type-Options: nosniff`, and
everything except inline PDFs also gets `Content-Security-Policy: sandbox`. Only
PNG/JPEG/GIF/WebP images and PDFs may be shown inline. Text, including HTML and SVG,
is always a download.

### Into the prompt
Attachment text goes before the user's words, in `<attachment>` tags, capped per file.
Project files go into the system prompt in `<project_file>` tags, within a share of the
context budget. The system prompt says that tagged content is data, not instructions.
Only the latest message's images are sent as pixels, and only to models that report
vision.

### Desktop folders
The desktop app reads only folders the user approved in the native picker, and only
files they tick. The native side resolves every path, refuses absolute paths and `..`,
and checks that the target is still inside the root after following symlinks. These
commands are granted at runtime to the connected server's origin only.

## Consequences
- A file deleted on one device stays referenced in old messages as "deleted".
- Optional ClamAV scanning (`CLAMAV_HOST`) adds latency to upload completion. If the
  scanner is down, completing an upload returns 503 instead of skipping the scan.

"""ZIP inspection that is safe against hostile archives.

Checked before anything reads an archive (including .docx/.xlsx/.pptx, which are ZIPs):
entry count, unsafe names (absolute, ``..``, drive letters, backslashes), symbolic links,
total declared size, and compression ratio. Entry headers can lie, so every read is also
bounded by the number of bytes actually decompressed.
"""

import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.services.files.paths import UnsafePath, safe_relative_path
from app.services.files.sniff import TEXT_EXTENSIONS, FileRejected, extension_of

MAX_TEXT_ENTRY_BYTES = 512 * 1024
SMALL_ENTRY = 1024 * 1024  # ratio checks ignore tiny entries (they compress well legitimately)


@dataclass
class ArchiveLimits:
    max_entries: int
    max_total_bytes: int
    max_ratio: int


@dataclass
class ArchiveReport:
    entries: list[dict] = field(default_factory=list)
    total_bytes: int = 0
    text: str = ""
    text_truncated: bool = False
    skipped_encrypted: int = 0


def inspect_zip(path: Path, limits: ArchiveLimits, *, text_chars: int = 0) -> ArchiveReport:
    """Validate an archive and, if `text_chars` > 0, collect the text of its text files."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise FileRejected("invalid_archive", "The archive is damaged or not a ZIP file") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise FileRejected(
                "archive_rejected", f"The archive has more than {limits.max_entries} entries"
            )
        report = ArchiveReport()
        for info in infos:
            name = info.filename
            if "\\" in name or "\x00" in name:
                raise FileRejected("archive_rejected", f"Unsafe path in archive: {name!r}")
            try:
                safe_relative_path(name.rstrip("/"))
            except UnsafePath as exc:
                raise FileRejected("archive_rejected", f"Unsafe path in archive: {name!r}") from exc
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise FileRejected(
                    "archive_rejected", f"The archive contains a symbolic link: {name!r}"
                )
            if info.file_size > SMALL_ENTRY and (
                info.compress_size == 0 or info.file_size / info.compress_size > limits.max_ratio
            ):
                raise FileRejected(
                    "archive_rejected", "The archive looks like a decompression bomb"
                )
            report.total_bytes += info.file_size
            if report.total_bytes > limits.max_total_bytes:
                raise FileRejected("archive_rejected", "The archive is too large when extracted")
            if not info.is_dir():
                report.entries.append({"path": name, "size": info.file_size})

        if text_chars > 0:
            _collect_text(archive, infos, report, text_chars)
        return report


def _collect_text(archive: zipfile.ZipFile, infos, report: ArchiveReport, budget: int) -> None:
    chunks: list[str] = []
    used = 0
    for info in infos:
        if info.is_dir() or extension_of(info.filename.rsplit("/", 1)[-1]) not in TEXT_EXTENSIONS:
            continue
        if info.flag_bits & 0x1:
            report.skipped_encrypted += 1
            continue
        if info.file_size > MAX_TEXT_ENTRY_BYTES:
            continue
        with archive.open(info) as member:
            data = member.read(MAX_TEXT_ENTRY_BYTES + 1)  # bounded even if the header lies
        if len(data) > MAX_TEXT_ENTRY_BYTES or b"\x00" in data:
            continue
        piece = f"=== {info.filename} ===\n{data.decode('utf-8', errors='replace')}\n"
        if used + len(piece) > budget:
            report.text_truncated = True
            break
        chunks.append(piece)
        used += len(piece)
    report.text = "\n".join(chunks)

"""Decide what a file really is from its bytes, and check that matches its name.

The client's declared media type is never trusted: the stored type comes from here.
"""

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.models import FileKind

TEXT_EXTENSIONS = {
    "txt",
    "md",
    "markdown",
    "rst",
    "tex",
    "csv",
    "tsv",
    "json",
    "jsonl",
    "ndjson",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "conf",
    "env",
    "log",
    "xml",
    "svg",
    "html",
    "htm",
    "css",
    "scss",
    "less",
    "js",
    "mjs",
    "cjs",
    "ts",
    "tsx",
    "jsx",
    "vue",
    "svelte",
    "py",
    "pyi",
    "rb",
    "go",
    "rs",
    "java",
    "kt",
    "kts",
    "scala",
    "groovy",
    "gradle",
    "c",
    "h",
    "cc",
    "cpp",
    "cxx",
    "hpp",
    "hh",
    "cs",
    "fs",
    "swift",
    "m",
    "mm",
    "php",
    "pl",
    "pm",
    "lua",
    "r",
    "jl",
    "dart",
    "ex",
    "exs",
    "erl",
    "hs",
    "ml",
    "clj",
    "sh",
    "bash",
    "zsh",
    "fish",
    "ps1",
    "psm1",
    "bat",
    "cmd",
    "sql",
    "graphql",
    "gql",
    "proto",
    "tf",
    "hcl",
    "dockerfile",
    "make",
    "mk",
    "cmake",
    "gitignore",
    "gitattributes",
    "editorconfig",
    "lock",
    "properties",
    "srt",
    "vtt",
    "ipynb",
}
IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
OFFICE = {
    "docx": (
        FileKind.document,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "word/",
    ),
    "xlsx": (
        FileKind.spreadsheet,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xl/",
    ),
    "pptx": (
        FileKind.presentation,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ppt/",
    ),
}


# Extension -> (kind, media type, container signatures it may have). The media type sent to
# browsers is chosen here, from the extension, only after the bytes matched a container.
MEDIA = {
    "mp3": (FileKind.audio, "audio/mpeg", ("mp3",)),
    "m4a": (FileKind.audio, "audio/mp4", ("iso",)),
    "aac": (FileKind.audio, "audio/aac", ("adts", "iso")),
    "wav": (FileKind.audio, "audio/wav", ("wave",)),
    "flac": (FileKind.audio, "audio/flac", ("flac",)),
    "ogg": (FileKind.audio, "audio/ogg", ("ogg",)),
    "oga": (FileKind.audio, "audio/ogg", ("ogg",)),
    "opus": (FileKind.audio, "audio/ogg", ("ogg",)),
    "weba": (FileKind.audio, "audio/webm", ("ebml",)),
    "mp4": (FileKind.video, "video/mp4", ("iso",)),
    "m4v": (FileKind.video, "video/mp4", ("iso",)),
    "mov": (FileKind.video, "video/quicktime", ("iso", "qt")),
    "webm": (FileKind.video, "video/webm", ("ebml",)),
    "mkv": (FileKind.video, "video/x-matroska", ("ebml",)),
    "ogv": (FileKind.video, "video/ogg", ("ogg",)),
    "avi": (FileKind.video, "video/x-msvideo", ("avi",)),
}


def _containers(head: bytes) -> set[str]:
    """Media containers the first bytes are consistent with."""
    found: set[str] = set()
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        found.add("wave")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        found.add("avi")
    if head[:4] == b"OggS":
        found.add("ogg")
    if head[:4] == b"fLaC":
        found.add("flac")
    if head[:4] == b"\x1a\x45\xdf\xa3":  # EBML: Matroska and WebM
        found.add("ebml")
    if head[4:8] == b"ftyp":  # ISO base media: MP4, M4A, MOV, 3GP
        found.add("iso")
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):  # older QuickTime
        found.add("qt")
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        found.add("mp3")  # an ID3 tag, or an MPEG audio frame header
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        found.add("adts")  # raw AAC
    return found


class FileRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Detected:
    kind: FileKind
    media_type: str


def _image_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head, final=False)  # tolerate a character cut at the boundary
        return True
    except UnicodeDecodeError:
        return False


def extension_of(filename: str) -> str:
    lower = filename.lower()
    if lower in ("dockerfile", "makefile", "license", "readme", "procfile", "gemfile"):
        return lower if lower in TEXT_EXTENSIONS else "txt"
    return lower.rsplit(".", 1)[-1] if "." in lower else ""


def detect(path: Path, filename: str) -> Detected:
    """Identify the file. Raises FileRejected when it's unsupported or isn't what its name says."""
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
    ext = extension_of(filename)
    image = _image_type(head)
    is_pdf = head.startswith(b"%PDF-")
    is_zip = head[:4] in (b"PK\x03\x04", b"PK\x05\x06")

    if ext in IMAGE_TYPES:
        if image is None:
            raise FileRejected("file_type_mismatch", f"{filename} is not a valid image")
        return Detected(FileKind.image, image)
    if ext == "pdf":
        if not is_pdf:
            raise FileRejected("file_type_mismatch", f"{filename} is not a valid PDF")
        return Detected(FileKind.pdf, "application/pdf")
    if ext in OFFICE:
        kind, media_type, folder = OFFICE[ext]
        if not is_zip or not _zip_has(path, "[Content_Types].xml", folder):
            raise FileRejected("file_type_mismatch", f"{filename} is not a valid .{ext} file")
        return Detected(kind, media_type)
    if ext in MEDIA:
        kind, media_type, containers = MEDIA[ext]
        if not _containers(head) & set(containers):
            noun = "audio" if kind == FileKind.audio else "video"
            raise FileRejected("file_type_mismatch", f"{filename} is not a valid {noun} file")
        return Detected(kind, media_type)
    if ext == "zip":
        if not is_zip:
            raise FileRejected("file_type_mismatch", f"{filename} is not a valid ZIP archive")
        return Detected(FileKind.archive, "application/zip")

    # Anything else must be text. Binary data with a text (or unknown) name is refused
    # rather than guessed at, so an executable can't be smuggled in as "notes.txt".
    if image or is_pdf or is_zip or not _looks_like_text(head):
        if ext in TEXT_EXTENSIONS or ext == "":
            raise FileRejected("file_type_mismatch", f"{filename} is not a text file")
        raise FileRejected("unsupported_type", f"Files of type .{ext} are not supported")
    return Detected(FileKind.text, "text/plain")


def _zip_has(path: Path, member: str, folder: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return False
    return member in names and any(n.startswith(folder) for n in names)

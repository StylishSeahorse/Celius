"""Path safety for user-supplied names. Nothing here touches the filesystem except
`resolve_within`, which is the only way server code should turn a user path into a real one."""

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

MAX_FILENAME = 255
MAX_DEPTH = 32
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class UnsafePath(ValueError):
    pass


def safe_filename(name: str) -> str:
    """A display/download filename: last path component, no control characters."""
    name = unicodedata.normalize("NFC", name)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL.sub("", name).strip().strip(".")
    if not name:
        raise UnsafePath("The file has no usable name")
    if len(name.encode()) > MAX_FILENAME:
        # Keep the extension, shorten the stem (by bytes, not cutting a character in half).
        stem, dot, ext = name.rpartition(".")
        if not dot or len(ext) > 16:
            stem, ext = name, ""
        room = MAX_FILENAME - (len(ext.encode()) + 1 if ext else 0)
        name = stem.encode()[:room].decode(errors="ignore") + (f".{ext}" if ext else "")
    return name


def safe_relative_path(path: str | None) -> str | None:
    """Normalise a folder-upload path like ``src/app/main.py``. Rejects anything that could
    point outside the folder: absolute paths, drive letters, ``..``, control characters."""
    if path is None or path == "":
        return None
    if _CONTROL.search(path):
        raise UnsafePath("The path contains control characters")
    path = unicodedata.normalize("NFC", path).replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise UnsafePath("Absolute paths are not allowed")
    parts = [p for p in PurePosixPath(path).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafePath("Paths may not contain '..'")
    if not parts or len(parts) > MAX_DEPTH:
        raise UnsafePath("The path is empty or too deep")
    if any(p.split(".")[0].lower() in _WINDOWS_RESERVED for p in parts):
        raise UnsafePath("The path uses a reserved name")
    normalised = "/".join(parts)
    if len(normalised) > 1024:
        raise UnsafePath("The path is too long")
    return normalised


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, following symlinks, and refuse to leave `root`."""
    clean = safe_relative_path(relative)
    base = root.resolve()
    target = (base / clean).resolve() if clean else base
    if target != base and base not in target.parents:
        raise UnsafePath("The path leads outside the allowed folder")
    return target


def is_within(root: Path, path: Path) -> bool:
    base = root.resolve()
    real = Path(os.path.realpath(path))
    return real == base or base in real.parents

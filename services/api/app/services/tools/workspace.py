"""Per-conversation workspaces on the volume shared with the sandbox.

The sandbox can write anything into a workspace, including symlinks to the worker's own
files (`ln -s /data/app/... x`). So the worker never follows a symlink here: every
path component is checked with lstat, and files are opened with O_NOFOLLOW.
"""

import errno
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services.files.paths import UnsafePath, safe_relative_path

ATTACHMENTS_DIR = "attachments"


class WorkspaceError(Exception):
    pass


def workspace_dir(settings: Settings, conversation_id: uuid.UUID) -> Path:
    path = Path(settings.workspace_root) / str(conversation_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(root: Path, relative: str, *, allow_root: bool = False) -> Path:
    """`relative` under `root` without following any symlink on the way."""
    relative = (relative or "").strip()
    if relative in ("", ".", "./"):
        if allow_root:
            return root
        raise WorkspaceError("A file path is required")
    try:
        clean = safe_relative_path(relative.removeprefix("./"))
    except UnsafePath as exc:
        raise WorkspaceError(str(exc)) from exc
    assert clean is not None
    current = root
    for part in clean.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue  # the rest doesn't exist yet (writing a new file)
        if stat.S_ISLNK(mode):
            raise WorkspaceError(f"{clean} goes through a symbolic link")
    return current


def _clean(relative: str) -> list[str]:
    try:
        clean = safe_relative_path((relative or "").strip().removeprefix("./"))
    except UnsafePath as exc:
        raise WorkspaceError(str(exc)) from exc
    if not clean:
        raise WorkspaceError("A file path is required")
    return clean.split("/")


def _open(root: Path, relative: str, flags: int, *, make_dirs: bool = False) -> int:
    """Open `relative` under `root` one component at a time with O_NOFOLLOW, relative to
    the directory just opened. Swapping a directory for a symlink mid-way can't redirect
    it, unlike checking a path first and opening it afterwards."""
    parts = _clean(relative)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                if not make_dirs:
                    raise WorkspaceError(f"{relative} does not exist") from None
                with _suppress_exists():
                    os.mkdir(part, 0o755, dir_fd=dir_fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                raise WorkspaceError(f"{relative} goes through a link or a non-folder") from exc
            os.close(dir_fd)
            dir_fd = next_fd
        try:
            return os.open(parts[-1], flags | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
        except FileNotFoundError:
            raise WorkspaceError(f"{relative} does not exist") from None
        except FileExistsError:
            raise WorkspaceError(f"{relative} already exists") from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise WorkspaceError(f"{relative} is a symbolic link") from exc
            raise WorkspaceError(f"{relative} can't be opened ({exc.strerror})") from exc
    finally:
        os.close(dir_fd)


class _suppress_exists:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        return kind is FileExistsError


def _open_regular(root: Path, relative: str):
    fd = _open(root, relative, os.O_RDONLY | os.O_NONBLOCK)
    handle = os.fdopen(fd, "rb")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        handle.close()
        raise WorkspaceError(f"{relative} is not a regular file")
    return handle, info.st_size


def read_bytes(root: Path, relative: str, limit: int) -> tuple[bytes, int]:
    """Up to `limit` bytes and the file's real size."""
    handle, size = _open_regular(root, relative)
    with handle:
        return handle.read(limit), size


def write_bytes(root: Path, relative: str, data: bytes, *, overwrite: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = _open(root, relative, flags, make_dirs=True)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


@dataclass
class Entry:
    path: str
    size: int
    is_dir: bool


def list_entries(root: Path, relative: str = "", *, limit: int = 500) -> tuple[list[Entry], bool]:
    """Recursive listing (without .git internals), symlinks shown but never entered."""
    base = resolve(root, relative, allow_root=True)
    if not base.exists():
        raise WorkspaceError(f"{relative or '.'} does not exist")
    entries: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in [*dirnames, *sorted(filenames)]:
            full = Path(dirpath) / name
            try:
                info = os.lstat(full)
            except OSError:
                continue
            entries.append(
                Entry(
                    path=str(full.relative_to(root)),
                    size=info.st_size if stat.S_ISREG(info.st_mode) else 0,
                    is_dir=stat.S_ISDIR(info.st_mode),
                )
            )
            if len(entries) >= limit:
                return sorted(entries, key=lambda e: e.path), True
    return sorted(entries, key=lambda e: e.path), False


def copy_in(root: Path, relative: str, source: Path) -> bool:
    """Copy a stored file into the workspace unless something is already at that path."""
    try:
        fd = _open(root, relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, make_dirs=True)
    except WorkspaceError:
        return False
    with os.fdopen(fd, "wb") as out, source.open("rb") as src:
        shutil.copyfileobj(src, out, 1024 * 1024)
    return True


def copy_out(root: Path, relative: str, destination: Path, limit: int) -> int:
    """Copy a workspace file out (no symlinks, regular files only, at most `limit` bytes)."""
    handle, size = _open_regular(root, relative)
    with handle:
        if size > limit:
            raise WorkspaceError(f"{relative} is larger than {limit} bytes")
        copied = 0
        with destination.open("wb") as out:
            while chunk := handle.read(1024 * 1024):
                copied += len(chunk)
                if copied > limit:
                    raise WorkspaceError(f"{relative} is larger than {limit} bytes")
                out.write(chunk)
    return copied

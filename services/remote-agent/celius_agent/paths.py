"""Path checks. Every path a command names is resolved (symlinks followed) and must stay
inside the session's working directory, which itself must be inside a folder this
machine's policy file allows. Comparison is case-insensitive on Windows."""

from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"


class PathRefused(ValueError):
    pass


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def is_within(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def root_for(directory: str, roots: list[dict]) -> dict | None:
    """The most specific allowed root containing `directory`, if any."""
    best = None
    for root in roots:
        path = root.get("path") if isinstance(root, dict) else root
        if not isinstance(path, str) or not path:
            continue
        if is_within(directory, path) and (
            best is None or len(_norm(path)) > len(_norm(best["path"]))
        ):
            best = root if isinstance(root, dict) else {"path": root, "write": True}
    return best


def check_working_directory(directory: str, roots: list[dict]) -> tuple[str, bool]:
    """Returns (resolved directory, writable). Raises PathRefused."""
    if not directory or "\x00" in directory or not os.path.isabs(directory):
        raise PathRefused("The working directory must be an absolute path")
    resolved = os.path.realpath(directory)
    root = root_for(resolved, roots)
    if root is None:
        raise PathRefused(
            f"{directory} is not inside a folder this machine allows (see the agent's "
            "policy file, 'roots')"
        )
    if not os.path.isdir(resolved):
        raise PathRefused(f"{directory} does not exist or is not a folder")
    return resolved, bool(root.get("write", True))


def resolve_in(workdir: str, path: str, *, allow_root: bool = False) -> str:
    """Resolve `path` (relative to `workdir`, or absolute) and require it inside `workdir`."""
    if not isinstance(path, str) or "\x00" in path or len(path) > 4096:
        raise PathRefused("Invalid path")
    if path in ("", ".") and not allow_root:
        raise PathRefused("A file path is required")
    full = os.path.realpath(os.path.join(workdir, path))
    if not is_within(full, workdir):
        raise PathRefused(f"{path} is outside the session's working directory")
    if not allow_root and _norm(full) == _norm(workdir):
        raise PathRefused("A file path is required")
    return full


def relative(workdir: str, full: str) -> str:
    rel = os.path.relpath(full, workdir)
    return rel.replace(os.sep, "/")

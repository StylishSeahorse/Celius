"""Credential storage. Windows: encrypted with DPAPI for the current user (only this
Windows account on this machine can decrypt it). Elsewhere: a file readable only by the
agent's user (0600, in a 0700 directory). The credential is never logged."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
DPAPI_PREFIX = "dpapi:"


def _dpapi(data: bytes, protect: bool) -> bytes:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    entropy_bytes = b"celius-agent-credential"
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    entropy = Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            "celius-agent",
            ctypes.byref(entropy),
            None,
            None,
            flags,
            ctypes.byref(target),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            flags,
            ctypes.byref(target),
        )
    if not ok:
        raise OSError(f"DPAPI failed (error {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def save_credential(path: Path, credential: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:  # pragma: no cover - Windows only
        payload = DPAPI_PREFIX + base64.b64encode(_dpapi(credential.encode(), True)).decode()
    else:
        payload = credential
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp, path)
    if not IS_WINDOWS:
        os.chmod(path, 0o600)


def load_credential(path: Path) -> str | None:
    env = os.environ.get("CELIUS_AGENT_CREDENTIAL")
    if env:
        return env.strip()
    if not path.exists():
        return None
    if not IS_WINDOWS and path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} is readable by other users; run: chmod 600 {path}")
    payload = path.read_text(encoding="utf-8").strip()
    if payload.startswith(DPAPI_PREFIX):  # pragma: no cover - Windows only
        return _dpapi(base64.b64decode(payload[len(DPAPI_PREFIX) :]), False).decode()
    return payload

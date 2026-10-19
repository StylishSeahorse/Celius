"""Optional malware scan through clamd's INSTREAM command."""

import asyncio
import struct
from pathlib import Path

CHUNK = 64 * 1024


class ScanUnavailable(RuntimeError):
    pass


async def clamd_scan(path: Path, host: str, port: int, reply_seconds: float = 60) -> str | None:
    """Return the signature name if the file is infected, None if clean."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    except (OSError, TimeoutError) as exc:
        raise ScanUnavailable(f"clamd at {host}:{port} is unreachable") from exc
    try:
        writer.write(b"zINSTREAM\0")
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                writer.write(struct.pack(">I", len(chunk)) + chunk)
                await writer.drain()
        writer.write(struct.pack(">I", 0))
        await writer.drain()
        reply = (
            (await asyncio.wait_for(reader.read(4096), timeout=reply_seconds))
            .rstrip(b"\0")
            .decode()
        )
    finally:
        writer.close()
    if reply.endswith("OK"):
        return None
    if reply.endswith("FOUND"):
        return reply.split(":", 1)[-1].removesuffix("FOUND").strip()
    raise ScanUnavailable(f"Unexpected clamd reply: {reply!r}")

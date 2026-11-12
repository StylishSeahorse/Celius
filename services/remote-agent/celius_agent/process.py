"""Run a command, stream its output, enforce a timeout and an output cap, and kill the
whole process tree on timeout or cancellation (POSIX: a new session/process group;
Windows: a new process group and `taskkill /T`)."""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"
KILL_GRACE_SECONDS = 3
# Output beyond the cap is dropped; this many times the cap stops the command.
RUNAWAY_FACTOR = 20

OutputCallback = Callable[[str, str], Awaitable[None]]  # (stream, text)


@dataclass
class ProcessResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    killed_reason: str | None = None  # timeout | cancelled | output_limit
    duration_ms: int = 0


@dataclass
class _Capture:
    limit: int
    parts: list[str] = field(default_factory=list)
    size: int = 0
    truncated: bool = False

    def add(self, text: str) -> None:
        if self.size >= self.limit:
            self.truncated = True
            return
        room = self.limit - self.size
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self.parts.append(text)
        self.size += len(text)

    def text(self) -> str:
        return "".join(self.parts)


def _resource_limits(memory_mb: int | None):
    if IS_WINDOWS or not memory_mb:
        return None

    def apply() -> None:  # runs in the child before exec (POSIX only)
        import resource

        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply


async def kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if IS_WINDOWS:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
    try:
        await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        process.kill()


async def run_process(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    output_limit: int,
    env: dict[str, str] | None = None,
    on_output: OutputCallback | None = None,
    cancel: asyncio.Event | None = None,
    memory_mb: int | None = None,
    stdin_data: bytes | None = None,
) -> ProcessResult:
    """Run `argv` (no shell is added here). Output is decoded as UTF-8 with replacement."""
    started = time.monotonic()
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
        limits = _resource_limits(memory_mb)
        if limits is not None:
            kwargs["preexec_fn"] = limits
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    captures = {"stdout": _Capture(output_limit), "stderr": _Capture(output_limit)}
    over_limit = asyncio.Event()
    seen = [0]

    async def pump(stream_name: str, reader: asyncio.StreamReader) -> None:
        # Incremental: a multi-byte character split across reads is decoded once whole.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await reader.read(8192)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                captures[stream_name].add(text)
                if on_output is not None:
                    await on_output(stream_name, text)
                seen[0] += len(text)
                if seen[0] > output_limit * RUNAWAY_FACTOR:
                    over_limit.set()  # runaway output: stop it rather than run to the timeout
            if not chunk:
                break

    if stdin_data is not None and process.stdin is not None:
        process.stdin.write(stdin_data)
        await process.stdin.drain()
        process.stdin.close()

    pumps = asyncio.gather(pump("stdout", process.stdout), pump("stderr", process.stderr))
    waiter = asyncio.ensure_future(process.wait())
    cancel_wait = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
    limit_wait = asyncio.ensure_future(over_limit.wait())
    watched = {waiter, limit_wait} | ({cancel_wait} if cancel_wait else set())
    killed_reason = None
    try:
        done, _ = await asyncio.wait(watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            if cancel_wait is not None and cancel_wait in done:
                killed_reason = "cancelled"
            elif limit_wait in done:
                killed_reason = "output_limit"
            else:
                killed_reason = "timeout"
            await kill_tree(process)
    except asyncio.CancelledError:
        await kill_tree(process)
        raise
    finally:
        for task in (cancel_wait, limit_wait):
            if task is not None and not task.done():
                task.cancel()
    await process.wait()
    try:
        await asyncio.wait_for(pumps, 5)
    except asyncio.TimeoutError:
        pumps.cancel()  # a grandchild kept the pipe open; its output is lost
    return ProcessResult(
        exit_code=process.returncode,
        stdout=captures["stdout"].text(),
        stderr=captures["stderr"].text(),
        stdout_truncated=captures["stdout"].truncated,
        stderr_truncated=captures["stderr"].truncated,
        killed_reason=killed_reason,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

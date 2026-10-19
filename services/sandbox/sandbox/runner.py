"""Run one piece of untrusted code.

Isolation, from the outside in:

1. **The container** (see infra/compose.yaml): no route to the internet or the app's
   network, no secrets, no Docker socket, all capabilities dropped, read-only root,
   non-root user, CPU/memory/pid limits. This is the boundary everything else relies on.
2. **bubblewrap** (when the kernel and seccomp allow unprivileged user namespaces):
   per execution, a fresh network namespace (no network at all), PID namespace, read-only
   view of the filesystem, private /tmp, and only this workspace mounted writable.
3. **Process limits** (always): CPU seconds, address space, file size, process and file
   counts, a wall-clock timeout, capped output, a fresh session that is killed as a whole,
   a scrubbed environment, and a sweep for any process that escaped its session.
"""

import asyncio
import contextlib
import ctypes
import os
import re
import resource
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.config import SandboxConfig

WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SNAPSHOT_LIMIT = 20_000
SIGNAL_REASONS = {signal.SIGXCPU: "cpu_limit", signal.SIGXFSZ: "file_size_limit"}
PR_SET_CHILD_SUBREAPER = 36

# Sessions of executions in progress; anything else we started is a stray.
_active_sessions: set[int] = set()


class InvalidRequest(ValueError):
    pass


@dataclass
class Execution:
    workspace: str
    kind: str  # "python" | "shell"
    code: str
    timeout_seconds: int = 60
    memory_mb: int = 1024
    output_limit: int = 64 * 1024


@dataclass
class Result:
    exit_code: int | None
    killed_reason: str | None  # timeout | cpu_limit | output_limit | cancelled | ...
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    mode: str
    changed_files: list[dict] = field(default_factory=list)
    workspace_bytes: int = 0
    workspace_over_limit: bool = False


def bwrap_available(scratch: Path) -> bool:
    """True if bubblewrap can create namespaces here (needs unprivileged user namespaces)."""
    if shutil.which("bwrap") is None:
        return False
    try:
        result = subprocess.run(
            ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def workspace_path(config: SandboxConfig, workspace: str) -> Path:
    if not WORKSPACE_ID.match(workspace) or ".." in workspace:
        raise InvalidRequest("Invalid workspace id")
    path = config.workspaces_root / workspace
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """path -> (size, mtime_ns) for regular files, without following symlinks."""
    files: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.islink(full):
                files[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns)
            if len(files) >= SNAPSHOT_LIMIT:
                return files
    return files


def _workspace_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _limits(config: SandboxConfig, memory_mb: int, cpu_seconds: int):
    def apply() -> None:  # runs in the child between fork and exec
        memory = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (config.max_file_bytes, config.max_file_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (config.max_processes, config.max_processes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.umask(0o022)

    return apply


def _environment(home: str, tmp: str) -> dict[str, str]:
    # Built from scratch: nothing from the service's own environment (e.g. its token) leaks.
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": tmp,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


def _command(
    mode: str, kind: str, code_file: Path, workspace: Path, scratch: Path, config: SandboxConfig
) -> tuple[list[str], dict[str, str], str]:
    interpreter = ["python3", "-I"] if kind == "python" else ["bash", "--noprofile", "--norc"]
    if mode == "bwrap":
        inner_code = f"/run/celius/{code_file.name}"
        command = [
            "bwrap",
            "--unshare-all",  # network, pid, ipc, uts, user, cgroup namespaces
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            str(config.workspaces_root),  # hide every other workspace
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/run",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(workspace),
            "/home/workspace",
            "--ro-bind",
            str(code_file),
            inner_code,
            "--chdir",
            "/home/workspace",
            *interpreter,
            inner_code,
        ]
        return command, _environment("/home/workspace", "/tmp"), str(workspace)
    tmp = scratch / "tmp"
    tmp.mkdir(exist_ok=True)
    return [*interpreter, str(code_file)], _environment(str(workspace), str(tmp)), str(workspace)


async def _read_capped(stream: asyncio.StreamReader, limit: int, on_flood) -> tuple[bytes, bool]:
    kept = bytearray()
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if len(kept) < limit:
            kept.extend(chunk[: limit - len(kept)])
        if total > limit * 8:  # far past the cap: stop wasting CPU on it
            on_flood()
            break
    return bytes(kept), total > limit


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def become_subreaper() -> bool:
    """Orphaned grandchildren (a daemonised `nohup … &`) get re-parented to us, not to init,
    so they can always be found and killed. Linux only; harmless if it fails."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def reap_strays() -> int:
    """Kill our descendants that are not part of a running execution's session
    (e.g. children that called setsid() to escape the process-group kill)."""
    me = os.getpid()
    my_sid = os.getsid(0)
    killed = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        pid = int(entry)
        try:
            sid = os.getsid(pid)
            if sid == my_sid or sid in _active_sessions or not _descends_from(pid, me):
                continue
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            continue
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, os.WNOHANG)  # re-parented to us: collect the zombie
    return killed


def _descends_from(pid: int, ancestor: int) -> bool:
    for _ in range(64):
        try:
            with open(f"/proc/{pid}/stat") as stat:
                ppid = int(stat.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if ppid == ancestor:
            return True
        if ppid <= 1:
            return False
        pid = ppid
    return False


async def run(execution: Execution, config: SandboxConfig, mode: str) -> Result:
    if execution.kind not in ("python", "shell"):
        raise InvalidRequest("kind must be python or shell")
    timeout = max(1, min(execution.timeout_seconds, config.max_timeout_seconds))
    memory_mb = max(64, min(execution.memory_mb, config.max_memory_mb))
    output_limit = max(1024, min(execution.output_limit, config.max_output_bytes))
    workspace = workspace_path(config, execution.workspace)

    scratch = config.scratch_root / f"exec-{uuid.uuid4().hex}"
    scratch.mkdir(parents=True, mode=0o700)
    code_file = scratch / ("main.py" if execution.kind == "python" else "main.sh")
    code_file.write_text(execution.code)
    before = _snapshot(workspace)
    command, env, cwd = _command(mode, execution.kind, code_file, workspace, scratch, config)

    started = time.monotonic()
    killed_reason: str | None = None
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        preexec_fn=_limits(config, memory_mb, timeout),
        start_new_session=True,  # its own session/process group, killed as a whole
    )
    _active_sessions.add(process.pid)

    def flood() -> None:
        nonlocal killed_reason
        killed_reason = killed_reason or "output_limit"
        _kill_group(process.pid)

    try:
        reader = asyncio.gather(
            _read_capped(process.stdout, output_limit, flood),
            _read_capped(process.stderr, output_limit, flood),
        )
        exited = asyncio.ensure_future(process.wait())
        done, _ = await asyncio.wait({exited}, timeout=timeout)
        if not done:
            killed_reason = killed_reason or "timeout"
            _kill_group(process.pid)
            await exited
        # The main process is gone. A background child may still hold the pipes open:
        # give it a moment to flush, then end the session and every stray.
        done, _ = await asyncio.wait({reader}, timeout=1)
        if not done:
            _kill_group(process.pid)
            reap_strays()
        (out, out_cut), (err, err_cut) = await reader
    except asyncio.CancelledError:
        _kill_group(process.pid)  # the caller went away: stop the code, now
        reader.cancel()
        await asyncio.shield(process.wait())
        raise
    finally:
        _kill_group(process.pid)  # anything still alive in its session
        _active_sessions.discard(process.pid)
        reap_strays()
        shutil.rmtree(scratch, ignore_errors=True)

    code = process.returncode
    if killed_reason is None and code is not None and code < 0:
        killed_reason = SIGNAL_REASONS.get(-code, f"signal_{-code}")

    after = _snapshot(workspace)
    changed = [
        {"path": path, "size": meta[0], "status": "created" if path not in before else "modified"}
        for path, meta in sorted(after.items())
        if before.get(path) != meta
    ]
    used = _workspace_bytes(workspace)
    return Result(
        exit_code=code,
        killed_reason=killed_reason,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        stdout_truncated=out_cut,
        stderr_truncated=err_cut,
        duration_ms=int((time.monotonic() - started) * 1000),
        mode=mode,
        changed_files=changed[:500],
        workspace_bytes=used,
        workspace_over_limit=used > config.max_workspace_bytes,
    )

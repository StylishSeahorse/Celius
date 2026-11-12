"""Native tools: what Celius's own agent can do on this machine (mode 1).

Names and arguments match Celius's built-in tools (list_files, read_file, write_file,
run_shell, run_python, git), so the model, prompts and approvals stay the same. Every path
is checked by paths.py; shell, Python and git need `allow_shell` in this machine's policy
file *and* in what Celius sent for the session; writes need `allow_write` and a writable
root. Results are {"content": what the model sees, "data": detail, "is_error": bool}.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from celius_agent.config import Policy
from celius_agent.paths import PathRefused, relative, resolve_in
from celius_agent.process import ProcessResult, run_process

IS_WINDOWS = sys.platform == "win32"
READ_LIMIT = 200_000
LIST_LIMIT = 500
DIFF_MAX_BYTES = 256_000
SNAPSHOT_LIMIT = 20_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".tox"}
OUTPUT_FLUSH_SECONDS = 0.1
OUTPUT_FLUSH_CHARS = 16_000

Emit = Callable[[str, dict], Awaitable[None]]


class ToolRefused(Exception):
    pass


@dataclass
class ToolEnv:
    workdir: str
    writable: bool
    permissions: dict  # from Celius for this session (already narrowed by Central)
    policy: Policy  # this machine's own policy file
    emit: Emit
    command_id: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    python: str | None = None
    session_id: str = ""

    @property
    def shell_allowed(self) -> bool:
        return self.policy.allow_shell and bool(self.permissions.get("allow_shell"))

    @property
    def write_allowed(self) -> bool:
        return (
            self.writable
            and self.policy.allow_write
            and bool(self.permissions.get("allow_write", True))
        )


def result(content: str, data: dict | None = None, is_error: bool = False) -> dict:
    return {"content": content, "data": data or {}, "is_error": is_error}


def error(message: str, **data) -> dict:
    return result(f"Error: {message}", {"error": message, **data}, True)


def find_python() -> str | None:
    for name in ("python3", "python", "py") if not IS_WINDOWS else ("python", "py", "python3"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:  # the Store stub only opens the Store
            return found
    return None


def shell_argv(command: str) -> list[str]:
    if IS_WINDOWS:
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    shell = shutil.which("bash") or "/bin/sh"
    return [shell, "-c", command]


def shell_name() -> str:
    if IS_WINDOWS:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash" if shutil.which("bash") else "sh"


def command_env(policy: Policy) -> dict[str, str]:
    deny = {"CELIUS_AGENT_CREDENTIAL", "CELIUS_AGENT_HOME", *policy.env_deny}
    env = {k: v for k, v in os.environ.items() if k not in deny}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------- files


def _list_files(env: ToolEnv, args: dict) -> dict:
    try:
        root = resolve_in(env.workdir, str(args.get("path") or ""), allow_root=True)
    except PathRefused as exc:
        return error(str(exc))
    if not os.path.isdir(root):
        return error(f"{args.get('path')} is not a folder")
    entries = []
    more = False
    for name in sorted(os.listdir(root)):
        if len(entries) >= LIST_LIMIT:
            more = True
            break
        full = os.path.join(root, name)
        is_dir = os.path.isdir(full)
        try:
            size = 0 if is_dir else os.path.getsize(full)
        except OSError:
            continue
        entries.append({"path": relative(env.workdir, full), "is_dir": is_dir, "size": size})
    lines = [
        f"{e['path']}/" if e["is_dir"] else f"{e['path']}  ({e['size']} bytes)" for e in entries
    ]
    if more:
        lines.append("[…more entries not shown]")
    return result("\n".join(lines) or "(empty folder)", {"entries": entries, "truncated": more})


def _read_file(env: ToolEnv, args: dict) -> dict:
    path = str(args.get("path") or "")
    offset = max(0, int(args.get("offset") or 0))
    limit = max(100, min(int(args.get("max_chars") or 30_000), READ_LIMIT))
    try:
        full = resolve_in(env.workdir, path)
    except PathRefused as exc:
        return error(str(exc))
    if not os.path.isfile(full):
        return error(f"{path} does not exist or is not a file")
    size = os.path.getsize(full)
    with open(full, "rb") as handle:
        data = handle.read((offset + limit) * 4 + 4)
    if b"\x00" in data[:8192]:
        return error(f"{path} is a binary file ({size} bytes)")
    text = data.decode("utf-8", errors="replace")
    piece = text[offset : offset + limit]
    end = offset + len(piece)
    more = end < len(text) or len(data) < size
    header = ""
    if offset or more:
        tail = f"; more follows, continue with offset={end}]" if more else "; end of file]"
        header = f"[{path}: characters {offset:,}–{end:,}{tail}\n"
    return result(
        header + piece,
        {"path": path, "size": size, "offset": offset, "end": end, "truncated": more},
    )


def _write_file(env: ToolEnv, args: dict) -> tuple[dict, dict | None]:
    if not env.write_allowed:
        return error("Writing files is not allowed in this session on this machine"), None
    path = str(args.get("path") or "")
    content = args.get("content")
    if not isinstance(content, str):
        return error("content must be text"), None
    overwrite = bool(args.get("overwrite", True))
    try:
        full = resolve_in(env.workdir, path)
    except PathRefused as exc:
        return error(str(exc)), None
    existed = os.path.exists(full)
    if existed and os.path.isdir(full):
        return error(f"{path} is a folder"), None
    if existed and not overwrite:
        return error(f"{path} already exists (overwrite is false)"), None
    old_text = None
    if existed and os.path.getsize(full) <= DIFF_MAX_BYTES:
        with open(full, "rb") as handle:
            raw = handle.read()
        if b"\x00" not in raw[:8192]:
            old_text = raw.decode("utf-8", errors="replace")
    data = content.encode("utf-8")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".celius-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if existed:
            shutil.copymode(full, tmp)
        os.replace(tmp, full)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    rel = relative(env.workdir, full)
    diff = None
    if len(data) <= DIFF_MAX_BYTES:
        diff = "".join(
            difflib.unified_diff(
                (old_text or "").splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}" if existed else "/dev/null",
                tofile=f"b/{rel}",
            )
        )[:64_000]
    event = {
        "command_id": env.command_id,
        "path": rel,
        "action": "modified" if existed else "created",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "diff": diff,
    }
    return result(
        f"Wrote {len(data)} bytes to {rel}",
        {"changed_files": [{"path": rel, "size": len(data)}]},
    ), event


def _fetch_file(env: ToolEnv, args: dict) -> dict:
    import base64

    path = str(args.get("path") or "")
    max_bytes = int(args.get("max_bytes") or 8 * 1024 * 1024)
    try:
        full = resolve_in(env.workdir, path)
    except PathRefused as exc:
        return error(str(exc))
    if not os.path.isfile(full):
        return error(f"{path} does not exist or is not a file")
    size = os.path.getsize(full)
    if size > max_bytes:
        return error(f"{path} is too large to download ({size} bytes)")
    with open(full, "rb") as handle:
        data = handle.read()
    return result(
        f"{path} ({size} bytes)",
        {
            "name": os.path.basename(full),
            "size": size,
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_b64": base64.b64encode(data).decode(),
        },
    )


async def list_files(env: ToolEnv, args: dict) -> dict:
    return await asyncio.to_thread(_list_files, env, args)


async def read_file(env: ToolEnv, args: dict) -> dict:
    return await asyncio.to_thread(_read_file, env, args)


async def write_file(env: ToolEnv, args: dict) -> dict:
    outcome, event = await asyncio.to_thread(_write_file, env, args)
    if event is not None:
        await env.emit("file.changed", event)
    return outcome


async def fetch_file(env: ToolEnv, args: dict) -> dict:
    return await asyncio.to_thread(_fetch_file, env, args)


# ---------------------------------------------------------------------- commands


def snapshot(workdir: str) -> dict[str, tuple[int, int]] | None:
    """(mtime_ns, size) per file, skipping bulky folders. None when there are too many."""
    files: dict[str, tuple[int, int]] = {}
    for current, dirs, names in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            full = os.path.join(current, name)
            try:
                stat = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            files[relative(workdir, full)] = (stat.st_mtime_ns, stat.st_size)
            if len(files) > SNAPSHOT_LIMIT:
                return None
    return files


def compare(before: dict, after: dict) -> list[dict]:
    changes = []
    for path, (mtime, size) in after.items():
        old = before.get(path)
        if old is None:
            changes.append({"path": path, "action": "created", "size": size})
        elif old != (mtime, size):
            changes.append({"path": path, "action": "modified", "size": size})
    for path in before.keys() - after.keys():
        changes.append({"path": path, "action": "deleted", "size": 0})
    return sorted(changes, key=lambda c: c["path"])


class OutputStreamer:
    """Coalesces process output into `terminal.output` events (every 100 ms or 16 kB)."""

    def __init__(self, env: ToolEnv):
        self.env = env
        self.buffer: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self.size = 0
        self.last = time.monotonic()

    async def __call__(self, stream: str, text: str) -> None:
        self.buffer[stream].append(text)
        self.size += len(text)
        if self.size >= OUTPUT_FLUSH_CHARS or time.monotonic() - self.last >= OUTPUT_FLUSH_SECONDS:
            await self.flush()

    async def flush(self) -> None:
        for stream, parts in self.buffer.items():
            if parts:
                await self.env.emit(
                    "terminal.output",
                    {"command_id": self.env.command_id, "stream": stream, "text": "".join(parts)},
                )
                parts.clear()
        self.size = 0
        self.last = time.monotonic()


async def _run(env: ToolEnv, argv: list[str], seconds: float) -> dict:
    timeout = max(1.0, min(seconds, env.policy.max_command_seconds))
    before = await asyncio.to_thread(snapshot, env.workdir)
    streamer = OutputStreamer(env)
    proc: ProcessResult = await run_process(
        argv,
        cwd=env.workdir,
        timeout=timeout,
        output_limit=env.policy.max_output_chars,
        env=command_env(env.policy),
        on_output=streamer,
        cancel=env.cancel,
        memory_mb=env.policy.memory_limit_mb,
    )
    await streamer.flush()
    changed: list[dict] = []
    if before is not None:
        after = await asyncio.to_thread(snapshot, env.workdir)
        if after is not None:
            changed = compare(before, after)
    for change in changed[:200]:
        await env.emit("file.changed", {"command_id": env.command_id, **change})
    parts = []
    if proc.stdout:
        parts.append(proc.stdout + ("\n[…output cut]" if proc.stdout_truncated else ""))
    if proc.stderr:
        parts.append("stderr:\n" + proc.stderr + ("\n[…cut]" if proc.stderr_truncated else ""))
    if proc.killed_reason:
        parts.append(f"[stopped: {proc.killed_reason.replace('_', ' ')}]")
    parts.append(f"[exit code {proc.exit_code}]")
    if changed:
        listed = ", ".join(f"{c['path']} ({c['action']})" for c in changed[:20])
        more = f" and {len(changed) - 20} more" if len(changed) > 20 else ""
        parts.append(f"[files changed: {listed}{more}]")
    failed = proc.killed_reason is not None or (proc.exit_code or 0) != 0
    return result(
        "\n".join(parts),
        {
            "stdout": proc.stdout[-20_000:],
            "stderr": proc.stderr[-20_000:],
            "stdout_truncated": proc.stdout_truncated,
            "stderr_truncated": proc.stderr_truncated,
            "exit_code": proc.exit_code,
            "killed_reason": proc.killed_reason,
            "duration_ms": proc.duration_ms,
            "changed_files": changed[:200],
        },
        failed,
    )


def _seconds(args: dict, default: float = 120) -> float:
    try:
        return float(args.get("timeout_seconds") or default)
    except (TypeError, ValueError):
        return default


async def run_shell(env: ToolEnv, args: dict) -> dict:
    if not env.shell_allowed:
        return error("Shell commands are not allowed on this machine (policy: allow_shell)")
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return error("command is required")
    return await _run(env, shell_argv(command), _seconds(args))


async def run_python(env: ToolEnv, args: dict) -> dict:
    if not env.shell_allowed:
        return error("Running code is not allowed on this machine (policy: allow_shell)")
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return error("code is required")
    python = env.python or find_python()
    if python is None:
        return error("Python is not installed on this machine")
    fd, script = tempfile.mkstemp(prefix="celius-", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code)
        return await _run(env, [python, script], _seconds(args))
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


async def git(env: ToolEnv, args: dict) -> dict:
    if not env.shell_allowed:
        return error("git is not allowed on this machine (policy: allow_shell)")
    git_args = args.get("args")
    if (
        not isinstance(git_args, list)
        or not git_args
        or not all(isinstance(a, str) for a in git_args)
    ):
        return error("args must be a list of strings")
    executable = shutil.which("git")
    if executable is None:
        return error("git is not installed on this machine")
    try:
        repo = resolve_in(env.workdir, str(args.get("repository") or ""), allow_root=True)
    except PathRefused as exc:
        return error(str(exc))
    return await _run(env, [executable, "-C", repo, *git_args], _seconds(args))


TOOLS: dict[str, Callable[[ToolEnv, dict], Awaitable[dict]]] = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_shell": run_shell,
    "run_python": run_python,
    "git": git,
}

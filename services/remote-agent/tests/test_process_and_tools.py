import asyncio
import os
import sys
import time

import pytest

from celius_agent.config import Policy
from celius_agent.process import run_process
from celius_agent.tools import TOOLS, ToolEnv, command_env, compare, snapshot

PY = sys.executable


async def test_output_is_streamed_and_exit_code_captured(tmp_path):
    seen = []

    async def on_output(stream, text):
        seen.append((stream, text))

    result = await run_process(
        [PY, "-c", "import sys; print('out é'); print('err', file=sys.stderr); sys.exit(3)"],
        cwd=str(tmp_path),
        timeout=30,
        output_limit=10_000,
        env=command_env(Policy()),  # what the agent runs commands with
        on_output=on_output,
    )
    assert result.exit_code == 3
    assert "out é" in result.stdout
    assert "err" in result.stderr
    assert {s for s, _ in seen} == {"stdout", "stderr"}


async def test_timeout_kills_the_process_tree(tmp_path):
    marker = tmp_path / "child-alive"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text("import sys, time\ntime.sleep(3)\nopen(sys.argv[1], 'w').write('x')\n")
    child = tmp_path / "child.py"
    # The child starts a grandchild that would write a file after 3 s, then waits.
    child.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = await run_process(
        [PY, str(child), str(grandchild), str(marker)],
        cwd=str(tmp_path),
        timeout=1,
        output_limit=1000,
    )
    assert result.killed_reason == "timeout", result
    assert time.monotonic() - started < 15
    await asyncio.sleep(4)
    assert not marker.exists()  # the grandchild was killed too


async def test_cancel_stops_a_command(tmp_path):
    cancel = asyncio.Event()
    task = asyncio.create_task(
        run_process(
            [PY, "-c", "import time; time.sleep(60)"],
            cwd=str(tmp_path),
            timeout=60,
            output_limit=1000,
            cancel=cancel,
        )
    )
    await asyncio.sleep(0.5)
    cancel.set()
    result = await asyncio.wait_for(task, 15)
    assert result.killed_reason == "cancelled"


async def test_output_is_capped(tmp_path):
    result = await run_process(
        [PY, "-c", "print('x' * 50000)"], cwd=str(tmp_path), timeout=30, output_limit=1000
    )
    assert len(result.stdout) == 1000
    assert result.stdout_truncated


def _env(tmp_path, *, shell=True, write=True, events=None):
    async def emit(type_, data):
        if events is not None:
            events.append((type_, data))

    return ToolEnv(
        workdir=os.path.realpath(tmp_path),
        writable=write,
        permissions={"allow_shell": shell, "allow_write": write},
        policy=Policy(roots=[{"path": str(tmp_path), "write": write}], allow_shell=shell),
        emit=emit,
        command_id="c1",
        python=PY,
    )


async def test_write_read_list_and_diff(tmp_path):
    events = []
    env = _env(tmp_path, events=events)
    out = await TOOLS["write_file"](env, {"path": "src/a.txt", "content": "one\n"})
    assert not out["is_error"]
    out = await TOOLS["write_file"](env, {"path": "src/a.txt", "content": "two\n"})
    changes = [d for t, d in events if t == "file.changed"]
    assert [c["action"] for c in changes] == ["created", "modified"]
    assert "-one" in changes[1]["diff"] and "+two" in changes[1]["diff"]
    out = await TOOLS["read_file"](env, {"path": "src/a.txt"})
    assert out["content"] == "two\n"
    out = await TOOLS["list_files"](env, {"path": "src"})
    assert "src/a.txt" in out["content"]
    out = await TOOLS["read_file"](env, {"path": "../outside.txt"})
    assert out["is_error"]


async def test_permissions_are_enforced_by_the_agent(tmp_path):
    env = _env(tmp_path, shell=False, write=False)
    assert (await TOOLS["run_shell"](env, {"command": "echo hi"}))["is_error"]
    assert (await TOOLS["run_python"](env, {"code": "print(1)"}))["is_error"]
    assert (await TOOLS["git"](env, {"args": ["status"]}))["is_error"]
    assert (await TOOLS["write_file"](env, {"path": "x", "content": "y"}))["is_error"]
    # Celius allowing it is not enough: the machine's own policy must allow it too.
    env.permissions = {"allow_shell": True, "allow_write": True}
    assert (await TOOLS["run_shell"](env, {"command": "echo hi"}))["is_error"]


async def test_shell_and_python_report_file_changes(tmp_path):
    events = []
    env = _env(tmp_path, events=events)
    out = await TOOLS["run_python"](env, {"code": "open('made.txt','w').write('x'); print('done')"})
    assert not out["is_error"], out
    assert "done" in out["content"]
    assert "made.txt (created)" in out["content"]
    assert any(t == "terminal.output" for t, _ in events)
    command = "Write-Output shell-ok" if sys.platform == "win32" else "echo shell-ok"
    out = await TOOLS["run_shell"](env, {"command": command})
    assert "shell-ok" in out["content"], out


def test_snapshot_compare(tmp_path):
    (tmp_path / "a").write_text("1")
    before = snapshot(str(tmp_path))
    (tmp_path / "a").write_text("22")
    (tmp_path / "b").write_text("new")
    after = snapshot(str(tmp_path))
    actions = {c["path"]: c["action"] for c in compare(before, after)}
    assert actions == {"a": "modified", "b": "created"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX memory limit")
async def test_memory_limit_on_posix(tmp_path):
    result = await run_process(
        [PY, "-c", "b = bytearray(600 * 1024 * 1024); print('allocated')"],
        cwd=str(tmp_path),
        timeout=30,
        output_limit=10_000,
        memory_mb=300,
    )
    assert result.exit_code != 0
    assert "allocated" not in result.stdout

import asyncio
import os
import time

import pytest

from sandbox.runner import Execution, InvalidRequest, run

from .conftest import MODES, TOKEN


def python(code: str, **kw) -> Execution:
    return Execution(workspace=kw.pop("workspace", "ws1"), kind="python", code=code, **kw)


def shell(code: str, **kw) -> Execution:
    return Execution(workspace=kw.pop("workspace", "ws1"), kind="shell", code=code, **kw)


async def test_python_runs_and_reports_changed_files(config, mode):
    result = await run(python("print('hello')\nopen('out.txt', 'w').write('x' * 10)"), config, mode)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "hello\n"
    assert result.killed_reason is None
    assert result.changed_files == [{"path": "out.txt", "size": 10, "status": "created"}]
    assert (config.workspaces_root / "ws1" / "out.txt").read_text() == "x" * 10

    again = await run(python("open('out.txt', 'a').write('y')"), config, mode)
    assert again.changed_files == [{"path": "out.txt", "size": 11, "status": "modified"}]


async def test_shell_runs_in_the_workspace(config, mode):
    (config.workspaces_root / "ws1").mkdir()
    (config.workspaces_root / "ws1" / "data.csv").write_text("a,b\n1,2\n")
    result = await run(shell("wc -l < data.csv; exit 3"), config, mode)
    assert result.exit_code == 3
    assert result.stdout.strip() == "2"


async def test_environment_is_scrubbed(config, mode):
    assert os.environ["SANDBOX_TOKEN"] == TOKEN
    result = await run(shell("env"), config, mode)
    assert TOKEN not in result.stdout
    assert "SANDBOX_TOKEN" not in result.stdout


async def test_wall_clock_timeout_kills(config, mode):
    started = time.monotonic()
    result = await run(python("import time\ntime.sleep(60)", timeout_seconds=1), config, mode)
    assert result.killed_reason == "timeout"
    assert time.monotonic() - started < 10


async def test_cpu_limit(config, mode):
    # Busy loop: the CPU rlimit (= timeout) or the wall clock stops it, never runs on.
    result = await run(python("while True: pass", timeout_seconds=1), config, mode)
    assert result.killed_reason in ("cpu_limit", "timeout")


async def test_memory_limit(config, mode):
    result = await run(python("x = bytearray(900 * 1024 * 1024)", memory_mb=128), config, mode)
    assert result.exit_code != 0
    assert "MemoryError" in result.stderr


async def test_output_is_capped_and_floods_are_killed(config, mode):
    result = await run(
        python("import sys\nwhile True: sys.stdout.write('x' * 65536)", output_limit=4096),
        config,
        mode,
    )
    assert result.stdout_truncated
    assert len(result.stdout) == 4096
    assert result.killed_reason == "output_limit"


async def test_file_size_limit(config, mode):
    config.max_file_bytes = 1024 * 1024
    result = await run(python("open('big', 'wb').write(b'0' * (4 * 1024 * 1024))"), config, mode)
    assert result.exit_code != 0
    assert (config.workspaces_root / "ws1" / "big").stat().st_size <= 1024 * 1024


@pytest.mark.parametrize(
    "script",
    [
        "(setsid sh -c 'sleep 2; touch alive' &)",  # holds the output pipes open
        "(nohup sh -c 'sleep 2; touch alive' &)",
        "setsid sh -c 'sleep 2; touch alive' >/dev/null 2>&1 </dev/null &",  # fully detached
    ],
)
async def test_background_processes_do_not_survive(config, mode, script):
    marker = config.workspaces_root / "ws1" / "alive"
    started = time.monotonic()
    await run(shell(script), config, mode)
    assert time.monotonic() - started < 2
    await asyncio.sleep(2.5)
    assert not marker.exists()


async def test_cancellation_kills_the_process(config, mode):
    marker = config.workspaces_root / "ws1" / "finished"
    task = asyncio.create_task(
        run(python("import time\ntime.sleep(2)\nopen('finished', 'w')"), config, mode)
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.5)
    assert not marker.exists()


@pytest.mark.parametrize("bad", ["../etc", "a/b", ".hidden", "", "x" * 200, "a/../b"])
async def test_invalid_workspace_ids_are_rejected(config, bad):
    with pytest.raises(InvalidRequest):
        await run(python("print(1)", workspace=bad), config, "process")


async def test_workspace_size_is_reported(config, mode):
    config.max_workspace_bytes = 1000
    result = await run(python("open('f', 'wb').write(b'0' * 5000)"), config, mode)
    assert result.workspace_bytes == 5000
    assert result.workspace_over_limit


bwrap_only = pytest.mark.skipif("bwrap" not in MODES, reason="bubblewrap unavailable here")


@bwrap_only
async def test_bwrap_has_no_network(config):
    code = (
        "import socket\n"
        "print(sorted(n for _, n in socket.if_nameindex()))\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "    print('connected')\n"
        "except OSError as e:\n"
        "    print('blocked')\n"
    )
    result = await run(python(code), config, "bwrap")
    assert result.stdout.splitlines() == ["['lo']", "blocked"]


@bwrap_only
async def test_bwrap_hides_other_workspaces_and_host_files(config, tmp_path):
    other = config.workspaces_root / "other"
    other.mkdir()
    (other / "secret.txt").write_text("other user's data")
    code = (
        f"ls -A {config.workspaces_root} | wc -l\n"
        f"cat {other}/secret.txt 2>/dev/null || echo hidden\n"
        "touch /etc/pwned 2>/dev/null && echo writable || echo readonly\n"
        "echo $$\n"
    )
    result = await run(shell(code), config, "bwrap")
    count, secret, root, pid = result.stdout.split()
    assert count == "0"
    assert secret == "hidden"
    assert root == "readonly"
    assert int(pid) < 10  # own PID namespace


@bwrap_only
async def test_bwrap_cannot_see_host_processes(config):
    result = await run(shell("ls /proc | grep -c '^[0-9]'"), config, "bwrap")
    assert int(result.stdout) < 10

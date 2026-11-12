import asyncio
import os
import sys

import pytest

from celius_agent.config import Policy
from celius_agent.runtime import Runtime
from celius_agent.state import StateStore


@pytest.fixture
def workdir(tmp_path):
    root = tmp_path / "code"
    (root / "proj").mkdir(parents=True)
    return root


def make_runtime(tmp_path, workdir, **policy) -> Runtime:
    store = StateStore(tmp_path / "state")
    return Runtime(
        store,
        Policy(roots=[{"path": str(workdir), "write": True}], allow_shell=True, **policy),
    )


def events(runtime: Runtime, session_id: str) -> list[dict]:
    return runtime.sessions[session_id].pending


async def settle(runtime: Runtime) -> None:
    while runtime.tasks:
        await asyncio.wait(set(runtime.tasks), timeout=30)


async def open_session(runtime, workdir, session_id="s1", directory=None):
    await runtime.handle_command(
        {
            "id": f"open-{session_id}",
            "kind": "session.open",
            "session_id": session_id,
            "payload": {
                "mode": "native",
                "adapter": "celius",
                "working_directory": str(directory or workdir / "proj"),
                "permissions": {"allow_shell": True, "allow_write": True},
                "next_seq": 1,
            },
        }
    )
    await settle(runtime)


async def test_open_and_execute(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await open_session(runtime, workdir)
    assert [e["type"] for e in events(runtime, "s1")] == ["session.opened"]
    assert events(runtime, "s1")[0]["data"]["working_directory"] == os.path.realpath(
        workdir / "proj"
    )
    await runtime.handle_command(
        {
            "id": "c1",
            "kind": "tool.execute",
            "session_id": "s1",
            "run_id": "r1",
            "payload": {"tool": "run_python", "arguments": {"code": "print(6 * 7)"}},
        }
    )
    await settle(runtime)
    types = [e["type"] for e in events(runtime, "s1")]
    assert types[1] == "command.started"
    assert types[-1] == "command.completed"
    done = events(runtime, "s1")[-1]
    assert done["run_id"] == "r1"
    assert "42" in done["data"]["result"]["content"]
    assert [e["seq"] for e in events(runtime, "s1")] == list(range(1, len(types) + 1))


async def test_a_redelivered_command_never_runs_twice(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await open_session(runtime, workdir)
    command = {
        "id": "c1",
        "kind": "tool.execute",
        "session_id": "s1",
        "payload": {"tool": "run_python", "arguments": {"code": "open('n','a').write('x')"}},
    }
    await runtime.handle_command(command)
    await settle(runtime)
    await runtime.handle_command(command)  # e.g. resent after a reconnect
    await settle(runtime)
    assert (workdir / "proj" / "n").read_text() == "x"
    completed = [e for e in events(runtime, "s1") if e["type"] == "command.completed"]
    assert len(completed) == 1


async def test_command_running_at_restart_is_reported_not_rerun(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await open_session(runtime, workdir)
    # Simulate a crash: the journal says started, nothing completed it.
    runtime.store.journal_put(
        "c9", {"id": "c9", "kind": "tool.execute", "session_id": "s1", "state": "started"}
    )
    restarted = make_runtime(tmp_path, workdir)
    await restarted.recover_interrupted()
    last = restarted.sessions["s1"].pending[-1]
    assert last["type"] == "command.completed"
    assert last["data"]["status"] == "failed"
    assert "not run again" in last["data"]["error"]
    # Delivered again: still not run.
    await restarted.handle_command(
        {
            "id": "c9",
            "kind": "tool.execute",
            "session_id": "s1",
            "payload": {"tool": "run_python", "arguments": {"code": "open('ran','w')"}},
        }
    )
    await settle(restarted)
    assert not (workdir / "proj" / "ran").exists()


async def test_session_outside_roots_or_adapter_not_allowed_fails(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await open_session(runtime, workdir, "s2", directory=tmp_path)
    assert events(runtime, "s2")[0]["type"] == "session.failed"
    await runtime.handle_command(
        {
            "id": "open-s3",
            "kind": "session.open",
            "session_id": "s3",
            "payload": {"adapter": "claude_code", "working_directory": str(workdir / "proj")},
        }
    )
    await settle(runtime)
    failed = events(runtime, "s3")[0]
    assert failed["type"] == "session.failed"
    assert "adapters" in failed["data"]["reason"]


async def test_cancel_and_close(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    await open_session(runtime, workdir)
    await runtime.handle_command(
        {
            "id": "long",
            "kind": "tool.execute",
            "session_id": "s1",
            "payload": {
                "tool": "run_python",
                "arguments": {"code": "import time; time.sleep(60)", "timeout_seconds": 120},
            },
        }
    )
    await asyncio.sleep(0.5)
    await runtime.handle_command(
        {"id": "x1", "kind": "cancel", "session_id": "s1", "payload": {"command_id": "long"}}
    )
    await settle(runtime)
    done = [e for e in events(runtime, "s1") if e["type"] == "command.completed"][-1]
    assert "cancelled" in done["data"]["result"]["content"]
    await runtime.handle_command({"id": "close", "kind": "session.close", "session_id": "s1"})
    await settle(runtime)
    assert events(runtime, "s1")[-1]["type"] == "session.closed"
    # Once Celius has everything, the closed session is forgotten.
    runtime.on_ack("s1", events(runtime, "s1")[-1]["seq"])
    assert "s1" not in runtime.sessions


async def test_welcome_restores_a_lost_session(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    runtime.on_welcome(
        {
            "s7": {
                "status": "idle",
                "last_seq": 12,
                "mode": "native",
                "adapter": "celius",
                "working_directory": str(workdir / "proj"),
                "permissions": {"allow_shell": True},
                "next_seq": 13,
            }
        }
    )
    session = runtime.sessions["s7"]
    assert session.status == "open"
    assert session.next_seq == 13


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_state_files_are_private(tmp_path, workdir):
    runtime = make_runtime(tmp_path, workdir)
    runtime.store.journal_put("c1", {"id": "c1", "state": "started"})
    mode = (tmp_path / "state" / "commands" / "c1.json").stat().st_mode
    assert mode & 0o077 == 0


async def test_frames_stay_small(tmp_path, workdir):
    """Many large events are sent in several frames, never one huge one."""
    import json

    from celius_agent.connection import SEND_BATCH_BYTES, Connection

    runtime = make_runtime(tmp_path, workdir)
    await open_session(runtime, workdir)
    session = runtime.sessions["s1"]
    for _ in range(40):
        runtime.store.append(session, "terminal.output", {"text": "y" * 200_000})

    class Socket:
        def __init__(self):
            self.frames = []

        async def send(self, text):
            self.frames.append(text)

    sock = Socket()
    connection = Connection.__new__(Connection)
    connection.runtime = runtime
    connection.sent_upto = {"s1": 0}
    while await connection._flush(sock):
        pass
    sizes = [len(f) for f in sock.frames]
    assert len(sock.frames) > 1
    assert max(sizes) <= SEND_BATCH_BYTES + 300_000
    sent = [e["seq"] for f in sock.frames for e in json.loads(f)["events"]]
    assert sent == list(range(1, len(session.pending) + 1))

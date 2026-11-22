"""Celius Remote end to end on this machine (Linux in CI; the same code runs on Windows):
the real API under uvicorn, the real worker loop, and the real agent (services/remote-agent)
connected over a WebSocket, with a scripted model that calls tools.

Covers: native execution with approval, live terminal/file events, a client that never
connects (work continues without it), agent disconnection and reconnection mid-task,
an offline machine (the command expires and never runs later), and two machines at once.
"""

import asyncio
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session_factory
from app.db.base import utcnow
from app.main import create_app
from app.models import RemoteCommand, Run, ToolCall
from app.services.model_gateway import get_model_gateway
from app.worker.loop import run_worker
from tests.conftest import PASSWORD
from tests.fakes import FakeLlama

AGENT_SRC = Path(__file__).resolve().parents[2] / "remote-agent"
pytestmark = [
    pytest.mark.skipif(
        not (AGENT_SRC / "celius_agent").exists(), reason="services/remote-agent not present"
    ),
    # Server, worker, agents and test share one database here. On SQLite (the unit-test
    # database) its single-writer lock stalls them now and then, which looks like a hang;
    # deployments use PostgreSQL. CI runs this on PostgreSQL; locally: make test-api-pg.
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="end-to-end remote tests run on PostgreSQL (set TEST_DATABASE_URL)",
    ),
]
sys.path.insert(0, str(AGENT_SRC))

from celius_agent.config import (  # noqa: E402
    AgentConfig,
    Paths,
    load_policy,  # noqa: E402
    save_config,
)
from celius_agent.connection import Connection  # noqa: E402
from celius_agent.credentials import save_credential  # noqa: E402
from celius_agent.monitor import Monitor  # noqa: E402
from celius_agent.runtime import Runtime  # noqa: E402
from celius_agent.state import StateStore  # noqa: E402


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


@pytest.fixture
def e2e_settings(settings):
    settings.remote_poll_interval_seconds = 0.05
    settings.remote_heartbeat_seconds = 2
    settings.remote_offline_wait_seconds = 3
    settings.worker_poll_interval_seconds = 0.05
    settings.run_lease_seconds = 30  # the production lease
    return settings


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def server(e2e_settings, sessionmaker, gateway):
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    app.dependency_overrides[get_settings] = lambda: e2e_settings
    app.dependency_overrides[get_model_gateway] = lambda: gateway
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="auto", lifespan="off"
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    while not instance.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    await asyncio.wait_for(task, 10)


@pytest.fixture
async def worker(sessionmaker, gateway, e2e_settings):
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(
            factory=sessionmaker, gateway=gateway, settings=e2e_settings, stop=stop, worker_id="w1"
        )
    )
    yield
    stop.set()
    await asyncio.wait_for(task, 30)


@pytest.fixture
async def http(server, user):
    async with httpx.AsyncClient(base_url=server, timeout=30) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
        )
        client.cookies.clear()
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client


class AgentProcess:
    """The real agent, as `celius-agent run` starts it, inside this event loop."""

    def __init__(self, home: Path, server: str, roots: list[Path], allow_shell: bool = True):
        self.home = home
        self.server = server
        self.paths = Paths(home)
        self.paths.ensure()
        self.paths.policy.write_text(
            json.dumps(
                {
                    "roots": [{"path": str(r), "write": True} for r in roots],
                    "allow_shell": allow_shell,
                    "allow_write": True,
                }
            )
        )
        self.task: asyncio.Task | None = None
        self.stop: asyncio.Event | None = None
        self.runtime: Runtime | None = None

    async def register(self, http: httpx.AsyncClient, name: str) -> str:
        """`celius login`, run for real, and accepted in Celius by the test user."""
        from celius_agent.login import device_login

        config = AgentConfig(server_url=self.server)
        accepted: list[str] = []

        def say(line: str) -> None:
            if "Sign-in code:" in line:
                accepted.append(line.split(":", 1)[1].strip())

        async def accept() -> None:
            while not accepted:
                await asyncio.sleep(0.05)
            response = await http.post(
                "/api/v1/remote/devices/accept", json={"user_code": accepted[0], "name": name}
            )
            assert response.status_code == 201, response.text

        acceptor = asyncio.create_task(accept())
        result = await asyncio.to_thread(
            device_login, config, name=name, say=say, sleep=lambda _: time.sleep(0.1)
        )
        await acceptor
        config.agent_id, config.name = result["agent_id"], result["name"]
        save_config(self.paths, config)
        save_credential(self.paths.credential, result["credential"])
        return result["agent_id"]

    def start(self) -> None:
        from celius_agent.config import load_config
        from celius_agent.credentials import load_credential

        config = load_config(self.paths)
        policy = load_policy(self.paths)
        self.runtime = Runtime(
            StateStore(self.paths.state), policy, policy_loader=lambda: load_policy(self.paths)
        )
        connection = Connection(
            config, self.runtime, load_credential(self.paths.credential), Monitor()
        )
        self.stop = asyncio.Event()

        async def main():
            await self.runtime.recover_interrupted()
            await connection.run_forever(self.stop)

        self.task = asyncio.create_task(main())

    async def kill(self) -> None:
        """Like a crash or a network drop: no goodbye, running commands cut off."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        for env in list(self.runtime.running.values()):
            env.cancel.set()
        for task in list(self.runtime.tasks):
            task.cancel()
        await asyncio.gather(*self.runtime.tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        if self.task and not self.task.done():
            self.stop.set()
            await asyncio.wait_for(self.task, 15)
            await self.runtime.shutdown()


async def until(predicate, wait: float = 20, what: str = "condition"):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def machine(http, agent_id):
    return (await http.get(f"/api/v1/remote/agents/{agent_id}")).json()


async def open_session(http, agent_id, directory: Path) -> dict:
    response = await http.post(
        "/api/v1/remote/sessions",
        json={"agent_id": agent_id, "title": "Remote task", "working_directory": str(directory)},
    )
    assert response.status_code == 201, response.text
    session = response.json()

    async def opened():
        current = (await http.get(f"/api/v1/remote/sessions/{session['id']}")).json()
        return current if current["status"] == "idle" else None

    return await until(opened, what="session open")


async def send(http, conversation_id, text="Do it") -> dict:
    response = await http.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": uuid.uuid4().hex, "content": text},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def pending_call(http, conversation_id):
    async def find():
        detail = (await http.get(f"/api/v1/conversations/{conversation_id}")).json()
        for message in detail["messages"]:
            for call in message["tool_calls"]:
                if call["status"] == "pending_approval":
                    return call
        return None

    return await until(find, what="an approval request")


async def approve(http, call):
    response = await http.post(
        f"/api/v1/tool-calls/{call['id']}/approve", json={"args_hash": call["args_hash"]}
    )
    assert response.status_code == 200, response.text


async def run_of(sessionmaker, run_id) -> Run:
    async with sessionmaker() as db:
        return await db.get(Run, uuid.UUID(run_id))


async def finished(sessionmaker, run_id, wait=30) -> Run:
    async def done():
        run = await run_of(sessionmaker, run_id)
        return run if run.status.value in ("completed", "failed", "cancelled") else None

    try:
        return await until(done, wait, "the run to finish")
    except AssertionError:
        async with sessionmaker() as db:
            run = await db.get(Run, uuid.UUID(run_id))
            calls = (await db.scalars(select(ToolCall).where(ToolCall.run_id == run.id))).all()
            commands = (await db.scalars(select(RemoteCommand))).all()
        raise AssertionError(
            f"run {run.status} attempts={run.attempts} worker={run.worker_id}; calls="
            f"{[(c.tool_name, c.status.value) for c in calls]}; commands="
            f"{[(c.kind, c.status, c.error, str(c.completed_at)) for c in commands]}; "
            f"now={utcnow()}"
        ) from None


# ---------------------------------------------------------------------- tests


async def test_native_task_runs_on_the_machine_with_approval(
    http, server, worker, fake, sessionmaker, tmp_path
):
    work = tmp_path / "code"
    (work / "project").mkdir(parents=True)
    agent = AgentProcess(tmp_path / "agent-home", server, [work])
    agent_id = await agent.register(http, "Linux box")
    agent.start()
    try:
        state = await until(lambda: _online(http, agent_id), what="the machine to come online")
        assert state["state"] == "idle"
        assert state["resources"]["memory"]["total_mb"] > 0
        session = await open_session(http, agent_id, work / "project")

        fake.turns = [
            [
                {
                    "name": "run_shell",
                    "arguments": {"command": "echo remote-ok > out.txt; cat out.txt"},
                }
            ],
        ]
        fake.reply = "The command ran on the machine."
        sent = await send(http, session["conversation_id"], "Write a file there")
        run = await run_of(sessionmaker, sent["run"]["id"])
        assert run.execution == "remote"

        # Commands on a real machine ask first (machine default: approval always).
        call = await pending_call(http, session["conversation_id"])
        assert call["tool_name"] == "run_shell"
        busy = await machine(http, agent_id)
        assert busy["state"] == "needs_attention"
        await approve(http, call)
        run = await finished(sessionmaker, sent["run"]["id"])
        assert run.status.value == "completed", run.error_message

        assert (work / "project" / "out.txt").read_text().strip() == "remote-ok"
        detail = (await http.get(f"/api/v1/conversations/{session['conversation_id']}")).json()
        reply = detail["messages"][-1]
        assert "machine" in "".join(p["text"] or "" for p in reply["parts"])
        result = reply["tool_calls"][0]
        assert result["status"] == "completed"
        assert "remote-ok" in json.dumps(result["result"])
        assert detail["remote"]["agent_name"] == "Linux box"
        assert detail["remote"]["status"] == "idle"
        # The tool definitions offered were the machine's (same names as the built-ins).
        offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert {"run_shell", "read_file", "write_file"} <= offered
        assert "attach_file" not in offered

        events = (await http.get(f"/api/v1/remote/sessions/{session['id']}/events")).json()["items"]
        types = [e["type"] for e in events]
        assert types[0] == "session.opened"
        assert "terminal.output" in types
        assert {"command.started", "command.completed"} <= set(types)
        changed = [e["data"] for e in events if e["type"] == "file.changed"]
        assert changed and changed[0]["path"] == "out.txt"
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    finally:
        await agent.shutdown()


async def _online(http, agent_id):
    state = await machine(http, agent_id)
    return state if state["online"] and state["resources"] else None


async def test_agent_disconnects_mid_task_and_events_are_replayed(
    http, server, worker, fake, sessionmaker, tmp_path
):
    """The machine loses its connection while a command runs; the command keeps running
    there, its events wait in the outbox, and everything arrives after reconnecting."""
    work = tmp_path / "code"
    work.mkdir()
    agent = AgentProcess(tmp_path / "agent-home", server, [work])
    agent_id = await agent.register(http, "Flaky")
    await http.patch(f"/api/v1/remote/agents/{agent_id}", json={"config": {"approval": "profile"}})
    agent.start()
    await until(lambda: _online(http, agent_id), what="online")
    session = await open_session(http, agent_id, work)
    await http.patch(
        f"/api/v1/conversations/{session['conversation_id']}",
        json={"tool_profile": "developer"},  # commands run without asking
    )
    script = (
        "import time\nfor i in range(3):\n    print('tick', i, flush=True)\n    time.sleep(1)\n"
    )
    fake.turns = [[{"name": "run_python", "arguments": {"code": script}}]]
    fake.reply = "Finished."
    sent = await send(http, session["conversation_id"])

    async def running():
        async with sessionmaker() as db:
            command = await db.scalar(
                select(RemoteCommand).where(RemoteCommand.kind == "tool.execute")
            )
            return command if command and command.status == "running" else None

    await until(running, what="the command to start")
    # Drop the network: close the socket from the agent's side without a goodbye. The
    # runtime (and the command) keep going.
    connection_task = agent.task
    agent.task = None
    connection_task.cancel()
    await asyncio.gather(connection_task, return_exceptions=True)
    state = await until(lambda: _state(http, agent_id, "disconnected"), what="disconnected state")
    assert state["disconnect_reason"] == "lost"
    await asyncio.sleep(3.5)  # the command finishes while offline
    assert not agent.runtime.running
    assert any(
        e["type"] == "command.completed" for s in agent.runtime.sessions.values() for e in s.pending
    )

    # Reconnect (same runtime and outbox, like the network coming back).
    from celius_agent.config import load_config
    from celius_agent.credentials import load_credential

    connection = Connection(
        load_config(agent.paths), agent.runtime, load_credential(agent.paths.credential), Monitor()
    )
    agent.stop = asyncio.Event()
    agent.task = asyncio.create_task(connection.run_forever(agent.stop))
    run = await finished(sessionmaker, sent["run"]["id"], wait=40)
    assert run.status.value == "completed", run.error_message
    events = (await http.get(f"/api/v1/remote/sessions/{session['id']}/events")).json()["items"]
    ticks = "".join(e["data"].get("text", "") for e in events if e["type"] == "terminal.output")
    assert "tick 0" in ticks and "tick 2" in ticks
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))  # none lost, none twice
    await agent.shutdown()


async def _state(http, agent_id, wanted):
    state = await machine(http, agent_id)
    return state if state["state"] == wanted else None


async def test_offline_machine_command_expires_and_never_runs_later(
    http, server, worker, fake, sessionmaker, tmp_path
):
    work = tmp_path / "code"
    work.mkdir()
    agent = AgentProcess(tmp_path / "agent-home", server, [work])
    agent_id = await agent.register(http, "Laptop")
    await http.patch(f"/api/v1/remote/agents/{agent_id}", json={"config": {"approval": "profile"}})
    agent.start()
    await until(lambda: _online(http, agent_id), what="online")
    session = await open_session(http, agent_id, work)
    await http.patch(
        f"/api/v1/conversations/{session['conversation_id']}", json={"tool_profile": "developer"}
    )
    await agent.shutdown()  # the laptop goes to sleep
    fake.turns = [[{"name": "run_shell", "arguments": {"command": "touch late.txt"}}]]
    fake.reply = "It was offline."
    sent = await send(http, session["conversation_id"])
    run = await finished(sessionmaker, sent["run"]["id"], wait=30)
    assert run.status.value == "completed"
    detail = (await http.get(f"/api/v1/conversations/{session['conversation_id']}")).json()
    call = detail["messages"][-1]["tool_calls"][0]
    assert call["is_error"] and "offline" in json.dumps(call["result"]).lower()
    # The laptop wakes up: the expired command is not delivered or run.
    agent.start()
    await until(lambda: _online(http, agent_id), what="online again")
    await asyncio.sleep(1)
    assert not (work / "late.txt").exists()
    await agent.shutdown()


async def test_two_machines_run_sessions_at_the_same_time(
    http, server, worker, fake, sessionmaker, tmp_path, e2e_settings
):
    e2e_settings.model_parallel_requests = 2
    agents, sessions = [], []
    for name in ("Alpha", "Beta"):
        work = tmp_path / name
        work.mkdir()
        agent = AgentProcess(tmp_path / f"home-{name}", server, [work])
        agent_id = await agent.register(http, name)
        await http.patch(
            f"/api/v1/remote/agents/{agent_id}", json={"config": {"approval": "profile"}}
        )
        agent.start()
        agents.append((agent, work))
        await until(lambda a=agent_id: _online(http, a), what=f"{name} online")
        session = await open_session(http, agent_id, work)
        await http.patch(
            f"/api/v1/conversations/{session['conversation_id']}",
            json={"tool_profile": "developer"},
        )
        sessions.append(session)
    try:
        fake.turns = [
            [{"name": "run_python", "arguments": {"code": "open('mark','w').write('A')"}}],
            [{"name": "run_python", "arguments": {"code": "open('mark','w').write('B')"}}],
        ]
        fake.reply = "Done."
        first = await send(http, sessions[0]["conversation_id"])
        second = await send(http, sessions[1]["conversation_id"])
        for sent in (first, second):
            run = await finished(sessionmaker, sent["run"]["id"])
            assert run.status.value == "completed", run.error_message
        marks = sorted((work / "mark").read_text() for _, work in agents)
        assert marks == ["A", "B"]  # each machine ran its own session's command
    finally:
        for agent, _ in agents:
            await agent.shutdown()


class ScriptedUI:
    """The `celius` chat's terminal, answered by the test."""

    def __init__(self, approve: str | None = "once"):
        self.text: list[str] = []
        self.tools: list[str] = []
        self.outputs: list[str] = []
        self.notes: list[str] = []
        self.answer = approve
        self.asked: list[dict] = []

    def write(self, text):
        self.text.append(text)

    def note(self, text):
        self.notes.append(text)

    def tool(self, text):
        self.tools.append(text)

    def output(self, text):
        self.outputs.append(text)

    def error(self, text):
        self.notes.append(f"error: {text}")

    def confirm(self, question):
        return True

    def approve(self, call):
        self.asked.append(call)
        return self.answer


async def test_celius_cli_chat_runs_here_and_shows_in_celius(
    http, server, worker, fake, sessionmaker, tmp_path
):
    """`celius` started in a folder on the device: Celius's model and approvals, tools
    run in that folder, approved from the terminal; the chat is an ordinary conversation."""
    from celius_agent.chat import Chat, ensure_folder_allowed
    from celius_agent.client import DeviceClient
    from celius_agent.config import load_config
    from celius_agent.credentials import load_credential

    project = tmp_path / "project"
    project.mkdir()
    agent = AgentProcess(tmp_path / "agent-home", server, [], allow_shell=False)
    agent_id = await agent.register(http, "Dev laptop")
    ui = ScriptedUI()
    # First start in this folder: the person at the keyboard allows it.
    assert ensure_folder_allowed(agent.paths, str(project), ui)
    agent.start()  # the agent the chat starts when no service runs
    await until(lambda: _online(http, agent_id), what="online")

    client = DeviceClient(load_config(agent.paths), load_credential(agent.paths.credential))
    chat = Chat(client, ui)
    await asyncio.to_thread(chat.start, str(project))
    fake.turns = [
        [{"name": "run_shell", "arguments": {"command": "echo from-the-cli > made.txt"}}],
    ]
    fake.reply = "Created made.txt."
    status = await asyncio.to_thread(chat.send, "Make a file here")
    assert status == "completed", ui.notes
    assert (project / "made.txt").read_text().strip() == "from-the-cli"
    assert [c["tool_name"] for c in ui.asked] == ["run_shell"]  # asked in the terminal
    assert ui.tools and ui.tools[0].startswith("run_shell")
    assert "Created made.txt." in "".join(ui.text)

    # The same conversation in Celius, marked as started on the device.
    conversations = (await http.get("/api/v1/conversations")).json()["items"]
    mine = next(c for c in conversations if c["id"] == chat.conversation_id)
    assert mine["remote"]["origin"] == "device"
    assert mine["remote"]["agent_name"] == "Dev laptop"
    detail = (await http.get(f"/api/v1/conversations/{chat.conversation_id}")).json()
    call = detail["messages"][-1]["tool_calls"][0]
    assert call["status"] == "completed" and call["decision_source"] == "user"
    # Another machine can't reach this chat.
    other = AgentProcess(tmp_path / "other-home", server, [])
    await other.register(http, "Other")
    stranger = DeviceClient(load_config(other.paths), load_credential(other.paths.credential))
    with pytest.raises(Exception) as denied:
        await asyncio.to_thread(
            stranger.call, "GET", f"/api/v1/remote/device/chats/{chat.conversation_id}"
        )
    assert getattr(denied.value, "status", None) == 404
    await agent.shutdown()


async def test_cli_one_shot_waits_for_approval_in_celius(
    http, server, worker, fake, sessionmaker, tmp_path
):
    """`celius -p …` without a terminal: the request is answered in Celius instead."""
    from celius_agent.chat import Chat
    from celius_agent.client import DeviceClient
    from celius_agent.config import load_config
    from celius_agent.credentials import load_credential

    project = tmp_path / "project"
    project.mkdir()
    agent = AgentProcess(tmp_path / "agent-home", server, [project])
    agent_id = await agent.register(http, "Server")
    agent.start()
    await until(lambda: _online(http, agent_id), what="online")
    ui = ScriptedUI(approve=None)
    chat = Chat(DeviceClient(load_config(agent.paths), load_credential(agent.paths.credential)), ui)
    await asyncio.to_thread(chat.start, str(project))
    fake.turns = [[{"name": "run_python", "arguments": {"code": "print(6 * 7)"}}]]
    fake.reply = "42"
    sending = asyncio.create_task(asyncio.to_thread(chat.send, "Compute"))
    call = await pending_call(http, chat.conversation_id)
    await approve(http, call)  # from the phone / web app
    assert await asyncio.wait_for(sending, 60) == "completed"
    assert any("Waiting for a decision in Celius" in n for n in ui.notes)
    assert any("42" in o for o in ui.outputs)
    await agent.shutdown()


async def test_ended_session_is_not_moved_back_to_the_sandbox(http, server, tmp_path):
    work = tmp_path / "code"
    work.mkdir()
    agent = AgentProcess(tmp_path / "agent-home", server, [work])
    agent_id = await agent.register(http, "Box")
    response = await http.post(
        "/api/v1/remote/sessions",
        json={"agent_id": agent_id, "working_directory": str(work)},
    )
    session = response.json()
    await http.post(f"/api/v1/remote/sessions/{session['id']}/terminate")
    response = await http.post(
        f"/api/v1/conversations/{session['conversation_id']}/messages",
        json={"client_message_id": uuid.uuid4().hex, "content": "hi"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "remote_session_ended"

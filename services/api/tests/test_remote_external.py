"""Celius Remote, Phase 4: an external agent (Claude Code) session projected into the
conversation, with its permission prompts decided through the existing approval routes.

The machine side is simulated at the protocol level (the events the agent's Claude Code
adapter emits; the adapter's translation is tested in services/remote-agent)."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session_factory
from app.main import create_app
from app.models import RemoteCommand, Run, RunStatus
from app.services.model_gateway import get_model_gateway
from app.worker.executor import claim_next_run
from tests.conftest import PASSWORD
from tests.remote_helpers import ASGIWebSocket, hello, register_machine

WS_PATH = "/api/v1/remote/agent/connect"


@pytest.fixture
def remote_settings(settings):
    settings.remote_poll_interval_seconds = 0.02
    return settings


@pytest.fixture
async def app(remote_settings, sessionmaker, gateway):
    application = create_app()
    application.dependency_overrides[get_session_factory] = lambda: sessionmaker
    application.dependency_overrides[get_settings] = lambda: remote_settings
    application.dependency_overrides[get_model_gateway] = lambda: gateway
    return application


@pytest.fixture
async def http(app, user):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
        )
        client.cookies.clear()
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client


class FakeClaudeMachine:
    """Speaks the agent protocol like a machine running the Claude Code adapter."""

    def __init__(self, sock: ASGIWebSocket, session_id: str):
        self.sock = sock
        self.session_id = session_id
        self.seq = 0

    async def emit(self, type_: str, data: dict, run_id: str | None = None) -> int:
        self.seq += 1
        event = {"seq": self.seq, "type": type_, "data": data}
        if run_id:
            event["run_id"] = run_id
        await self.sock.send_json(
            {"type": "events", "session_id": self.session_id, "events": [event]}
        )
        ack = await self.sock.receive_until("ack")
        assert ack["seq"] == self.seq
        return self.seq

    async def command(self, kind: str) -> dict:
        while True:
            frame = await self.sock.receive_until("command")
            if frame["kind"] == kind:
                return frame


async def _setup(app, http):
    machine = await register_machine(http, http.headers)
    sock = ASGIWebSocket(app, WS_PATH, {"Authorization": f"Bearer {machine['token']}"})
    await sock.__aenter__()
    await sock.send_json(
        hello(
            capabilities={"claude_code": {"sdk": "0.2", "cli": "/usr/bin/claude"}},
            local_policy={
                "roots": ["/srv/work"],
                "allow_shell": True,
                "allow_write": True,
                "adapters": ["celius", "claude_code"],
            },
        )
    )
    await sock.receive_json()
    response = await http.post(
        "/api/v1/remote/sessions",
        json={
            "agent_id": machine["agent_id"],
            "adapter": "claude_code",
            "title": "Build authentication",
            "working_directory": "/srv/work/app",
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["mode"] == "external"
    fake = FakeClaudeMachine(sock, session["id"])
    opened = await fake.command("session.open")
    assert opened["payload"]["adapter"] == "claude_code"
    await fake.emit("session.opened", {"command_id": opened["id"]})
    return machine, session, fake


async def _send(http, conversation_id, text):
    response = await http.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": uuid.uuid4().hex, "content": text},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _detail(http, conversation_id):
    return (await http.get(f"/api/v1/conversations/{conversation_id}")).json()


async def _run(sessionmaker, run_id) -> Run:
    async with sessionmaker() as db:
        return await db.get(Run, uuid.UUID(run_id))


async def test_claude_code_turn_with_approval(app, http, sessionmaker, settings):
    machine, session, fake = await _setup(app, http)
    conversation_id = session["conversation_id"]
    sent = await _send(http, conversation_id, "Add a login endpoint")
    run_id = sent["run"]["id"]
    run = await _run(sessionmaker, run_id)
    assert run.execution == "external"
    # The worker never takes it: the agent on the machine drives this run.
    assert await claim_next_run(sessionmaker, "w1", settings) is None

    message = await fake.command("session.message")
    assert message["payload"]["text"] == "Add a login endpoint"
    assert message["run_id"] == run_id

    await fake.emit("session.started", {"external_session_id": "cc-123"})
    await fake.emit("turn.started", {"run_id": run_id}, run_id)
    assert (await _run(sessionmaker, run_id)).status == RunStatus.running
    await fake.emit("assistant.text", {"text": "Let me look at the code."}, run_id)
    await fake.emit(
        "tool.started",
        {"tool_use_id": "toolu_1", "name": "Read", "input": {"file_path": "a.py"}},
        run_id,
    )
    await fake.emit("tool.completed", {"tool_use_id": "toolu_1", "content": "def a(): ..."}, run_id)
    # Claude Code asks before running a command: it becomes a Celius approval.
    await fake.emit(
        "permission.request",
        {
            "request_id": "req-9",
            "tool_use_id": "toolu_2",
            "tool": "Bash",
            "input": {"command": "pytest -q"},
        },
        run_id,
    )
    run = await _run(sessionmaker, run_id)
    assert run.status == RunStatus.awaiting_approval
    pending = (await http.get("/api/v1/tool-calls/pending")).json()
    assert [p["tool_name"] for p in pending] == ["Bash"]
    assert pending[0]["risk"] == "execute"
    sessions = (await http.get(f"/api/v1/remote/agents/{machine['agent_id']}")).json()
    assert sessions["state"] == "needs_attention"

    response = await http.post(
        f"/api/v1/tool-calls/{pending[0]['id']}/approve",
        json={"args_hash": pending[0]["args_hash"]},
    )
    assert response.status_code == 200, response.text
    decision = await fake.command("permission.decision")
    assert decision["payload"] == {"request_id": "req-9", "behavior": "allow", "message": None}
    run = await _run(sessionmaker, run_id)
    assert run.status == RunStatus.running  # back to the agent, not queued for a worker
    assert await claim_next_run(sessionmaker, "w1", settings) is None

    await fake.emit("tool.started", {"tool_use_id": "toolu_2", "name": "Bash", "input": {}}, run_id)
    await fake.emit("tool.completed", {"tool_use_id": "toolu_2", "content": "3 passed"}, run_id)
    await fake.emit(
        "file.changed",
        {"path": "/srv/work/app/login.py", "action": "written", "diff": "+x"},
        run_id,
    )
    await fake.emit("assistant.text", {"text": "The endpoint is added and tests pass."}, run_id)
    await fake.emit(
        "turn.completed",
        {
            "is_error": False,
            "result": "done",
            "total_cost_usd": 0.02,
            "external_session_id": "cc-123",
        },
        run_id,
    )

    run = await _run(sessionmaker, run_id)
    assert run.status == RunStatus.completed
    assert run.usage["total_cost_usd"] == 0.02
    detail = await _detail(http, conversation_id)
    reply = detail["messages"][-1]
    assert reply["status"] == "complete"
    texts = [p["text"] for p in reply["parts"] if p["type"] == "text"]
    assert texts == ["Let me look at the code.", "The endpoint is added and tests pass."]
    calls = [(c["tool_name"], c["status"]) for c in reply["tool_calls"]]
    assert calls == [("Read", "completed"), ("Bash", "completed")]
    assert reply["tool_calls"][1]["decision_source"] == "user"
    assert detail["remote"]["adapter"] == "claude_code"
    remote = (await http.get(f"/api/v1/remote/sessions/{session['id']}")).json()
    assert remote["external_session_id"] == "cc-123"
    assert remote["status"] == "idle"

    # The run's own SSE stream carries the reply like any other.
    stream = await http.get(f"/api/v1/runs/{run_id}/events")
    assert "message.delta" in stream.text and "run.completed" in stream.text

    # A follow-up instruction goes to the same Claude Code session.
    second = await _send(http, conversation_id, "Now add logout")
    message = await fake.command("session.message")
    assert message["run_id"] == second["run"]["id"]
    await fake.sock.disconnect()


async def test_denial_and_stop(app, http, sessionmaker):
    _, session, fake = await _setup(app, http)
    conversation_id = session["conversation_id"]
    sent = await _send(http, conversation_id, "Delete the build folder")
    run_id = sent["run"]["id"]
    await fake.command("session.message")
    await fake.emit("turn.started", {}, run_id)
    await fake.emit(
        "permission.request",
        {
            "request_id": "r1",
            "tool_use_id": "t1",
            "tool": "Bash",
            "input": {"command": "rm -rf build"},
        },
        run_id,
    )
    pending = (await http.get("/api/v1/tool-calls/pending")).json()[0]
    await http.post(f"/api/v1/tool-calls/{pending['id']}/deny", json={"reason": "Not now"})
    decision = await fake.command("permission.decision")
    assert decision["payload"]["behavior"] == "deny"
    assert decision["payload"]["message"] == "Not now"

    await fake.emit(
        "permission.request",
        {"request_id": "r2", "tool_use_id": "t2", "tool": "Write", "input": {"file_path": "x"}},
        run_id,
    )
    # The user stops the reply instead of deciding.
    response = await http.post(f"/api/v1/runs/{run_id}/cancel")
    assert response.json()["status"] == "cancelled"
    interrupt = await fake.command("interrupt")
    assert interrupt["run_id"] == run_id
    detail = await _detail(http, conversation_id)
    assert [c["status"] for c in detail["messages"][-1]["tool_calls"]] == ["denied", "cancelled"]
    # Events still in flight from the machine don't reopen the stopped reply.
    await fake.emit("assistant.text", {"text": "late"}, run_id)
    assert (await _run(sessionmaker, run_id)).status == RunStatus.cancelled
    await fake.sock.disconnect()


async def test_external_history_cannot_fork(app, http, sessionmaker):
    _, session, fake = await _setup(app, http)
    sent = await _send(http, session["conversation_id"], "First")
    run_id = sent["run"]["id"]
    await fake.command("session.message")
    await fake.emit("turn.started", {}, run_id)
    await fake.emit("assistant.text", {"text": "ok"}, run_id)
    await fake.emit("turn.completed", {"is_error": False}, run_id)
    response = await http.post(
        f"/api/v1/messages/{sent['assistant_message']['id']}/regenerate",
        json={"client_request_id": uuid.uuid4().hex},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_supported_for_external_agent"
    await fake.sock.disconnect()


async def test_claude_code_needs_the_machine_to_allow_it(app, http):
    machine = await register_machine(http, http.headers)
    sock = ASGIWebSocket(app, WS_PATH, {"Authorization": f"Bearer {machine['token']}"})
    async with sock:
        await sock.send_json(hello())  # policy allows only "celius"
        await sock.receive_json()
        response = await http.post(
            "/api/v1/remote/sessions",
            json={
                "agent_id": machine["agent_id"],
                "adapter": "claude_code",
                "working_directory": "/srv/work",
            },
        )
        assert response.json()["error"]["code"] == "adapter_not_allowed"


async def test_agent_error_fails_the_run(app, http, sessionmaker):
    _, session, fake = await _setup(app, http)
    sent = await _send(http, session["conversation_id"], "Go")
    run_id = sent["run"]["id"]
    await fake.command("session.message")
    await fake.emit("turn.started", {}, run_id)
    await fake.emit("turn.completed", {"is_error": True, "result": "API overloaded"}, run_id)
    run = await _run(sessionmaker, run_id)
    assert run.status == RunStatus.failed
    assert run.error_message == "API overloaded"
    async with sessionmaker() as db:
        kinds = [c.kind for c in (await db.scalars(select(RemoteCommand))).all()]
    assert kinds.count("session.message") == 1
    await asyncio.sleep(0)
    await fake.sock.disconnect()

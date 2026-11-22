"""Celius Remote, Phase 1: registration, authorisation, credentials, tokens, the agent
connection (heartbeats, supersession, revocation) and durable session events."""

import asyncio
import time
import uuid

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.config import get_settings
from app.db import get_session_factory
from app.db.base import utcnow
from app.main import create_app
from app.models import AuditEvent, RemoteCommand, RemoteDeviceRequest, RemoteSessionEvent, User
from app.services.model_gateway import get_model_gateway
from app.services.remote import identity
from tests.conftest import PASSWORD
from tests.remote_helpers import ASGIWebSocket, WSClosed, hello, machine_row, register_machine

WS_PATH = "/api/v1/remote/agent/connect"


@pytest.fixture
def remote_settings(settings):
    settings.remote_poll_interval_seconds = 0.02
    settings.remote_heartbeat_seconds = 2
    return settings


@pytest.fixture
async def app(remote_settings, sessionmaker, gateway):
    application = create_app()
    application.dependency_overrides[get_session_factory] = lambda: sessionmaker
    application.dependency_overrides[get_settings] = lambda: remote_settings
    application.dependency_overrides[get_model_gateway] = lambda: gateway
    return application


@pytest.fixture
async def http(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


@pytest.fixture
async def headers(http, user):
    response = await http.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    http.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _other_user_headers(http, sessionmaker) -> dict:
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password(PASSWORD)))
        await db.commit()
    response = await http.post(
        "/api/v1/auth/login", json={"username": "other", "password": PASSWORD}
    )
    http.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def agent_url(machine: dict) -> str:
    return f"/api/v1/remote/agents/{machine['agent_id']}"


def ws(app, token: str | None):
    return ASGIWebSocket(app, WS_PATH, {"Authorization": f"Bearer {token}"} if token else {})


async def wait_for(predicate, wait: float = 5):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------------- registration


DEVICE = {"hostname": "build-box", "os": "linux", "agent_version": "0.2.0", "protocol_version": 1}


async def _start_login(http, **extra) -> dict:
    response = await http.post("/api/v1/remote/device/login", json={**DEVICE, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def _poll(http, login) -> httpx.Response:
    return await http.post(
        "/api/v1/remote/device/login/poll", json={"device_code": login["device_code"]}
    )


async def test_device_signs_in_and_is_accepted_in_celius(http, headers):
    login = await _start_login(http, username="owner", name="Build box")
    assert len(login["user_code"]) == 9 and login["user_code"][4] == "-"
    assert login["verification_url"].endswith(f"#/devices?code={login['user_code']}")
    # Waiting: the device keeps polling.
    assert (await _poll(http, login)).json()["error"]["code"] == "authorization_pending"
    # Named at login, it waits in the owner's list with the same code.
    waiting = (await http.get("/api/v1/remote/devices", headers=headers)).json()
    assert [d["user_code"] for d in waiting["items"]] == [login["user_code"]]
    assert waiting["items"][0]["hostname"] == "build-box"
    assert waiting["login_command"].endswith("--user owner")
    assert (await http.get("/api/v1/remote/agents", headers=headers)).json()["items"] == []

    accepted = await http.post(
        "/api/v1/remote/devices/accept",
        json={"user_code": login["user_code"].lower().replace("-", "")},  # typed loosely
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    machine = accepted.json()
    assert machine["name"] == "Build box"
    assert machine["status"] == "active"
    assert (await http.get("/api/v1/remote/devices", headers=headers)).json()["items"] == []

    result = await _poll(http, login)
    assert result.status_code == 200
    assert result.json()["agent_id"] == machine["id"]
    assert result.json()["username"] == "owner"
    # The credential is handed out once.
    assert (await _poll(http, login)).json()["error"]["code"] == "invalid_grant"
    token = await http.post(
        "/api/v1/remote/token", json={"credential": result.json()["credential"]}
    )
    assert token.status_code == 200


async def test_refused_and_expired_sign_ins(http, headers, sessionmaker):
    login = await _start_login(http)
    refused = await http.post(
        "/api/v1/remote/devices/refuse", json={"user_code": login["user_code"]}, headers=headers
    )
    assert refused.status_code == 204
    assert (await _poll(http, login)).json()["error"]["code"] == "access_denied"

    login = await _start_login(http)
    async with sessionmaker() as db:
        for row in (await db.scalars(select(RemoteDeviceRequest))).all():
            row.expires_at = utcnow()
        await db.commit()
    assert (await _poll(http, login)).json()["error"]["code"] == "expired_token"
    late = await http.post(
        "/api/v1/remote/devices/accept", json={"user_code": login["user_code"]}, headers=headers
    )
    assert late.status_code == 404
    wrong = await http.post(
        "/api/v1/remote/devices/accept", json={"user_code": "BCDF-GHJK"}, headers=headers
    )
    assert wrong.status_code == 404


async def test_a_device_named_for_someone_else_is_theirs_alone(http, headers, sessionmaker):
    other = await _other_user_headers(http, sessionmaker)
    login = await _start_login(http, username="other")
    # Not listed for the owner, and the owner can't accept it even with the code.
    assert (await http.get("/api/v1/remote/devices", headers=headers)).json()["items"] == []
    response = await http.post(
        "/api/v1/remote/devices/accept", json={"user_code": login["user_code"]}, headers=headers
    )
    assert response.status_code == 404
    listed = (await http.get("/api/v1/remote/devices", headers=other)).json()["items"]
    assert [d["user_code"] for d in listed] == [login["user_code"]]
    # An unknown name isn't revealed: the request starts normally and lists for nobody.
    unknown = await http.post("/api/v1/remote/device/login", json={**DEVICE, "username": "nobody"})
    assert unknown.status_code == 201


async def test_sign_in_requests_are_rate_limited(http):
    for _ in range(10):
        await _start_login(http)
    response = await http.post("/api/v1/remote/device/login", json=DEVICE)
    assert response.status_code == 429


async def test_machines_are_private(http, headers, sessionmaker):
    machine = await register_machine(http, headers)
    other = await _other_user_headers(http, sessionmaker)
    assert (await http.get("/api/v1/remote/agents", headers=other)).json()["items"] == []
    for method, path in [
        ("GET", f"/api/v1/remote/agents/{machine['agent_id']}"),
        ("POST", f"/api/v1/remote/agents/{machine['agent_id']}/revoke"),
        ("DELETE", f"/api/v1/remote/agents/{machine['agent_id']}"),
    ]:
        response = await http.request(method, path, headers=other)
        assert response.status_code == 404, (method, path)
    # Machine endpoints never accept a user session.
    response = await http.post("/api/v1/remote/credentials/rotate", headers=headers)
    assert response.status_code == 401
    response = await http.get("/api/v1/remote/device/chats", headers=headers)
    assert response.status_code == 401


def test_access_tokens_are_signed_and_expire(settings):
    agent_id = uuid.uuid4()
    token, expires = identity.issue_token(agent_id, settings)
    assert identity.verify_token(token, settings).agent_id == agent_id
    assert expires > time.time()
    body, _, signature = token.partition(".")
    assert identity.verify_token(body + "." + signature[:-2] + "AA", settings) is None
    assert identity.verify_token(token + "x", settings) is None
    assert identity.verify_token("garbage", settings) is None
    other_key = settings.model_copy(
        update={"app_secret_key": SecretStr("another-secret-key-of-enough-length")}
    )
    assert identity.verify_token(token, other_key) is None


async def test_credential_rotation_with_grace(http, headers):
    machine = await register_machine(http, headers)
    response = await http.post(
        "/api/v1/remote/credentials/rotate",
        headers={"Authorization": f"Bearer {machine['token']}"},
    )
    assert response.status_code == 200
    new = response.json()["credential"]
    # The old credential still works during the grace period...
    old = await http.post("/api/v1/remote/token", json={"credential": machine["credential"]})
    assert old.status_code == 200
    # ...until the new one is used.
    assert (await http.post("/api/v1/remote/token", json={"credential": new})).status_code == 200
    old = await http.post("/api/v1/remote/token", json={"credential": machine["credential"]})
    assert old.status_code == 401


# ---------------------------------------------------------------------- connection


async def test_connection_requires_a_valid_token(app, http, headers):
    with pytest.raises(WSClosed) as closed:
        async with ws(app, None):
            pass
    assert closed.value.code == 4401
    with pytest.raises(WSClosed):
        async with ws(app, "crt_forged.token"):
            pass


async def test_hello_heartbeat_and_state(app, http, headers, sessionmaker):
    machine = await register_machine(http, headers)
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        welcome = await sock.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["agent_id"] == machine["agent_id"]
        await sock.send_json({"type": "heartbeat", "resources": {"cpu_percent": 42.0}})
        await sock.send_json({"type": "ping"})
        assert (await sock.receive_until("pong"))["type"] == "pong"
        detail = (
            await http.get(f"/api/v1/remote/agents/{machine['agent_id']}", headers=headers)
        ).json()
        assert detail["online"] is True
        assert detail["state"] == "idle"
        assert detail["resources"] == {"cpu_percent": 42.0}
        assert detail["local_policy"]["allow_shell"] is True
        assert detail["permissions"]["allow_shell"] is True
        await sock.send_json({"type": "bye"})
        await asyncio.sleep(0.1)
    row = await machine_row(sessionmaker, machine["agent_id"])
    assert row.connection_id is None
    assert row.disconnect_reason == "clean"
    detail = (
        await http.get(f"/api/v1/remote/agents/{machine['agent_id']}", headers=headers)
    ).json()
    assert detail["state"] == "offline"


async def test_dropped_connection_shows_as_disconnected(app, http, headers):
    machine = await register_machine(http, headers)
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        await sock.receive_json()
    # No "bye": the connection was lost.
    detail = (
        await http.get(f"/api/v1/remote/agents/{machine['agent_id']}", headers=headers)
    ).json()
    assert detail["state"] == "disconnected"
    assert detail["disconnect_reason"] == "lost"


async def test_newest_connection_wins(app, http, headers, sessionmaker):
    machine = await register_machine(http, headers)
    first = ws(app, machine["token"])
    await first.__aenter__()
    await first.send_json(hello())
    await first.receive_json()
    async with ws(app, machine["token"]) as second:
        await second.send_json(hello())
        await second.receive_json()
        with pytest.raises(WSClosed) as closed:
            await first.receive_until("never", wait=3)
        assert closed.value.code == 4409
        row = await machine_row(sessionmaker, machine["agent_id"])
        assert row.connection_id is not None  # the second connection is still current
    await first.disconnect()


async def test_revocation_closes_the_connection(app, http, headers):
    machine = await register_machine(http, headers)
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        await sock.receive_json()
        response = await http.post(
            f"/api/v1/remote/agents/{machine['agent_id']}/revoke", headers=headers
        )
        assert response.json()["state"] == "revoked"
        with pytest.raises(WSClosed) as closed:
            await sock.receive_until("never", wait=3)
        assert closed.value.code == 4401
    # Credential and existing token are dead.
    assert (
        await http.post("/api/v1/remote/token", json={"credential": machine["credential"]})
    ).status_code == 401
    with pytest.raises(WSClosed):
        async with ws(app, machine["token"]):
            pass


# ---------------------------------------------------------------------- sessions + events


async def _open_session(http, headers, machine, directory="/srv/work/project") -> dict:
    response = await http.post(
        "/api/v1/remote/sessions",
        json={
            "agent_id": machine["agent_id"],
            "title": "Build authentication",
            "working_directory": directory,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_session_open_is_delivered_and_events_are_stored_once(
    app, http, headers, sessionmaker
):
    machine = await register_machine(http, headers)
    session = await _open_session(http, headers, machine)
    assert session["status"] == "opening"
    assert session["origin"] == "portal"
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        await sock.receive_json()
        command = await sock.receive_until("command")
        assert command["kind"] == "session.open"
        assert command["session_id"] == session["id"]
        assert command["payload"]["working_directory"] == "/srv/work/project"
        assert command["payload"]["next_seq"] == 1
        assert command["payload"]["permissions"]["allow_shell"] is True

        events = [
            {"seq": 1, "type": "session.opened", "data": {"command_id": command["id"]}},
            {"seq": 2, "type": "terminal.output", "data": {"stream": "stdout", "text": "hi\n"}},
        ]
        await sock.send_json({"type": "events", "session_id": session["id"], "events": events})
        ack = await sock.receive_until("ack")
        assert ack["seq"] == 2
        # A resend after a reconnect is acknowledged but not stored again.
        await sock.send_json({"type": "events", "session_id": session["id"], "events": events})
        assert (await sock.receive_until("ack"))["seq"] == 2
        # A gap is held back: only the contiguous prefix is stored.
        await sock.send_json(
            {
                "type": "events",
                "session_id": session["id"],
                "events": [{"seq": 4, "type": "warning", "data": {}}],
            }
        )
        assert (await sock.receive_until("ack"))["seq"] == 2
        await sock.send_json(
            {
                "type": "events",
                "session_id": session["id"],
                "events": [
                    {"seq": 4, "type": "warning", "data": {}},
                    {"seq": 3, "type": "warning", "data": {"n": 3}},
                ],
            }
        )
        assert (await sock.receive_until("ack"))["seq"] == 4

    async with sessionmaker() as db:
        rows = (await db.scalars(select(RemoteSessionEvent).order_by(RemoteSessionEvent.seq))).all()
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    detail = (await http.get(f"/api/v1/remote/sessions/{session['id']}", headers=headers)).json()
    assert detail["status"] == "idle"
    assert detail["last_seq"] == 4
    page = (
        await http.get(f"/api/v1/remote/sessions/{session['id']}/events?after=1", headers=headers)
    ).json()
    assert [e["seq"] for e in page["items"]] == [2, 3, 4]
    assert page["items"][0]["data"]["text"] == "hi\n"


async def test_commands_are_redelivered_after_reconnect(app, http, headers, sessionmaker):
    machine = await register_machine(http, headers)
    session = await _open_session(http, headers, machine)
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        welcome = await sock.receive_json()
        assert welcome["sessions"][session["id"]]["status"] == "opening"
        first = await sock.receive_until("command")
    # Delivered, never answered: the connection dropped. The next connection gets it again.
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        await sock.receive_json()
        again = await sock.receive_until("command")
        assert again["id"] == first["id"]


async def test_agent_cannot_report_for_another_machines_session(app, http, headers):
    mine = await register_machine(http, headers, name="One")
    other = await register_machine(http, headers, name="Two")
    session = await _open_session(http, headers, mine)
    async with ws(app, other["token"]) as sock:
        await sock.send_json(hello())
        await sock.receive_json()
        await sock.send_json(
            {
                "type": "events",
                "session_id": session["id"],
                "events": [{"seq": 1, "type": "session.failed", "data": {"reason": "x"}}],
            }
        )
        ack = await sock.receive_until("ack")
        assert ack["error"] == "unknown_session"
    detail = (await http.get(f"/api/v1/remote/sessions/{session['id']}", headers=headers)).json()
    assert detail["status"] == "opening"


async def test_session_needs_an_active_machine_and_absolute_directory(http, headers):
    revoked = await register_machine(http, headers, name="Old")
    await http.post(f"/api/v1/remote/agents/{revoked['agent_id']}/revoke", headers=headers)
    response = await http.post(
        "/api/v1/remote/sessions",
        json={"agent_id": revoked["agent_id"], "working_directory": "/srv"},
        headers=headers,
    )
    assert response.json()["error"]["code"] == "agent_not_active"
    machine = await register_machine(http, headers, name="Win", os="windows")
    response = await http.post(
        "/api/v1/remote/sessions",
        json={"agent_id": machine["agent_id"], "working_directory": "relative\\dir"},
        headers=headers,
    )
    assert response.json()["error"]["code"] == "invalid_directory"
    ok = await http.post(
        "/api/v1/remote/sessions",
        json={"agent_id": machine["agent_id"], "working_directory": "C:\\Users\\me\\code"},
        headers=headers,
    )
    assert ok.status_code == 201


async def test_terminate_and_revoke_end_sessions(app, http, headers, sessionmaker):
    machine = await register_machine(http, headers)
    session = await _open_session(http, headers, machine)
    response = await http.post(
        f"/api/v1/remote/sessions/{session['id']}/terminate", headers=headers
    )
    assert response.json()["status"] == "terminated"
    async with sessionmaker() as db:
        kinds = {(c.kind, c.status) for c in (await db.scalars(select(RemoteCommand))).all()}
    # The unsent open is cancelled; a close is queued for the machine.
    assert ("session.open", "cancelled") in kinds
    assert ("session.close", "pending") in kinds

    second = await _open_session(http, headers, machine, "/srv/work/other")
    await http.post(f"/api/v1/remote/agents/{machine['agent_id']}/revoke", headers=headers)
    detail = (await http.get(f"/api/v1/remote/sessions/{second['id']}", headers=headers)).json()
    assert detail["status"] == "terminated"
    async with sessionmaker() as db:
        open_commands = (
            await db.scalars(select(RemoteCommand).where(RemoteCommand.status == "pending"))
        ).all()
    assert open_commands == []


async def test_effective_permissions_are_the_intersection(app, http, headers):
    machine = await register_machine(http, headers)
    async with ws(app, machine["token"]) as sock:
        # The machine's own policy forbids the shell.
        await sock.send_json(hello(local_policy={"roots": ["/srv"], "allow_write": True}))
        await sock.receive_json()
    detail = (
        await http.get(f"/api/v1/remote/agents/{machine['agent_id']}", headers=headers)
    ).json()
    assert detail["permissions"]["allow_shell"] is False  # Central can't widen it
    assert detail["permissions"]["approval"] == "always"
    response = await http.patch(
        f"/api/v1/remote/agents/{machine['agent_id']}",
        json={"name": "Build box", "config": {"allow_write": False, "approval": "profile"}},
        headers=headers,
    )
    body = response.json()
    assert body["name"] == "Build box"
    assert body["permissions"]["allow_write"] is False
    assert body["permissions"]["approval"] == "profile"


async def test_remote_can_be_turned_off(http, headers, remote_settings):
    remote_settings.remote_agents_enabled = False
    response = await http.get("/api/v1/remote/agents", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "remote_disabled"


async def test_retention_prunes_old_output_commands_and_sign_ins(
    app, http, headers, sessionmaker, remote_settings
):
    from datetime import timedelta

    from app.models import RemoteSession
    from app.services.remote import maintenance

    machine = await register_machine(http, headers)
    session = await _open_session(http, headers, machine)
    async with ws(app, machine["token"]) as sock:
        await sock.send_json(hello())
        await sock.receive_json()
        await sock.receive_until("command")
        await sock.send_json(
            {
                "type": "events",
                "session_id": session["id"],
                "events": [
                    {"seq": 1, "type": "session.opened", "data": {}},
                    {"seq": 2, "type": "terminal.output", "data": {"text": "old"}},
                    {"seq": 3, "type": "file.changed", "data": {"path": "a"}},
                ],
            }
        )
        await sock.receive_until("ack")
    old = utcnow() - timedelta(days=remote_settings.remote_output_retention_days + 1)
    async with sessionmaker() as db:
        for event in (await db.scalars(select(RemoteSessionEvent))).all():
            event.created_at = old
        for command in (await db.scalars(select(RemoteCommand))).all():
            command.status = "completed"
            command.updated_at = utcnow() - timedelta(days=31)
        for request in (await db.scalars(select(RemoteDeviceRequest))).all():
            request.expires_at = utcnow() - timedelta(days=2)
        await db.commit()
    removed = await maintenance.prune(sessionmaker, remote_settings)
    assert removed == {"terminal_output": 1, "commands": 1, "device_requests": 1}
    async with sessionmaker() as db:
        kept = [e.type for e in (await db.scalars(select(RemoteSessionEvent))).all()]
        assert sorted(kept) == ["file.changed", "session.opened"]
        assert await db.get(RemoteSession, uuid.UUID(session["id"])) is not None


async def test_the_machines_policy_is_changed_from_celius(app, http, headers, sessionmaker):
    machine = await register_machine(http, headers)
    url = f"{agent_url(machine)}/policy"
    change = {"roots": [{"path": "/srv/new", "write": False}], "allow_shell": True}
    assert (await http.put(url, json=change, headers=headers)).json()["error"]["code"] == (
        "agent_outdated"  # never connected: nothing says it can be edited
    )
    async with ws(app, machine["token"]) as sock:
        policy = {**hello()["local_policy"], "allow_remote_edit": True}
        await sock.send_json(hello(local_policy=policy))
        await sock.receive_until("welcome")

        pending = asyncio.create_task(http.put(url, json=change, headers=headers))
        command = await sock.receive_until("command")
        assert command["kind"] == "policy.update" and command["session_id"] is None
        assert command["payload"] == {"policy": change}
        saved = {**policy, "roots": [{"path": "/srv/new", "write": False}], "allow_shell": True}
        await sock.send_json(
            {
                "type": "result",
                "command_id": command["id"],
                "status": "completed",
                "result": {"local_policy": saved},
            }
        )
        response = await pending
        assert response.status_code == 200, response.text
        assert response.json()["local_policy"]["roots"] == [{"path": "/srv/new", "write": False}]

        # The machine refuses what it can't do; the reason reaches the person.
        pending = asyncio.create_task(
            http.put(url, json={"roots": [{"path": "nope"}]}, headers=headers)
        )
        command = await sock.receive_until("command")
        await sock.send_json(
            {
                "type": "result",
                "command_id": command["id"],
                "status": "failed",
                "error": "nope is not a full path on this machine.",
            }
        )
        refused = await pending
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "policy_refused"
        assert refused.json()["error"]["message"] == "nope is not a full path on this machine."

        # Locked on the machine: refused without asking it.
        await sock.send_json(
            {"type": "policy", "local_policy": {**saved, "allow_remote_edit": False}}
        )
        await asyncio.sleep(0.3)
        locked = await http.put(url, json=change, headers=headers)
        assert locked.status_code == 409 and locked.json()["error"]["code"] == "policy_locked"
        await sock.send_json({"type": "bye"})
        await asyncio.sleep(0.1)

    offline = await http.put(url, json=change, headers=headers)
    assert offline.json()["error"]["code"] == "policy_locked"  # still what it last said
    async with sessionmaker() as db:
        actions = (
            await db.scalars(
                select(AuditEvent.action).where(AuditEvent.action == "remote_agent.policy_changed")
            )
        ).all()
    assert actions == ["remote_agent.policy_changed"]  # the refused change isn't recorded
    # Another person can't change it.
    other = await _other_user_headers(http, sessionmaker)
    assert (await http.put(url, json=change, headers=other)).status_code == 404

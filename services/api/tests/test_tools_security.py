"""Path containment, SSRF guard, MCP registration and scheduled tasks."""

import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.models import McpServer, Run, ScheduledTask, User
from app.services import schedules
from app.services.secrets import seal, unseal
from app.services.tools import workspace as ws
from app.services.tools.builtin import fetch, html_to_text
from app.services.tools.mcp import McpClient, McpError, tool_name
from app.services.tools.netguard import (
    BlockedDestination,
    GuardedTransport,
    check_url,
    is_public,
    resolve_checked,
)
from app.services.tools.registry import BUILTIN, ToolContext
from tests.fakes import FakeLlama, drain_runs
from tests.tools_helpers import sandbox_client


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


# ---------------------------------------------------------------------- workspace


def test_workspace_paths_stay_inside_and_never_follow_symlinks(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path)

    for bad in ("../outside.txt", "/etc/passwd", "a/../../x", "C:/x", "x\x00y"):
        with pytest.raises(ws.WorkspaceError):
            ws.read_bytes(root, bad, 100)
    with pytest.raises(ws.WorkspaceError):
        ws.read_bytes(root, "link", 100)
    with pytest.raises(ws.WorkspaceError):
        ws.read_bytes(root, "dirlink/outside.txt", 100)
    with pytest.raises(ws.WorkspaceError):
        ws.write_bytes(root, "link", b"overwrite")
    with pytest.raises(ws.WorkspaceError):
        ws.write_bytes(root, "dirlink/new.txt", b"x")
    assert outside.read_text() == "secret"
    assert not (tmp_path / "new.txt").exists()

    ws.write_bytes(root, "sub/dir/file.txt", b"ok")
    assert ws.read_bytes(root, "sub/dir/file.txt", 100) == (b"ok", 2)
    with pytest.raises(ws.WorkspaceError):
        ws.copy_out(root, "link", tmp_path / "copy", 1000)
    assert not ws.copy_in(root, "link", outside)  # never writes through a link


# ---------------------------------------------------------------------- SSRF


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.0.204",
        "172.16.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ],
)
def test_private_and_reserved_addresses_are_not_public(address):
    assert not is_public(address)


def test_public_addresses_are_public():
    assert is_public("1.1.1.1") and is_public("2606:4700:4700::1111")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "http://example.com:8080/",
        "http://u:p@x.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http:///x",
        "gopher://example.com",
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(BlockedDestination):
        check_url(url)


def test_allowlist():
    assert check_url("https://docs.python.org/3/", ["python.org"]) == "docs.python.org"
    with pytest.raises(BlockedDestination):
        check_url("https://evilpython.org/", ["python.org"])


async def test_resolution_to_private_addresses_is_blocked():
    async def rebinding(host, port):
        return ["93.184.216.34", "127.0.0.1"]  # one public, one private

    with pytest.raises(BlockedDestination):
        await resolve_checked("example.com", 443, resolver=rebinding)


async def test_guarded_transport_refuses_to_connect_to_private_addresses():
    async def to_localhost(host, port):
        return ["127.0.0.1"]

    async with httpx.AsyncClient(transport=GuardedTransport(resolver=to_localhost)) as client:
        with pytest.raises(BlockedDestination):
            await client.get("http://innocent.example/")


def _ctx(settings, tmp_path, transport) -> ToolContext:
    return ToolContext(
        settings=settings,
        factory=None,
        owner_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        workspace=tmp_path,
        web_transport=transport,
        time_limit=10,
    )


async def test_redirects_to_private_addresses_are_blocked(settings, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        return httpx.Response(200, text="metadata")

    ctx = _ctx(settings, tmp_path, httpx.MockTransport(handler))
    with pytest.raises(BlockedDestination):
        await fetch(ctx, "https://example.com/", transport=ctx.web_transport)


async def test_web_fetch_returns_readable_text_and_caps_size(settings, tmp_path):
    page = "<html><head><title>T</title><script>x()</script></head><body><p>Hello</p></body>"

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (settings.tool_web_max_bytes + 10))
        return httpx.Response(200, html=page)

    ctx = _ctx(settings, tmp_path, httpx.MockTransport(handler))
    spec = BUILTIN["web_fetch"]
    out = await spec.run(ctx, {"url": "https://example.com/"})
    assert "Title: T" in out.content and "Hello" in out.content and "x()" not in out.content
    big = await spec.run(ctx, {"url": "https://example.com/big"})
    assert big.is_error and "larger" in big.content
    saved = await spec.run(ctx, {"url": "https://example.com/", "save_as": "page.html"})
    assert not saved.is_error and (tmp_path / "page.html").read_text() == page


def test_html_to_text():
    title, text = html_to_text("<title>A &amp; B</title><style>p{}</style><p>one</p><p>two</p>")
    assert (title, text) == ("A & B", "one\n\ntwo")


# ---------------------------------------------------------------------- MCP


def mcp_server(tools=None, *, require_token=None):
    """A minimal streamable-HTTP MCP server for httpx.MockTransport."""
    state = {"calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if require_token and request.headers.get("authorization") != require_token:
            return httpx.Response(401)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                headers={"mcp-session-id": "s1"},
            )
        if method == "tools/list":
            result = {
                "tools": tools
                or [
                    {
                        "name": "lookup.weather",
                        "description": "Weather",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ]
            }
            # Answer as an SSE stream, as many servers do.
            data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return httpx.Response(
                200,
                text=f"event: message\ndata: {data}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        if method == "tools/call":
            state["calls"].append(body["params"])
            assert request.headers.get("mcp-session-id") == "s1"
            result = {
                "content": [
                    {"type": "text", "text": f"Sunny in {body['params']['arguments']['city']}"}
                ]
            }
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"message": "nope"}}
        )

    return handler, state


async def test_mcp_client_lists_and_calls_tools():
    handler, state = mcp_server(require_token="Bearer t0k")
    transport = httpx.MockTransport(handler)
    async with McpClient(
        "https://mcp.example/mcp", {"Authorization": "Bearer t0k"}, transport=transport
    ) as client:
        tools = await client.list_tools()
        assert tools[0]["name"] == "lookup.weather"
        text, is_error = await client.call_tool("lookup.weather", {"city": "Oslo"})
    assert text == "Sunny in Oslo" and not is_error
    with pytest.raises(McpError, match="credentials"):
        async with McpClient("https://mcp.example/mcp", transport=transport):
            pass
    with pytest.raises(McpError):
        async with McpClient("http://127.0.0.1/mcp", transport=transport):
            pass


def test_mcp_tool_names_are_namespaced_and_safe():
    assert tool_name("home", "lookup.weather") == "mcp__home__lookup_weather"
    assert len(tool_name("x" * 40, "y" * 100)) == 64


async def test_mcp_tools_run_through_the_policy(
    client, auth_headers, sessionmaker, gateway, settings, fake, user
):
    handler, state = mcp_server()
    async with sessionmaker() as db:
        db.add(
            McpServer(
                owner_id=user.id,
                name="home",
                url="https://mcp.example/mcp",
                tools=[
                    {
                        "name": "lookup.weather",
                        "description": "Weather",
                        "input_schema": {"type": "object"},
                    }
                ],
            )
        )
        await db.commit()
    fake.turns = [[{"name": "mcp__home__lookup_weather", "arguments": {"city": "Oslo"}}], "ok"]
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "standard"}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "m", "content": "weather?"},
        headers=auth_headers,
    )
    overrides = {"web_transport": httpx.MockTransport(handler)}
    await drain_runs(sessionmaker, gateway, settings, tool_overrides=overrides)
    pending = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    assert pending[0]["tool_name"] == "mcp__home__lookup_weather"
    assert pending[0]["risk"] == "external"
    assert state["calls"] == []  # nothing reached the MCP server before approval
    await client.post(
        f"/api/v1/tool-calls/{pending[0]['id']}/approve",
        json={"args_hash": pending[0]["args_hash"]},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings, tool_overrides=overrides)
    assert state["calls"] == [{"name": "lookup.weather", "arguments": {"city": "Oslo"}}]
    assert fake.requests[-1]["messages"][-1]["content"] == "Sunny in Oslo"


async def test_only_admins_register_mcp_servers_and_credentials_stay_sealed(
    client, auth_headers, sessionmaker, settings, user
):
    body = {"name": "lan", "url": "http://127.0.0.1:9/mcp", "headers": {"Authorization": "x"}}
    created = await client.post("/api/v1/mcp-servers", json=body, headers=auth_headers)
    assert created.status_code == 201, created.text
    out = created.json()
    assert out["has_credentials"] and "headers" not in out
    assert "private" in out["last_error"]  # SSRF guard: not allowed to reach loopback
    async with sessionmaker() as db:
        row = await db.scalar(select(McpServer))
        assert "Authorization" not in row.credentials
        assert unseal(settings, row.credentials, "mcp") == {"Authorization": "x"}
        (await db.get(User, user.id)).is_admin = False
        await db.commit()
    forbidden = await client.get("/api/v1/mcp-servers", headers=auth_headers)
    assert forbidden.status_code == 403


def test_sealed_values_need_the_same_secret(settings):
    token = seal(settings, {"a": 1})
    assert unseal(settings, token) == {"a": 1}
    other = settings.model_copy(
        update={"app_secret_key": settings.app_secret_key.__class__("x" * 40)}
    )
    assert unseal(other, token) is None


# ---------------------------------------------------------------------- schedules


def test_next_occurrence_respects_time_zone_and_weekdays():
    after = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)  # a Monday
    daily = ScheduledTask(kind="daily", time_of_day="09:00", timezone="Europe/Oslo", weekdays=[])
    assert schedules.next_occurrence(daily, after) == datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    weekly = ScheduledTask(
        kind="weekly", time_of_day="18:30", timezone="America/New_York", weekdays=[4]
    )
    assert schedules.next_occurrence(weekly, after) == datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
    every = ScheduledTask(kind="interval", interval_minutes=30)
    assert (schedules.next_occurrence(every, after) - after).total_seconds() == 1800


async def test_schedule_validation_and_quota(client, auth_headers, settings):
    base = {"name": "n", "prompt": "p", "kind": "interval", "interval_minutes": 5}
    too_often = await client.post("/api/v1/schedules", json=base, headers=auth_headers)
    assert too_often.status_code == 422
    bad_zone = await client.post(
        "/api/v1/schedules",
        headers=auth_headers,
        json={**base, "kind": "daily", "time_of_day": "08:00", "timezone": "Mars/Base"},
    )
    assert bad_zone.json()["error"]["code"] == "invalid_timezone"
    settings.schedule_max_per_user = 1
    ok = await client.post(
        "/api/v1/schedules", json={**base, "interval_minutes": 60}, headers=auth_headers
    )
    assert ok.status_code == 201 and ok.json()["next_run_at"]
    full = await client.post(
        "/api/v1/schedules", json={**base, "interval_minutes": 60}, headers=auth_headers
    )
    assert full.json()["error"]["code"] == "quota_exceeded"


async def test_due_schedule_fires_once_into_its_own_conversation(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    created = (
        await client.post(
            "/api/v1/schedules",
            headers=auth_headers,
            json={
                "name": "Digest",
                "prompt": "Summarise",
                "kind": "interval",
                "interval_minutes": 60,
                "tool_profile": "read_only",
            },
        )
    ).json()
    async with sessionmaker() as db:
        task = await db.get(ScheduledTask, uuid.UUID(created["id"]))
        task.next_run_at = utcnow()
        await db.commit()
    assert await schedules.fire_due(sessionmaker, settings) == 1
    assert await schedules.fire_due(sessionmaker, settings) == 0  # claimed: not again
    await drain_runs(sessionmaker, gateway, settings)
    listed = (await client.get("/api/v1/schedules", headers=auth_headers)).json()[0]
    assert listed["last_status"] == "started" and listed["conversation_id"]
    conv = (
        await client.get(f"/api/v1/conversations/{listed['conversation_id']}", headers=auth_headers)
    ).json()
    assert conv["tool_profile"] == "read_only"
    assert conv["messages"][0]["parts"][0]["text"] == "Summarise"
    assert conv["messages"][1]["status"] == "complete"
    async with sessionmaker() as db:
        run = await db.get(Run, uuid.UUID(listed["last_run_id"]))
        assert run.params["source"] == "schedule"
    activity = (await client.get("/api/v1/activity", headers=auth_headers)).json()["items"]
    assert activity[0]["source"] == "schedule"


# ---------------------------------------------------------------------- sandbox-only tools


def _system_python_has(*modules: str) -> bool:
    """Whether the python3 the sandbox runs (from its fixed PATH) has these modules."""
    python = shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")
    code = "import " + ",".join(modules)
    return (
        bool(python)
        and subprocess.run([python, "-I", "-c", code], capture_output=True).returncode == 0
    )


async def test_git_tool_works_offline_and_refuses_network_commands(settings, tmp_path):
    async with sandbox_client(settings.workspace_root, tmp_path / "s") as sandbox:
        ctx = _ctx(settings, Path(settings.workspace_root) / "c1", None)
        ctx.conversation_id = "c1"
        ctx.sandbox = sandbox
        os.makedirs(ctx.workspace, exist_ok=True)
        git = BUILTIN["git"]
        assert not (await git.run(ctx, {"args": ["init", "-q", "repo"]})).is_error
        refused = await git.run(ctx, {"args": ["push", "origin"], "repository": "repo"})
        assert refused.is_error and "network" in refused.content
        status = await git.run(ctx, {"args": ["status", "--short"], "repository": "repo"})
        assert not status.is_error


@pytest.mark.skipif(not _system_python_has("docx", "openpyxl"), reason="sandbox libraries absent")
async def test_make_document_creates_office_files(settings, tmp_path):
    async with sandbox_client(settings.workspace_root, tmp_path / "s") as sandbox:
        ctx = _ctx(settings, Path(settings.workspace_root) / "c2", None)
        ctx.conversation_id = "c2"
        ctx.sandbox = sandbox
        os.makedirs(ctx.workspace, exist_ok=True)
        tool = BUILTIN["make_document"]
        out = await tool.run(
            ctx,
            {
                "format": "docx",
                "path": "r.docx",
                "title": "Report",
                "markdown": "# Intro\n- one\ntext",
            },
        )
        assert not out.is_error, out.content
        out = await tool.run(
            ctx, {"format": "xlsx", "path": "t.xlsx", "sheets": [{"name": "S", "rows": [["a", 1]]}]}
        )
        assert not out.is_error, out.content
        assert (ctx.workspace / "r.docx").read_bytes()[:2] == b"PK"

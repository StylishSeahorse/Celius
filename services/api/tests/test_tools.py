"""Phase 5 exit gate: policy enforcement, approvals, loop limits, cancellation, audit."""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db.base import utcnow
from app.models import AuditEvent, Run, RunStatus, StoredFile, ToolCall, ToolCallStatus
from app.services.tools.calls import expire_approvals
from app.services.tools.policy import BUILTIN_PROFILES, Profile, args_hash, decide
from app.worker.executor import LIMIT_NOTE
from tests.fakes import FakeLlama, drain_runs
from tests.tools_helpers import sandbox_client


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


@pytest.fixture
async def sandbox(settings, tmp_path):
    async with sandbox_client(settings.workspace_root, tmp_path / "sandbox-scratch") as client:
        yield client


async def start(client, headers, text="Do it", **conversation) -> tuple[dict, dict]:
    created = await client.post("/api/v1/conversations", json=conversation, headers=headers)
    assert created.status_code == 201, created.text
    conv = created.json()
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    return conv, sent.json()


async def detail(client, headers, conversation_id) -> dict:
    response = await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


async def run_status(sessionmaker, run_id) -> RunStatus:
    async with sessionmaker() as db:
        return (await db.get(Run, uuid.UUID(str(run_id)))).status


def tool_names(request: dict) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools") or []}


# ---------------------------------------------------------------------- policy


def test_profiles_decide_by_risk_and_grants_never_override_deny():
    standard = Profile("standard", "Standard", "standard")
    assert decide(standard, "read_file", "read", set()).decision == "allow"
    assert decide(standard, "run_python", "execute", set()).decision == "ask"
    assert decide(standard, "run_python", "execute", {"run_python"}).decision == "allow"
    read_only = Profile("read_only", "Read only", "read_only")
    assert decide(read_only, "run_python", "execute", {"run_python"}).decision == "deny"
    custom = Profile("custom:x", "Mine", "developer", {"run_shell": "deny", "web_fetch": "allow"})
    assert decide(custom, "run_shell", "execute", set()).decision == "deny"
    assert decide(custom, "run_python", "execute", set()).decision == "allow"
    assert decide(custom, "web_fetch", "network", set()).decision == "allow"
    assert set(BUILTIN_PROFILES) == {"read_only", "standard", "developer", "administrator"}


def test_args_hash_is_canonical():
    assert args_hash({"b": 1, "a": [1, 2]}) == args_hash({"a": [1, 2], "b": 1})
    assert args_hash({"a": 1}) != args_hash({"a": 2})


# ---------------------------------------------------------------------- the loop


async def test_allowed_read_tool_runs_and_result_reaches_the_model(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [[{"name": "list_files", "arguments": {}}], "There are no files."]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)

    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    first, second = fake.requests
    assert "list_files" in tool_names(first)
    assert "run_python" not in tool_names(first)  # no sandbox configured: not offered
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool" and "empty" in tool_message["content"]
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "list_files"

    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert reply["status"] == "complete"
    assert [c["status"] for c in reply["tool_calls"]] == ["completed"]
    assert [p["type"] for p in reply["parts"]] == ["tool_call", "text"]
    assert reply["parts"][-1]["text"] == "There are no files."


async def test_write_needs_approval_bound_to_exact_arguments(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        [{"name": "write_file", "arguments": {"path": "notes.txt", "content": "hello"}}],
        "Saved.",
    ]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.awaiting_approval
    assert len(fake.requests) == 1

    pending = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    assert len(pending) == 1
    call = pending[0]
    assert call["arguments"] == {"path": "notes.txt", "content": "hello", "overwrite": True}
    assert call["conversation_title"] == "Do it"

    # Approving anything but the exact arguments fails.
    wrong = args_hash({"path": "notes.txt", "content": "evil"})
    refused = await client.post(
        f"/api/v1/tool-calls/{call['id']}/approve",
        json={"args_hash": wrong},
        headers=auth_headers,
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "arguments_changed"
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.awaiting_approval

    approved = await client.post(
        f"/api/v1/tool-calls/{call['id']}/approve",
        json={"args_hash": call["args_hash"]},
        headers=auth_headers,
    )
    assert approved.status_code == 200, approved.text
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.queued
    again = await client.post(
        f"/api/v1/tool-calls/{call['id']}/approve",
        json={"args_hash": call["args_hash"]},
        headers=auth_headers,
    )
    assert again.status_code == 409  # decided once only

    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    workspace = settings.workspace_root + "/" + conv["id"]
    with open(f"{workspace}/notes.txt") as handle:
        assert handle.read() == "hello"
    # The resumed request replays the step and its result.
    resumed = fake.requests[-1]["messages"]
    assert resumed[-2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert resumed[-1] == {
        "role": "tool",
        "tool_call_id": resumed[-2]["tool_calls"][0]["id"],
        "content": "Wrote 5 bytes to notes.txt",
    }

    async with sessionmaker() as db:
        actions = (await db.scalars(select(AuditEvent.action))).all()
    assert "tool_call.approve_mismatch" in actions
    assert "tool_call.approved" in actions


async def test_denied_call_is_reported_to_the_model_and_not_run(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [[{"name": "write_file", "arguments": {"path": "x.txt", "content": "x"}}], "OK"]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    call = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()[0]
    denied = await client.post(
        f"/api/v1/tool-calls/{call['id']}/deny", json={"reason": "no"}, headers=auth_headers
    )
    assert denied.json()["status"] == "denied"
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    assert "denied" in fake.requests[-1]["messages"][-1]["content"]
    assert not os.path.exists(f"{settings.workspace_root}/{conv['id']}/x.txt")
    async with sessionmaker() as db:
        assert "tool_call.denied" in (await db.scalars(select(AuditEvent.action))).all()


async def test_allow_for_conversation_skips_later_prompts(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    write = {"name": "write_file", "arguments": {"path": "a.txt", "content": "1"}}
    fake.turns = [[write], "one", [write], "two"]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    call = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()[0]
    await client.post(
        f"/api/v1/tool-calls/{call['id']}/approve",
        json={"args_hash": call["args_hash"], "remember": True},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    grants = await client.get(
        f"/api/v1/conversations/{conv['id']}/tool-grants", headers=auth_headers
    )
    assert grants.json() == ["write_file"]

    second = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "second", "content": "again"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, second.json()["run"]["id"]) == RunStatus.completed
    async with sessionmaker() as db:
        latest = (
            await db.scalars(
                select(ToolCall).where(ToolCall.run_id == uuid.UUID(second.json()["run"]["id"]))
            )
        ).one()
    assert latest.decision_source == "grant"


async def test_profile_denies_and_unknown_or_invalid_calls_are_refused(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        [
            {"name": "write_file", "arguments": {"path": "a", "content": "b"}},
            {"name": "format_disk", "arguments": {}},
            {"name": "read_file", "arguments": "{not json"},
            {"name": "read_file", "arguments": {"max_chars": 5}},
        ],
        "Done",
    ]
    conv, sent = await start(client, auth_headers, tool_profile="read_only")
    await drain_runs(sessionmaker, gateway, settings)
    assert "write_file" not in tool_names(fake.requests[0])  # denied tools aren't offered
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    calls = (await detail(client, auth_headers, conv["id"]))["messages"][-1]["tool_calls"]
    assert [c["status"] for c in calls] == ["refused", "refused", "failed", "failed"]
    assert "Read only" in calls[0]["decision_reason"]
    assert "no tool named" in calls[1]["decision_reason"]
    results = [m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 4 and all(r.startswith("Error:") for r in results)
    assert not os.path.exists(f"{settings.workspace_root}/{conv['id']}/a")


async def test_unanswered_approval_expires_and_run_continues(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [[{"name": "write_file", "arguments": {"path": "a", "content": "b"}}], "OK"]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    async with sessionmaker() as db:
        await db.execute(update(ToolCall).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    assert await expire_approvals(sessionmaker) == 1
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    assert "approved this call in time" in fake.requests[-1]["messages"][-1]["content"]


async def test_cancel_while_awaiting_approval(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [[{"name": "write_file", "arguments": {"path": "a", "content": "b"}}], "OK"]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    cancelled = await client.post(f"/api/v1/runs/{sent['run']['id']}/cancel", headers=auth_headers)
    assert cancelled.json()["status"] == "cancelled"
    async with sessionmaker() as db:
        assert (await db.scalars(select(ToolCall.status))).one() == ToolCallStatus.cancelled
    assert (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json() == []


async def test_iteration_limit_forces_an_answer(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.tool_max_iterations = 2
    fake.turns = [[{"name": "list_files", "arguments": {}}]] * 2 + ["Final."]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    assert len(fake.requests) == 3
    last = fake.requests[-1]
    assert "tools" not in last
    assert LIMIT_NOTE in last["messages"][-1]["content"]


async def test_tool_results_are_capped(client, auth_headers, sessionmaker, gateway, settings, fake):
    settings.tool_max_result_chars = 500
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "developer"}, headers=auth_headers
        )
    ).json()
    root = f"{settings.workspace_root}/{conv['id']}"
    os.makedirs(root)
    with open(f"{root}/big.txt", "w") as handle:
        handle.write("x" * 5000)
    fake.turns = [[{"name": "read_file", "arguments": {"path": "big.txt"}}], "ok"]
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "c", "content": "read"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    content = fake.requests[-1]["messages"][-1]["content"]
    assert len(content) < 600 and "cut" in content


# ---------------------------------------------------------------------- sandbox


async def test_python_in_sandbox_and_attach_result(
    client, auth_headers, sessionmaker, gateway, settings, fake, sandbox
):
    code = "open('out.csv','w').write('a,b\\n1,2\\n')\nprint(6*7)"
    fake.turns = [
        [{"name": "run_python", "arguments": {"code": code}}],
        [{"name": "attach_file", "arguments": {"path": "out.csv"}}],
        "Here it is.",
    ]
    conv, sent = await start(client, auth_headers, tool_profile="developer")
    await drain_runs(sessionmaker, gateway, settings, sandbox=sandbox)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.completed
    python_result = fake.requests[1]["messages"][-1]["content"]
    assert "42" in python_result and "out.csv" in python_result
    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert [c["tool_name"] for c in reply["tool_calls"]] == ["run_python", "attach_file"]
    assert reply["tool_calls"][0]["result"]["exit_code"] == 0
    files = [p for p in reply["parts"] if p["type"] == "file"]
    assert files[0]["data"]["filename"] == "out.csv"
    async with sessionmaker() as db:
        stored = await db.get(StoredFile, uuid.UUID(files[0]["data"]["file_id"]))
        assert stored.size_bytes == 8


async def test_sandbox_code_cannot_read_worker_secrets(
    client, auth_headers, sessionmaker, gateway, settings, fake, sandbox, monkeypatch
):
    monkeypatch.setenv("APP_SECRET_KEY", "worker-secret-value-123")
    fake.turns = [[{"name": "run_shell", "arguments": {"command": "env; echo done"}}], "ok"]
    await start(client, auth_headers, tool_profile="developer")
    await drain_runs(sessionmaker, gateway, settings, sandbox=sandbox)
    output = fake.requests[-1]["messages"][-1]["content"]
    assert "done" in output
    assert "worker-secret-value-123" not in output and "SANDBOX_TOKEN" not in output


async def test_cancelling_a_running_call_kills_the_code(
    client, auth_headers, sessionmaker, gateway, settings, fake, sandbox
):
    code = "import time\ntime.sleep(4)\nopen('finished','w').write('1')"
    fake.turns = [[{"name": "run_python", "arguments": {"code": code}}], "never"]
    conv, sent = await start(client, auth_headers, tool_profile="developer")
    worker = asyncio.create_task(drain_runs(sessionmaker, gateway, settings, sandbox=sandbox))
    async with sessionmaker() as db:
        for _ in range(200):
            call = await db.scalar(select(ToolCall))
            if call is not None and call.status == ToolCallStatus.running:
                break
            await asyncio.sleep(0.05)
            db.expire_all()
    await client.post(f"/api/v1/runs/{sent['run']['id']}/cancel", headers=auth_headers)
    await asyncio.wait_for(worker, timeout=10)
    assert await run_status(sessionmaker, sent["run"]["id"]) == RunStatus.cancelled
    async with sessionmaker() as db:
        assert (await db.scalar(select(ToolCall))).status == ToolCallStatus.cancelled
    await asyncio.sleep(4.5)
    assert not os.path.exists(f"{settings.workspace_root}/{conv['id']}/finished")
    assert len(fake.requests) == 1  # the model was never called again


async def test_interrupted_call_is_not_run_twice(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    """A worker that died mid-call: the call is reported as interrupted, never repeated."""
    fake.turns = [[{"name": "list_files", "arguments": {}}], "after restart"]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    # Rewind: pretend the call was still running when the worker vanished.
    async with sessionmaker() as db:
        await db.execute(update(ToolCall).values(status=ToolCallStatus.running, model_content=None))
        await db.execute(update(Run).values(status=RunStatus.queued))
        await db.commit()
    fake.turns = ["after restart"]
    await drain_runs(sessionmaker, gateway, settings)
    async with sessionmaker() as db:
        assert (await db.scalar(select(ToolCall))).status == ToolCallStatus.interrupted
    assert "interrupted" in fake.requests[-1]["messages"][-1]["content"]


# ---------------------------------------------------------------------- endpoints


async def test_tool_catalogue_settings_and_custom_profiles(client, auth_headers, settings):
    tools = (await client.get("/api/v1/tools", headers=auth_headers)).json()
    by_name = {t["name"]: t for t in tools}
    assert by_name["read_file"]["available"]
    assert not by_name["run_python"]["available"]
    assert by_name["run_python"]["unavailable_reason"] == "The sandbox is not configured"

    created = await client.post(
        "/api/v1/tool-profiles",
        json={"name": "Careful dev", "base": "developer", "rules": {"run_shell": "ask"}},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["decisions"]["run_shell"] == "ask"
    assert profile["decisions"]["run_python"] == "allow"

    updated = await client.put(
        "/api/v1/tools/settings",
        json={"default_profile": profile["key"], "disabled_tools": ["web_fetch"]},
        headers=auth_headers,
    )
    assert updated.json()["default_profile"] == profile["key"]
    bad = await client.put(
        "/api/v1/tools/settings", json={"default_profile": "root"}, headers=auth_headers
    )
    assert bad.status_code == 422
    tools = (await client.get("/api/v1/tools", headers=auth_headers)).json()
    assert {t["name"]: t for t in tools}["web_fetch"][
        "unavailable_reason"
    ] == "Turned off in settings"

    bad_conv = await client.post(
        "/api/v1/conversations", json={"tool_profile": "custom:nope"}, headers=auth_headers
    )
    assert bad_conv.status_code == 422


async def test_workspace_download_is_contained_and_never_inline(client, auth_headers, settings):
    conv = (await client.post("/api/v1/conversations", json={}, headers=auth_headers)).json()
    root = f"{settings.workspace_root}/{conv['id']}"
    os.makedirs(root)
    with open(f"{root}/page.html", "w") as handle:
        handle.write("<script>alert(1)</script>")
    secret = os.path.join(settings.storage_root, "secret.txt")
    os.makedirs(settings.storage_root, exist_ok=True)
    with open(secret, "w") as handle:
        handle.write("secret")
    os.symlink(secret, f"{root}/link.txt")

    url = f"/api/v1/conversations/{conv['id']}/workspace/file"
    ok = await client.get(url, params={"path": "page.html"}, headers=auth_headers)
    assert ok.status_code == 200
    assert ok.headers["content-disposition"].startswith("attachment")
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in ok.headers["content-security-policy"]
    for path in ("link.txt", "../../storage/secret.txt", "/etc/passwd"):
        response = await client.get(url, params={"path": path}, headers=auth_headers)
        assert response.status_code in (404, 422), path
        assert b"secret" not in response.content
    listing = await client.get(
        f"/api/v1/conversations/{conv['id']}/workspace", headers=auth_headers
    )
    assert {e["path"] for e in listing.json()["entries"]} == {"page.html", "link.txt"}


async def test_settings_show_the_sandbox_as_the_worker_last_saw_it(client, auth_headers, settings):
    from app.services.tools.sandbox_status import write_status

    shown = (await client.get("/api/v1/tools/settings", headers=auth_headers)).json()
    assert shown["sandbox"] is None  # not configured
    settings.sandbox_url = "http://sandbox:8100"
    settings.sandbox_token = settings.app_secret_key.__class__("t" * 20)
    write_status(settings, {"status": "ok", "mode": "process"})
    shown = (await client.get("/api/v1/tools/settings", headers=auth_headers)).json()
    assert shown["sandbox"] == {"status": "ok", "mode": "process"}


async def test_allow_all_approves_everything_waiting_and_later_calls(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    a = {"name": "write_file", "arguments": {"path": "a.txt", "content": "a"}}
    b = {"name": "write_file", "arguments": {"path": "b.txt", "content": "b"}}
    fake.turns = [
        [a, b],
        "both",
        [{"name": "write_file", "arguments": {"path": "c.txt", "content": "c"}}],
        "three",
    ]
    conv, sent = await start(client, auth_headers, tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    pending = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    assert len(pending) == 2
    await client.post(
        f"/api/v1/tool-calls/{pending[0]['id']}/approve",
        json={"args_hash": pending[0]["args_hash"], "remember_all": True},
        headers=auth_headers,
    )
    assert (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json() == []
    await drain_runs(sessionmaker, gateway, settings)
    root = f"{settings.workspace_root}/{conv['id']}"
    assert os.path.exists(f"{root}/a.txt") and os.path.exists(f"{root}/b.txt")
    assert (await detail(client, auth_headers, conv["id"]))["tool_grants"] == ["*"]

    second = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "next", "content": "again"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, second.json()["run"]["id"]) == RunStatus.completed
    assert os.path.exists(f"{root}/c.txt")
    # "Ask again" removes it.
    await client.delete(f"/api/v1/conversations/{conv['id']}/tool-grants", headers=auth_headers)
    assert (await detail(client, auth_headers, conv["id"]))["tool_grants"] == []


def test_allow_all_never_overrides_a_deny():
    read_only = Profile("read_only", "Read only", "read_only")
    assert decide(read_only, "write_file", "write", {"*"}).decision == "deny"
    standard = Profile("standard", "Standard", "standard")
    assert decide(standard, "run_python", "execute", {"*"}).source == "grant"


async def test_identical_failing_call_is_not_repeated(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    read = {"name": "read_file", "arguments": {"path": "index.html"}}
    write = {"name": "write_file", "arguments": {"path": "index.html", "content": "<p>"}}
    fake.turns = [[read], [read], [write], [read], "done"]
    conv, sent = await start(client, auth_headers, tool_profile="developer")
    await drain_runs(sessionmaker, gateway, settings)
    calls = (await detail(client, auth_headers, conv["id"]))["messages"][-1]["tool_calls"]
    assert [c["status"] for c in calls] == ["failed", "refused", "completed", "completed"]
    assert "already failed" in calls[1]["decision_reason"]


async def test_model_is_told_which_tools_this_chat_lacks(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await start(client, auth_headers, tool_profile="read_only")
    await drain_runs(sessionmaker, gateway, settings)
    system = fake.requests[0]["messages"][0]["content"]
    assert "Not available in this chat:" in system and "write_file" in system
    assert "Read only" in system

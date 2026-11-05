"""Phase 3 (agent orchestration): task state outside the model, checkpoints, interruption
and recovery, error-recovery hints, the activity API."""

import uuid

import pytest
from sqlalchemy import select

from app.models import AgentTask, AgentTaskEvent
from tests.fakes import FakeLlama, drain_runs


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def new_conversation(client, headers, **body) -> dict:
    response = await client.post("/api/v1/conversations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def say(client, headers, conversation_id, text, sessionmaker, gateway, settings) -> dict:
    sent = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": uuid.uuid4().hex, "content": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    await drain_runs(sessionmaker, gateway, settings)
    return sent.json()


def system_prompt(fake: FakeLlama) -> str:
    return fake.requests[-1]["messages"][0]["content"]


def last_tool_result(fake: FakeLlama) -> str:
    return fake.requests[-1]["messages"][-1]["content"]


async def open_tasks(client, headers, **params) -> list[dict]:
    response = await client.get("/api/v1/agent-tasks", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


PLAN = {
    "name": "plan_start",
    "arguments": {
        "objective": "Add a health endpoint to the API and test it",
        "steps": ["Inspect the app", "Add the endpoint", "Run the tests"],
    },
}


# ---------------------------------------------------------------------- the model's record


async def test_plan_is_recorded_and_given_back_every_run(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [
        [PLAN],
        [
            {
                "name": "plan_update",
                "arguments": {"step": 1, "step_status": "done", "step_note": "FastAPI app"},
            }
        ],
        [{"name": "plan_update", "arguments": {"note": "Routes live in app/api"}}],
        "Step 1 done.",
    ]
    await say(
        client, auth_headers, conv["id"], "Add a health endpoint", sessionmaker, gateway, settings
    )
    assert "first understand it" in system_prompt(fake)  # guidance before a plan exists
    assert "Step 1 is done." in fake.requests[-1]["messages"][-3]["content"]

    tasks = await open_tasks(client, auth_headers, conversation_id=conv["id"])
    assert len(tasks) == 1
    task = tasks[0]
    assert task["objective"] == "Add a health endpoint to the API and test it"
    assert [s["status"] for s in task["plan"]] == ["done", "pending", "pending"]
    assert task["notes"] == ["Routes live in app/api"]
    assert task["runs"] == 1

    await say(client, auth_headers, conv["id"], "go on", sessionmaker, gateway, settings)
    prompt = system_prompt(fake)
    assert "<task_state>" in prompt
    assert "Objective: Add a health endpoint to the API and test it" in prompt
    assert "[x] 1. Inspect the app — FastAPI app" in prompt
    assert "[ ] 2. Add the endpoint" in prompt
    assert "Continue from the first unfinished step." in prompt
    assert (await open_tasks(client, auth_headers))[0]["runs"] == 2


async def test_one_open_task_per_chat_and_replacing_it(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    second = {"name": "plan_start", "arguments": {"objective": "Something else", "steps": ["a"]}}
    fake.turns = [
        [PLAN],
        [second],
        [{**second, "arguments": {**second["arguments"], "replace": True}}],
        "ok",
    ]
    await say(client, auth_headers, conv["id"], "Plan it", sessionmaker, gateway, settings)
    assert "A task is already open" in fake.requests[-2]["messages"][-1]["content"]
    open_now = await open_tasks(client, auth_headers)
    assert [t["objective"] for t in open_now] == ["Something else"]
    every = await open_tasks(client, auth_headers, status="abandoned")
    assert [t["objective"] for t in every] == ["Add a health endpoint to the API and test it"]


async def test_completion_needs_finished_steps_and_reports_verification(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [
        [PLAN],
        [{"name": "plan_update", "arguments": {"status": "completed"}}],
        [
            {"name": "plan_update", "arguments": {"step": 1, "step_status": "done"}},
            {"name": "plan_update", "arguments": {"step": 2, "step_status": "done"}},
            {"name": "plan_update", "arguments": {"step": 3, "step_status": "skipped"}},
        ],
        [
            {
                "name": "plan_update",
                "arguments": {"check": "pytest", "passed": False},
            }
        ],
        [{"name": "plan_update", "arguments": {"status": "completed"}}],
        "Done, but a test fails.",
    ]
    await say(client, auth_headers, conv["id"], "Do it", sessionmaker, gateway, settings)
    messages = fake.requests[-1]["messages"]
    tool_results = [m["content"] for m in messages if m["role"] == "tool"]
    assert any("Steps 1, 2, 3 are not done" in r for r in tool_results)
    assert "WITHOUT a passing verification" in tool_results[-1]
    done = await open_tasks(client, auth_headers, status="completed")
    assert done[0]["verified"] is False
    assert done[0]["verifications"][0]["check"] == "pytest"
    assert await open_tasks(client, auth_headers) == []


async def test_blockers_block_and_clear(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [
        [PLAN],
        [{"name": "plan_update", "arguments": {"blocker": "Need the database password"}}],
        "I need the password.",
    ]
    await say(client, auth_headers, conv["id"], "Do it", sessionmaker, gateway, settings)
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "blocked" and task["blockers"] == ["Need the database password"]
    fake.turns = [[{"name": "plan_update", "arguments": {"clear_blockers": True}}], "Thanks."]
    await say(
        client, auth_headers, conv["id"], "It's in the vault now", sessionmaker, gateway, settings
    )
    assert "It is blocked: ask the user" in fake.requests[-2]["messages"][0]["content"]
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "active" and task["blockers"] == []


# ---------------------------------------------------------------------- the backend's record


async def test_files_and_tool_failures_are_recorded_by_the_backend(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="developer")
    fake.turns = [
        [PLAN],
        [{"name": "write_file", "arguments": {"path": "app/health.py", "content": "ok = True\n"}}],
        [{"name": "read_file", "arguments": {"path": "missing.py"}}],
        "Written.",
    ]
    await say(client, auth_headers, conv["id"], "Write it", sessionmaker, gateway, settings)
    task = (await open_tasks(client, auth_headers))[0]
    assert [(f["path"], f["action"]) for f in task["files"]] == [("app/health.py", "written")]
    assert (task["tool_calls"], task["failed_tool_calls"]) == (2, 1)
    detail = (await client.get(f"/api/v1/agent-tasks/{task['id']}", headers=auth_headers)).json()
    kinds = [e["kind"] for e in detail["events"]]
    assert "tool_failed" in kinds and "checkpoint" in kinds and "created" in kinds


async def test_repeated_failures_get_a_recovery_hint(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [
        [PLAN],
        [{"name": "read_file", "arguments": {"path": "a.py"}}],
        [{"name": "read_file", "arguments": {"path": "b.py"}}],
        "Stuck.",
    ]
    await say(client, auth_headers, conv["id"], "Read them", sessionmaker, gateway, settings)
    result = last_tool_result(fake)
    assert "2 tool calls in a row have failed" in result
    assert "record a blocker with plan_update" in result
    first = fake.requests[-2]["messages"][-1]["content"]
    assert "in a row" not in first


# ---------------------------------------------------------------------- interruption


async def test_running_out_of_tool_steps_pauses_and_the_next_reply_resumes(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.tool_max_iterations = 2
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [[PLAN], [{"name": "list_files", "arguments": {}}], "Out of steps."]
    await say(
        client, auth_headers, conv["id"], "Do the whole thing", sessionmaker, gateway, settings
    )
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "paused"
    assert task["status_reason"] == "The reply used all of its tool steps"

    fake.turns = ["Continuing."]
    await say(client, auth_headers, conv["id"], "continue", sessionmaker, gateway, settings)
    assert "It was interrupted" in system_prompt(fake)
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "active" and task["runs"] == 2
    async with sessionmaker() as db:
        kinds = (await db.scalars(select(AgentTaskEvent.kind))).all()
    assert "resumed" in kinds


async def test_a_failed_reply_pauses_the_task(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [[PLAN], "Planned."]
    await say(client, auth_headers, conv["id"], "Plan it", sessionmaker, gateway, settings)
    fake.fail_status = 400
    await say(client, auth_headers, conv["id"], "Now do it", sessionmaker, gateway, settings)
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "paused"
    assert task["status_reason"].startswith("The reply failed")


async def test_stopping_a_reply_pauses_the_task(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, tool_profile="standard")
    fake.turns = [
        [PLAN],
        [{"name": "write_file", "arguments": {"path": "x.txt", "content": "x"}}],
    ]
    sent = await say(client, auth_headers, conv["id"], "Write x", sessionmaker, gateway, settings)
    # write_file asks first under the standard profile: the run waits; the user stops it.
    cancelled = await client.post(f"/api/v1/runs/{sent['run']['id']}/cancel", headers=auth_headers)
    assert cancelled.status_code == 200, cancelled.text
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "paused" and task["status_reason"] == "The reply was stopped"


async def test_state_survives_when_history_no_longer_fits(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    """The first message (with the request) is dropped from the context of a later reply;
    the stored task state still carries the objective and progress."""
    fake.context_tokens = 16_000
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [[PLAN], "Planned."]
    first = "Please add a health endpoint. " + "Background detail. " * 630
    await say(client, auth_headers, conv["id"], first, sessionmaker, gateway, settings)
    fake.turns = ["Continuing."]
    second = "Here is the log you asked for: " + "log line ok. " * 1600
    await say(client, auth_headers, conv["id"], second, sessionmaker, gateway, settings)
    messages = fake.requests[-1]["messages"]
    assert not any("Background detail" in str(m.get("content")) for m in messages[1:])
    assert "Objective: Add a health endpoint to the API and test it" in messages[0]["content"]


# ---------------------------------------------------------------------- API


async def test_user_can_resume_abandon_and_delete(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.tool_max_iterations = 1
    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [[PLAN], "Paused."]
    await say(client, auth_headers, conv["id"], "Do it", sessionmaker, gateway, settings)
    task = (await open_tasks(client, auth_headers))[0]
    assert task["status"] == "paused"
    assert task["conversation_title"]

    fake.turns = ["Resumed and continuing."]
    resumed = await client.post(f"/api/v1/agent-tasks/{task['id']}/resume", headers=auth_headers)
    assert resumed.status_code == 200, resumed.text
    await drain_runs(sessionmaker, gateway, settings)
    assert fake.requests[-1]["messages"][-1]["content"].startswith(
        "Continue the task: Add a health"
    )
    assert "<task_state>" in system_prompt(fake)

    abandoned = await client.patch(
        f"/api/v1/agent-tasks/{task['id']}", json={"status": "abandoned"}, headers=auth_headers
    )
    assert abandoned.json()["status"] == "abandoned"
    assert abandoned.json()["status_reason"] == "Stopped by you"
    again = await client.post(f"/api/v1/agent-tasks/{task['id']}/resume", headers=auth_headers)
    assert again.status_code == 409
    fake.turns = ["Something new."]
    await say(client, auth_headers, conv["id"], "Hello", sessionmaker, gateway, settings)
    assert "<task_state>" not in system_prompt(fake)

    deleted = await client.delete(f"/api/v1/agent-tasks/{task['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    async with sessionmaker() as db:
        assert (await db.scalars(select(AgentTask))).all() == []


async def test_tasks_are_private(client, auth_headers, sessionmaker, gateway, settings, fake):
    from app.auth.passwords import hash_password
    from app.models import User
    from tests.conftest import PASSWORD

    conv = await new_conversation(client, auth_headers, tool_profile="read_only")
    fake.turns = [[PLAN], "ok"]
    await say(client, auth_headers, conv["id"], "Plan", sessionmaker, gateway, settings)
    task = (await open_tasks(client, auth_headers))[0]
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password(PASSWORD)))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": PASSWORD}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/agent-tasks?status=all", headers=other)).json() == []
    for method, path in [
        ("GET", f"/api/v1/agent-tasks/{task['id']}"),
        ("POST", f"/api/v1/agent-tasks/{task['id']}/resume"),
        ("DELETE", f"/api/v1/agent-tasks/{task['id']}"),
    ]:
        assert (await client.request(method, path, headers=other)).status_code == 404

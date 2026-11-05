"""Phase 5 (personal assistant): tasks, recurrence, dependencies, reminders, suggestions
from chats, the daily briefing, the task tools and the assistant settings."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Job, ScheduledTask, UserTask
from app.services.assistant import tasks as svc
from tests.fakes import FakeLlama, drain_jobs, drain_runs


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def add(client, headers, title, **body) -> dict:
    response = await client.post("/api/v1/tasks", json={"title": title, **body}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def patch(client, headers, task_id, **body) -> dict:
    response = await client.patch(f"/api/v1/tasks/{task_id}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def chat(client, headers, text, sessionmaker, gateway, settings, **conversation) -> dict:
    conv = (await client.post("/api/v1/conversations", json=conversation, headers=headers)).json()
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": uuid.uuid4().hex, "content": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    await drain_runs(sessionmaker, gateway, settings)
    return conv


def today() -> date:
    return datetime.now(UTC).date()


# ---------------------------------------------------------------------- dates


def test_due_dates_are_local_and_all_day_or_timed():
    oslo = ZoneInfo("Europe/Oslo")
    day, all_day = svc.parse_due("2026-11-06", oslo)
    assert all_day and day == datetime(2026, 11, 5, 23, 0, tzinfo=UTC)
    moment, all_day = svc.parse_due("2026-11-06T15:00", oslo)
    assert not all_day and moment == datetime(2026, 11, 6, 14, 0, tzinfo=UTC)
    assert svc.parse_due("2026-11-06T15:00+00:00", oslo)[0].hour == 15
    assert svc.parse_due(None, oslo) == (None, True)


def test_repeats_keep_local_time_and_month_ends():
    oslo = ZoneInfo("Europe/Oslo")
    # 09:00 in Oslo on the Friday before summer time stays 09:00 a week later.
    before = datetime(2026, 3, 27, 8, 0, tzinfo=UTC)  # 09:00 CET
    after = svc.next_due(before, {"freq": "weekly"}, oslo)
    assert after.astimezone(oslo).hour == 9 and after.astimezone(oslo).day == 3
    jan31 = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    assert svc.next_due(jan31, {"freq": "monthly"}, ZoneInfo("UTC")).date() == date(2026, 2, 28)
    friday = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    assert svc.next_due(friday, {"freq": "weekdays"}, ZoneInfo("UTC")).weekday() == 0
    assert svc.next_due(friday, {"freq": "daily", "interval": 3}, ZoneInfo("UTC")).day == 9


# ---------------------------------------------------------------------- the list


async def test_create_list_update_complete(client, auth_headers):
    later = (today() + timedelta(days=5)).isoformat()
    soon = (today() + timedelta(days=1)).isoformat()
    a = await add(client, auth_headers, "Write the report", due=later, priority="high")
    b = await add(client, auth_headers, "Call the plumber", due=soon)
    await add(client, auth_headers, "Someday: learn Rust", priority="low")
    listed = (await client.get("/api/v1/tasks", headers=auth_headers)).json()
    assert [t["title"] for t in listed] == [
        "Call the plumber",
        "Write the report",
        "Someday: learn Rust",
    ]
    assert listed[1]["due_date"] == later and listed[1]["priority"] == "high"

    changed = await patch(client, auth_headers, b["id"], status="done")
    assert changed["task"]["status"] == "done" and changed["task"]["completed_at"]
    assert changed["next_occurrence"] is None
    assert [
        t["title"]
        for t in (await client.get("/api/v1/tasks?status=done", headers=auth_headers)).json()
    ] == ["Call the plumber"]
    cleared = await patch(client, auth_headers, a["id"], due="", notes="Section 3 first")
    assert cleared["task"]["due_at"] is None and cleared["task"]["notes"] == "Section 3 first"


async def test_subtasks_dependencies_and_validation(client, auth_headers):
    parent = await add(client, auth_headers, "Move house")
    child = await add(client, auth_headers, "Book the van", parent_id=parent["id"])
    pack = await add(client, auth_headers, "Pack", blocked_by=[child["id"]])
    assert pack["blocked"] is True
    await patch(client, auth_headers, child["id"], status="done")
    assert (await client.get(f"/api/v1/tasks/{pack['id']}", headers=auth_headers)).json()[
        "blocked"
    ] is False
    loop = await client.patch(
        f"/api/v1/tasks/{parent['id']}", json={"parent_id": child["id"]}, headers=auth_headers
    )
    assert loop.status_code == 422
    unknown = await client.post(
        "/api/v1/tasks",
        json={"title": "x", "blocked_by": [str(uuid.uuid4())]},
        headers=auth_headers,
    )
    assert unknown.status_code == 422
    bad_file = await client.post(
        "/api/v1/tasks",
        json={"title": "x", "attachment_ids": [str(uuid.uuid4())]},
        headers=auth_headers,
    )
    assert bad_file.status_code == 422
    bad_date = await client.post(
        "/api/v1/tasks", json={"title": "x", "due": "next Friday"}, headers=auth_headers
    )
    assert bad_date.status_code == 422


async def test_completing_a_repeating_task_creates_the_next(client, auth_headers):
    due = (today() + timedelta(days=1)).isoformat()
    task = await add(
        client,
        auth_headers,
        "Water the plants",
        due=due,
        remind=f"{due}T08:00",
        recurrence={"freq": "weekly"},
    )
    changed = await patch(client, auth_headers, task["id"], status="done")
    following = changed["next_occurrence"]
    assert following["status"] == "open" and following["title"] == "Water the plants"
    assert following["due_date"] == (today() + timedelta(days=8)).isoformat()
    assert following["remind_at"].startswith((today() + timedelta(days=8)).isoformat())
    assert following["recurrence"] == {"freq": "weekly", "interval": 1}
    no_due = await client.post(
        "/api/v1/tasks", json={"title": "x", "recurrence": {"freq": "daily"}}, headers=auth_headers
    )
    assert no_due.status_code == 422


# ---------------------------------------------------------------------- reminders


async def test_reminders_fire_once_and_only_for_active_tasks(
    client, auth_headers, sessionmaker, settings
):
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    due = await add(client, auth_headers, "Take the bins out", remind=past)
    done = await add(client, auth_headers, "Already done", remind=past)
    await patch(client, auth_headers, done["id"], status="done")
    await add(
        client, auth_headers, "Later", remind=(datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    assert await svc.fire_due_reminders(sessionmaker, settings) == 1
    assert await svc.fire_due_reminders(sessionmaker, settings) == 0  # once
    async with sessionmaker() as db:
        pushes = (await db.scalars(select(Job).where(Job.kind == "push.notify"))).all()
    assert len(pushes) == 1
    assert pushes[0].payload["body"] == "Take the bins out"
    assert pushes[0].payload["url"] == "/#/today"
    shown = (await client.get(f"/api/v1/tasks/{due['id']}", headers=auth_headers)).json()
    assert shown["reminded_at"] is not None
    # Moving the reminder arms it again.
    again = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await patch(client, auth_headers, due["id"], remind=again)
    assert await svc.fire_due_reminders(sessionmaker, settings) == 1


# ---------------------------------------------------------------------- briefing


async def test_briefing_and_suggested_order(client, auth_headers):
    yesterday = (today() - timedelta(days=1)).isoformat()
    tomorrow = (today() + timedelta(days=1)).isoformat()
    await add(client, auth_headers, "Overdue invoice", due=yesterday)
    await add(client, auth_headers, "Today's call", due=today().isoformat(), priority="high")
    await add(client, auth_headers, "Tomorrow's prep", due=tomorrow)
    await add(client, auth_headers, "No date, urgent", priority="urgent")
    briefing = (await client.get("/api/v1/assistant/briefing", headers=auth_headers)).json()
    assert [t["title"] for t in briefing["overdue"]] == ["Overdue invoice"]
    assert briefing["overdue"][0]["overdue"] is True
    assert [t["title"] for t in briefing["due_today"]] == ["Today's call"]
    assert [t["title"] for t in briefing["upcoming"]] == ["Tomorrow's prep"]
    assert [t["title"] for t in briefing["plan"]][:2] == ["Overdue invoice", "Today's call"]
    assert briefing["calendar"] is None
    assert "Overdue:" in briefing["text"] and "Calendar: not connected." in briefing["text"]
    assert briefing["waiting"] == {"approvals": 0, "memories": 0, "skills": 0}


async def test_timezone_decides_what_today_is(client, auth_headers):
    response = await client.put(
        "/api/v1/assistant/settings", json={"timezone": "Pacific/Kiritimati"}, headers=auth_headers
    )
    assert response.json()["timezone"] == "Pacific/Kiritimati"  # UTC+14
    local_today = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    await add(client, auth_headers, "Local today", due=local_today.isoformat())
    briefing = (await client.get("/api/v1/assistant/briefing", headers=auth_headers)).json()
    assert briefing["date"] == local_today.isoformat()
    assert [t["title"] for t in briefing["due_today"]] == ["Local today"]
    bad = await client.put(
        "/api/v1/assistant/settings", json={"timezone": "Mars/Base"}, headers=auth_headers
    )
    assert bad.status_code == 422


async def test_daily_briefing_schedule(client, auth_headers, sessionmaker):
    response = await client.put(
        "/api/v1/assistant/settings", json={"briefing_time": "07:30"}, headers=auth_headers
    )
    assert response.json()["briefing_time"] == "07:30"
    async with sessionmaker() as db:
        schedule = (await db.scalars(select(ScheduledTask))).one()
    assert (schedule.kind, schedule.time_of_day, schedule.tool_profile) == (
        "daily",
        "07:30",
        "read_only",
    )
    assert "daily_briefing" in schedule.prompt
    await client.put(
        "/api/v1/assistant/settings", json={"briefing_time": "08:15"}, headers=auth_headers
    )
    async with sessionmaker() as db:
        assert [(s.time_of_day) for s in (await db.scalars(select(ScheduledTask))).all()] == [
            "08:15"
        ]
    off = await client.put(
        "/api/v1/assistant/settings", json={"briefing_time": ""}, headers=auth_headers
    )
    assert off.json()["briefing_time"] is None
    async with sessionmaker() as db:
        assert (await db.scalars(select(ScheduledTask))).all() == []


# ---------------------------------------------------------------------- suggestions


async def test_deadlines_in_a_chat_become_suggestions(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    due = (today() + timedelta(days=10)).isoformat()
    fake.memory_reply = json.dumps(
        {"memories": [], "tasks": [{"title": "Renew passport", "due": due}, "Call mum"]}
    )
    await chat(
        client,
        auth_headers,
        "I need to renew my passport before the trip",
        sessionmaker,
        gateway,
        settings,
    )
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    prompt = fake.extraction_requests[-1]["messages"][0]["content"]
    assert "Also note to-dos the user said" in prompt and "Today is" in prompt
    suggested = (await client.get("/api/v1/tasks?status=suggested", headers=auth_headers)).json()
    assert {(t["title"], t["due_date"]) for t in suggested} == {
        ("Renew passport", due),
        ("Call mum", None),
    }
    assert all(t["source"] == "suggested" and t["source_conversation_id"] for t in suggested)
    assert (await client.get("/api/v1/tasks", headers=auth_headers)).json() == []  # not to-dos yet

    passport = next(t for t in suggested if t["title"] == "Renew passport")
    accepted = await client.post(f"/api/v1/tasks/{passport['id']}/accept", headers=auth_headers)
    assert accepted.json()["status"] == "open"
    mum = next(t for t in suggested if t["title"] == "Call mum")
    await client.delete(f"/api/v1/tasks/{mum['id']}", headers=auth_headers)

    # The same to-do mentioned again isn't suggested twice.
    await chat(
        client,
        auth_headers,
        "Remember the passport renewal please",
        sessionmaker,
        gateway,
        settings,
    )
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    titles = [
        t["title"]
        for t in (await client.get("/api/v1/tasks?status=all", headers=auth_headers)).json()
    ]
    assert titles.count("Renew passport") == 1


async def test_suggestions_can_be_switched_off(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await client.put(
        "/api/v1/assistant/settings", json={"suggest_tasks": False}, headers=auth_headers
    )
    fake.memory_reply = json.dumps({"memories": [], "tasks": ["Pay the bill"]})
    await chat(
        client, auth_headers, "I must pay the electricity bill", sessionmaker, gateway, settings
    )
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    assert "Also note to-dos" not in fake.extraction_requests[-1]["messages"][0]["content"]
    assert (await client.get("/api/v1/tasks?status=all", headers=auth_headers)).json() == []


# ---------------------------------------------------------------------- tools


async def test_task_tools_in_a_chat(client, auth_headers, sessionmaker, gateway, settings, fake):
    project = (
        await client.post("/api/v1/projects", json={"name": "Home"}, headers=auth_headers)
    ).json()
    due = (today() + timedelta(days=2)).isoformat()
    fake.turns = [
        [
            {
                "name": "task_add",
                "arguments": {
                    "title": "Fix the gate",
                    "due": due,
                    "priority": "high",
                    "repeat": "weekly",
                },
            }
        ],
        [{"name": "task_list", "arguments": {}}],
        "Added.",
    ]
    await chat(
        client,
        auth_headers,
        "Add fixing the gate for Saturday",
        sessionmaker,
        gateway,
        settings,
        tool_profile="developer",
        project_id=project["id"],
    )
    system = fake.requests[-1]["messages"][0]["content"]
    assert "The user keeps a task list in Celius" in system
    listed = fake.requests[-1]["messages"][-1]["content"]
    assert (
        "Fix the gate, due " + due in listed
        and "high priority" in listed
        and "(this project)" in listed
    )
    task = (await client.get("/api/v1/tasks", headers=auth_headers)).json()[0]
    assert (task["source"], task["project_id"], task["recurrence"]["freq"]) == (
        "model",
        project["id"],
        "weekly",
    )

    short = task["id"][:8]
    fake.turns = [
        [{"name": "task_update", "arguments": {"task": short, "status": "done"}}],
        [{"name": "daily_briefing", "arguments": {}}],
        "Done.",
    ]
    await chat(
        client,
        auth_headers,
        "I fixed the gate",
        sessionmaker,
        gateway,
        settings,
        tool_profile="developer",
    )
    result = fake.requests[-2]["messages"][-1]["content"]
    assert "[done]" in result and "Next occurrence: " in result
    assert "Briefing for" in fake.requests[-1]["messages"][-1]["content"]


async def test_changing_tasks_asks_first_under_the_standard_profile(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [[{"name": "task_add", "arguments": {"title": "Book dentist"}}]]
    await chat(
        client,
        auth_headers,
        "Add a dentist task",
        sessionmaker,
        gateway,
        settings,
        tool_profile="standard",
    )
    pending = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    assert [p["tool_name"] for p in pending] == ["task_add"]
    assert (await client.get("/api/v1/tasks", headers=auth_headers)).json() == []


# ---------------------------------------------------------------------- privacy


async def test_tasks_are_private(client, auth_headers, sessionmaker):
    from app.auth.passwords import hash_password
    from app.models import User
    from tests.conftest import PASSWORD

    mine = await add(client, auth_headers, "My secret errand")
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password(PASSWORD)))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": PASSWORD}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/tasks?status=all", headers=other)).json() == []
    assert (await client.get(f"/api/v1/tasks/{mine['id']}", headers=other)).status_code == 404
    hijack = await client.post(
        "/api/v1/tasks", json={"title": "x", "parent_id": mine["id"]}, headers=other
    )
    assert hijack.status_code == 422
    briefing = (await client.get("/api/v1/assistant/briefing", headers=other)).json()
    assert "secret errand" not in json.dumps(briefing)
    async with sessionmaker() as db:
        assert (await db.get(UserTask, uuid.UUID(mine["id"]))).title == "My secret errand"


async def test_task_changes_reach_the_sync_feed(client, auth_headers):
    task = await add(client, auth_headers, "Synced")
    feed = await client.get("/api/v1/sync/changes?cursor=0", headers=auth_headers)
    assert feed.status_code == 200, feed.text
    assert any(
        c["entity_type"] == "user_task" and c["entity_id"] == task["id"]
        for c in feed.json()["changes"]
    )

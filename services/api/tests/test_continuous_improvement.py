"""Phase 7: analytics from real activity, memory benchmark and upkeep, keyword stemming,
skill improvement from failure clusters, autonomy presets, benchmark comparison."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Job, Memory, MemoryConflict, MemoryStatus, SkillEvent, User
from app.services.memory import consolidate
from app.services.memory.retrieval import keyword_score, stem, terms
from tests.fakes import FakeLlama, drain_jobs, drain_runs


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


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


async def remember(client, headers, content, **body) -> dict:
    response = await client.post(
        "/api/v1/memories", json={"content": content, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def member(client, sessionmaker, name="member", **fields) -> dict:
    from app.auth.passwords import hash_password
    from tests.conftest import PASSWORD

    async with sessionmaker() as db:
        db.add(User(username=name, password_hash=hash_password(PASSWORD), **fields))
        await db.commit()
    login = await client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
    client.cookies.clear()
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ---------------------------------------------------------------------- analytics


async def test_insights_from_real_activity(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    empty = (await client.get("/api/v1/insights", headers=auth_headers)).json()
    assert empty["runs"]["total"] == 0 and empty["runs"]["completion_rate"] is None  # no data ≠ 0%

    fake.turns = [
        [{"name": "list_files", "arguments": {}}],
        [{"name": "read_file", "arguments": {"path": "missing.txt"}}],
        "Done.",
    ]
    await chat(
        client,
        auth_headers,
        "Look around",
        sessionmaker,
        gateway,
        settings,
        tool_profile="read_only",
    )
    await chat(client, auth_headers, "Hello", sessionmaker, gateway, settings)
    fake.fail_status = 400
    await chat(client, auth_headers, "This one fails", sessionmaker, gateway, settings)
    fake.fail_status = None

    data = (await client.get("/api/v1/insights?days=7", headers=auth_headers)).json()
    runs = data["runs"]
    assert runs["total"] == 3 and runs["completed"] == 2
    assert runs["completion_rate"] == pytest.approx(0.667, abs=0.001)
    assert runs["failure_reasons"] == {"model_request_rejected": 1}
    assert runs["with_tools"] == 1 and runs["duration_seconds_p50"] is not None
    tools = {t["tool"]: t for t in data["tools"]["by_tool"]}
    assert tools["list_files"]["success_rate"] == 1.0 and tools["read_file"]["success_rate"] == 0.0
    assert data["tools"]["success_rate"] == 0.5
    context = data["context"]
    assert context["prompt_tokens_p50"] > 0 and context["profiles"] == {"standard": 3}
    assert (
        "identity" in context["section_tokens_mean"] and "tools" in context["section_tokens_mean"]
    )

    # Someone else sees none of it; everyone's figures are for administrators.
    other = await member(client, sessionmaker)
    assert (await client.get("/api/v1/insights", headers=other)).json()["runs"]["total"] == 0
    assert (await client.get("/api/v1/insights?scope=everyone", headers=other)).status_code == 403
    everyone = (await client.get("/api/v1/insights?scope=everyone", headers=auth_headers)).json()
    assert everyone["scope"] == "everyone" and everyone["runs"]["total"] == 3


async def test_insights_cover_memory_agent_and_assistant(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.memory_reply = '{"memories": ["The user likes jazz.", "The user owns a canoe."]}'
    settings.memory_extraction_max_per_turn = 2
    await chat(
        client, auth_headers, "I love jazz and paddle my canoe", sessionmaker, gateway, settings
    )
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    proposed = (await client.get("/api/v1/memories?status=proposed", headers=auth_headers)).json()
    await client.post(
        f"/api/v1/memories/{proposed[0]['id']}/approve", json={}, headers=auth_headers
    )
    await client.post(f"/api/v1/memories/{proposed[1]['id']}/reject", headers=auth_headers)
    fake.turns = [
        [{"name": "plan_start", "arguments": {"objective": "Tidy", "steps": ["a"]}}],
        [
            {
                "name": "plan_update",
                "arguments": {"step": 1, "step_status": "done", "check": "ls", "passed": True},
            }
        ],
        [{"name": "plan_update", "arguments": {"status": "completed"}}],
        "Done.",
    ]
    await chat(
        client, auth_headers, "Tidy up", sessionmaker, gateway, settings, tool_profile="read_only"
    )
    await client.post("/api/v1/tasks", json={"title": "Buy milk"}, headers=auth_headers)
    data = (await client.get("/api/v1/insights", headers=auth_headers)).json()
    assert data["memory"]["proposals"] == 2 and data["memory"]["acceptance_rate"] == 0.5
    assert data["agent"]["tasks"] == 1 and data["agent"]["completion_rate"] == 1.0
    assert data["agent"]["verified_completion_rate"] == 1.0
    assert data["assistant"]["tasks_added"] == 1


# ---------------------------------------------------------------------- keyword stemming


def test_stemming_makes_forms_of_a_word_meet():
    assert stem("deploys") == stem("deploying") == stem("deployed") == "deploy"
    assert stem("backups") == "backup" and stem("batteries") == "battery"
    assert stem("class") == "class" and stem("is") == "is" and stem("boxes") == "box"
    assert (
        keyword_score(terms("How do I deploy Celius?"), "The user deploys Celius with compose.")
        > 0.2
    )


# ---------------------------------------------------------------------- memory benchmark


async def test_memory_benchmark_guards_retrieval_quality():
    from devtools.memory_eval import evaluate, report

    score = (await evaluate())[0]
    # Measured on the bundled set with keyword search only (ADR C-12). A change that
    # lowers these is a regression; one that raises them should update the numbers.
    assert score.precision >= 0.67 and score.recall >= 0.71 and score.mrr >= 0.75
    assert score.silence >= 0.75
    previous = [
        {
            "weights": list(score.weights),
            "precision": 0.5,
            "recall": 0.469,
            "mrr": 0.5,
            "silence": 0.75,
        }
    ]
    text = report([score], previous)
    assert "(+0.2" in text and "Missed" in text


async def test_memory_benchmark_grid_and_offline_embeddings():
    from devtools.memory_eval import GRID, evaluate

    scores = await evaluate(grid=True, hash_embeddings=True)
    assert [s.weights for s in scores] == GRID
    assert all(0 <= s.recall <= 1 for s in scores)


# ---------------------------------------------------------------------- memory upkeep


async def test_consolidation_flags_duplicates_once(
    client, auth_headers, sessionmaker, settings, user
):
    first = await remember(client, auth_headers, "The user drinks green tea every single morning.")
    second = await remember(client, auth_headers, "The user drinks green tea each morning.")
    async with sessionmaker() as db:
        result = await consolidate.consolidate(db, user.id, settings)
        await db.commit()
    assert result["duplicates"] == 1
    conflicts = (await client.get("/api/v1/memory/conflicts", headers=auth_headers)).json()
    assert conflicts[0]["reason"] == "Possibly the same fact"
    assert (conflicts[0]["memory"]["id"], conflicts[0]["other"]["id"]) == (
        second["id"],
        first["id"],
    )
    await client.post(
        f"/api/v1/memory/conflicts/{conflicts[0]['id']}/resolve",
        json={"decision": "keep_new"},
        headers=auth_headers,
    )
    assert [
        m["id"] for m in (await client.get("/api/v1/memories", headers=auth_headers)).json()
    ] == [second["id"]]
    async with sessionmaker() as db:
        again = await consolidate.consolidate(db, user.id, settings)
    assert again["duplicates"] == 0


async def test_stale_inferences_go_back_to_review(
    client, auth_headers, sessionmaker, settings, user
):
    old = await remember(client, auth_headers, "The user may like sailing.")
    kept = await remember(client, auth_headers, "The user is called Robin.")
    async with sessionmaker() as db:
        row = await db.get(Memory, uuid.UUID(old["id"]))
        row.confidence, row.created_at = 0.5, datetime.now(UTC) - timedelta(days=120)
        other = await db.get(Memory, uuid.UUID(kept["id"]))
        other.created_at = datetime.now(UTC) - timedelta(days=120)  # confirmed: stays
        await db.commit()
        result = await consolidate.consolidate(db, user.id, settings)
        await db.commit()
    assert result["stale"] == 1
    review = (await client.get("/api/v1/memories?status=proposed", headers=auth_headers)).json()
    assert [m["id"] for m in review] == [old["id"]]
    assert review[0]["attributes"]["review_reason"].startswith("Never confirmed")
    approved = await client.post(
        f"/api/v1/memories/{old['id']}/approve", json={}, headers=auth_headers
    )
    assert (
        approved.json()["confidence"] == 1.0
        and "review_reason" not in approved.json()["attributes"]
    )


async def test_consolidation_is_queued_per_user_and_period(
    client, auth_headers, sessionmaker, settings
):
    await remember(client, auth_headers, "The user likes chess.")
    assert await consolidate.queue_due(sessionmaker, settings) == 1
    assert await consolidate.queue_due(sessionmaker, settings) == 0  # already waiting
    await drain_jobs(sessionmaker, settings)
    assert await consolidate.queue_due(sessionmaker, settings) == 0  # done this week
    settings.memory_consolidation_days = 0
    async with sessionmaker() as db:
        assert (await db.scalars(select(Job).where(Job.kind == "memory.consolidate"))).all()


# ---------------------------------------------------------------------- skill improvement


SKILL = """---
name: deploy-app
description: Deploy the app with docker compose and check it is healthy.
---

1. Read config/app.env first.
2. Run docker compose up -d.
"""


async def use_skill_and_fail(client, headers, sessionmaker, gateway, settings, fake):
    fake.turns = [
        [{"name": "skill_read", "arguments": {"name": "deploy-app"}}],
        [{"name": "read_file", "arguments": {"path": "config/app.env"}}],
        "It failed.",
    ]
    await chat(
        client, headers, "Deploy the app", sessionmaker, gateway, settings, tool_profile="read_only"
    )


async def test_a_failure_cluster_proposes_an_improved_version(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.skill_autogen_min_tool_calls = 100  # only the improvement path
    skill = (
        await client.post(
            "/api/v1/skills", json={"files": {"SKILL.md": SKILL}}, headers=auth_headers
        )
    ).json()
    for _ in range(2):
        await use_skill_and_fail(client, auth_headers, sessionmaker, gateway, settings, fake)
    async with sessionmaker() as db:
        assert (await db.scalars(select(Job).where(Job.kind == "skill.improve"))).all() == []
    await use_skill_and_fail(client, auth_headers, sessionmaker, gateway, settings, fake)  # third
    await use_skill_and_fail(client, auth_headers, sessionmaker, gateway, settings, fake)  # fourth
    async with sessionmaker() as db:
        jobs = (await db.scalars(select(Job).where(Job.kind == "skill.improve"))).all()
    assert len(jobs) == 1  # once a week at most

    fake.skill_reply = "UPDATE deploy-app\n" + SKILL.replace(
        "1. Read config/app.env first.",
        "1. If config/app.env is missing, copy config/app.env.example.",
    )
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    request = fake.skill_requests[-1]["messages"][1]["content"]
    assert (
        "This skill keeps failing" in request
        and "read_file" in request
        and "config/app.env" in request
    )
    detail = (await client.get(f"/api/v1/skills/{skill['id']}", headers=auth_headers)).json()
    assert detail["published_version"] == 1 and detail["pending_versions"] == [
        2
    ]  # balanced: approve
    assert detail["versions"][0]["changelog"] == "Improved after repeated failures"


async def test_user_feedback_counts_and_conservative_trust_never_improves(
    client, auth_headers, sessionmaker, settings
):
    skill = (
        await client.post(
            "/api/v1/skills", json={"files": {"SKILL.md": SKILL}}, headers=auth_headers
        )
    ).json()
    await client.put(
        "/api/v1/skills/settings", json={"trust": "conservative"}, headers=auth_headers
    )
    for _ in range(3):
        await client.post(
            f"/api/v1/skills/{skill['id']}/feedback",
            json={"outcome": "failure"},
            headers=auth_headers,
        )
    async with sessionmaker() as db:
        assert (await db.scalars(select(Job).where(Job.kind == "skill.improve"))).all() == []
    await client.put("/api/v1/skills/settings", json={"trust": "balanced"}, headers=auth_headers)
    await client.post(
        f"/api/v1/skills/{skill['id']}/feedback",
        json={"outcome": "corrected", "note": "needed --build"},
        headers=auth_headers,
    )
    async with sessionmaker() as db:
        assert len((await db.scalars(select(Job).where(Job.kind == "skill.improve"))).all()) == 1
        events = (await db.scalars(select(SkillEvent).where(SkillEvent.kind == "lifecycle"))).all()
    assert any(e.detail.get("action") == "improvement_requested" for e in events)


# ---------------------------------------------------------------------- autonomy


async def test_autonomy_presets(client, auth_headers):
    current = (await client.get("/api/v1/assistant/autonomy", headers=auth_headers)).json()
    assert current["preset"] == "balanced" and current["always_asks"] == ["email_send"]
    careful = (
        await client.put(
            "/api/v1/assistant/autonomy", json={"preset": "careful"}, headers=auth_headers
        )
    ).json()
    assert careful["preset"] == "careful" and careful["tool_profile"] == "read_only"
    assert (await client.get("/api/v1/skills/settings", headers=auth_headers)).json()[
        "trust"
    ] == "conservative"
    assert (await client.get("/api/v1/assistant/settings", headers=auth_headers)).json()[
        "suggest_tasks"
    ] is False
    custom = (
        await client.put(
            "/api/v1/assistant/autonomy", json={"suggest_tasks": True}, headers=auth_headers
        )
    ).json()
    assert custom["preset"] == "custom"
    hands_off = (
        await client.put(
            "/api/v1/assistant/autonomy", json={"preset": "hands_off"}, headers=auth_headers
        )
    ).json()
    assert hands_off["memory_require_review"] is False and hands_off["skill_trust"] == "autonomous"
    assert (await client.get("/api/v1/memory/settings", headers=auth_headers)).json()[
        "require_review"
    ] is False


async def test_presets_never_widen_an_administrators_limit(client, sessionmaker):
    limited = await member(
        client, sessionmaker, "limited", allowed_tool_profiles=["read_only", "standard"]
    )
    result = (
        await client.put(
            "/api/v1/assistant/autonomy", json={"preset": "hands_off"}, headers=limited
        )
    ).json()
    assert result["tool_profile"] == "standard"
    assert "isn't allowed" in result["notes"][0]
    assert result["allowed_tool_profiles"] == ["read_only", "standard"]


# ---------------------------------------------------------------------- benchmarks


def test_prompt_eval_summary_and_compare():
    from devtools.prompt_eval import Result, compare, summary

    results = [
        Result("a", "lite", True, "ok", 400, {}, 9, ["list_files"], 1.0),
        Result("b", "lite", False, "x", 600, {}, 9, [], 3.0),
    ]
    totals = summary(results)
    assert totals["lite"] == {
        "pass_rate": 0.5,
        "prompt_tokens_mean": 500,
        "tool_calls_mean": 0.5,
        "seconds_mean": 2.0,
    }
    text = compare(totals, {"lite": {"pass_rate": 0.25, "prompt_tokens_mean": 520}})
    assert "0.5 (+0.25)" in text and "500 (-20)" in text
    json.dumps(totals)


async def test_stale_memory_rule_respects_settings(
    client, auth_headers, sessionmaker, settings, user
):
    memory = await remember(client, auth_headers, "The user might collect stamps.")
    async with sessionmaker() as db:
        row = await db.get(Memory, uuid.UUID(memory["id"]))
        row.confidence, row.created_at = 0.5, datetime.now(UTC) - timedelta(days=120)
        await db.commit()
    settings.memory_stale_days = 0
    async with sessionmaker() as db:
        assert (await consolidate.consolidate(db, user.id, settings))["stale"] == 0
        assert (await db.get(Memory, uuid.UUID(memory["id"]))).status == MemoryStatus.active
        assert (await db.scalars(select(MemoryConflict))).all() == []

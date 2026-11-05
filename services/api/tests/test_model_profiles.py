"""Phase 4 (local model intelligence): capability registry, prompt profiles, modular
prompt, tool-calling behaviour per profile, structured output, the evaluation harness."""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.models import ModelCatalogEntry, RunEvent, ToolCall
from app.services.model_registry import auto_profile
from app.services.prompting import PRINCIPLES, PromptBuilder
from tests.fakes import FakeLlama, drain_jobs, drain_runs


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def chat(client, headers, text, sessionmaker, gateway, settings, **conversation) -> dict:
    created = await client.post("/api/v1/conversations", json=conversation, headers=headers)
    conv = created.json()
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": uuid.uuid4().hex, "content": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    await drain_runs(sessionmaker, gateway, settings)
    return conv


async def started(sessionmaker) -> dict:
    async with sessionmaker() as db:
        events = (
            await db.scalars(
                select(RunEvent).where(RunEvent.type == "run.started").order_by(RunEvent.id)
            )
        ).all()
    return events[-1].data


def system_prompt(fake: FakeLlama) -> str:
    return fake.requests[-1]["messages"][0]["content"]


def offered(fake: FakeLlama) -> set[str]:
    return {t["function"]["name"] for t in fake.requests[-1].get("tools") or []}


async def set_registry(client, headers, **body) -> dict:
    response = await client.put(
        "/api/v1/models/registry", json={"model_id": "local-model", **body}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------- units


def test_auto_profile_uses_the_measured_context_never_the_name(settings):
    assert auto_profile(None, settings) == "standard"
    assert auto_profile(8192, settings) == "lite"
    assert auto_profile(32768, settings) == "standard"
    assert auto_profile(131072, settings) == "advanced"


def test_prompt_builder_orders_sections_and_measures_them():
    prompt = PromptBuilder()
    prompt.set("tools", "Tool rules.")
    prompt.set("identity", "You are Celius.")
    prompt.set("memory", "")
    prompt.set("principles", PRINCIPLES["standard"])
    assert prompt.render() == "You are Celius.\n\nTool rules."
    assert set(prompt.sizes()) == {"identity", "tools"}


# ---------------------------------------------------------------------- profiles in runs


async def test_standard_profile_keeps_the_prompt_as_it_was(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await chat(
        client, auth_headers, "Hi", sessionmaker, gateway, settings, tool_profile="read_only"
    )
    prompt = system_prompt(fake)
    assert prompt.startswith("You are a helpful assistant.\n\nToday's date is ")
    assert PRINCIPLES["lite"] not in prompt and PRINCIPLES["advanced"] not in prompt
    data = await started(sessionmaker)
    assert data["profile"] == "standard"
    assert {"identity", "task", "tools"} <= set(data["prompt_sections"])


async def test_small_context_gets_the_lite_profile(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.context_tokens = 8192
    await chat(
        client, auth_headers, "Hi", sessionmaker, gateway, settings, tool_profile="developer"
    )
    assert PRINCIPLES["lite"] in system_prompt(fake)
    assert "plan_status" not in offered(fake)  # compact toolset
    assert "plan_start" in offered(fake)
    data = await started(sessionmaker)
    assert data["profile"] == "lite"


async def test_lite_runs_one_tool_call_per_turn(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.context_tokens = 8192
    fake.turns = [
        [
            {"name": "list_files", "arguments": {}},
            {"name": "list_files", "arguments": {"path": "x"}},
        ],
        [{"name": "list_files", "arguments": {"path": "x"}}],
        "Done.",
    ]
    await chat(
        client, auth_headers, "List", sessionmaker, gateway, settings, tool_profile="read_only"
    )
    async with sessionmaker() as db:
        calls = (
            await db.scalars(
                select(ToolCall)
                .options(undefer(ToolCall.model_content))
                .order_by(ToolCall.step, ToolCall.position)
            )
        ).all()
    assert [(c.step, c.status.value) for c in calls] == [
        (0, "completed"),
        (0, "refused"),
        (1, "failed"),
    ]
    assert "one tool call per turn" in calls[1].model_content
    # The refused call could be made again next turn (it failed on its own merits:
    # the folder doesn't exist), not refused as a repeat.
    assert "already failed" not in (calls[2].model_content or "")


async def test_large_context_gets_the_advanced_profile(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.context_tokens = 131072
    await chat(
        client, auth_headers, "Hi", sessionmaker, gateway, settings, tool_profile="read_only"
    )
    assert PRINCIPLES["advanced"] in system_prompt(fake)
    assert (await started(sessionmaker))["profile"] == "advanced"


# ---------------------------------------------------------------------- the registry


async def test_admin_sets_profile_overrides_and_limits(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    entry = await set_registry(
        client,
        auth_headers,
        profile="lite",
        overrides={"parallel_tools": True, "context_tokens": 20000},
        limits={"max_tool_iterations": 1},
        notes="Small 4B model",
    )
    effective = entry["effective"]
    assert effective["profile"] == "lite" and effective["profile_source"] == "admin"
    assert effective["parallel_tools"] is True and effective["sources"]["parallel_tools"] == "admin"
    assert effective["context_tokens"] == 20000
    assert effective["max_tool_iterations"] == 1

    fake.turns = [
        [{"name": "list_files", "arguments": {}}],
        [{"name": "list_files", "arguments": {}}],
    ]
    await chat(
        client, auth_headers, "List", sessionmaker, gateway, settings, tool_profile="read_only"
    )
    # One tool step allowed: the second turn is offered no tools and must answer.
    assert fake.requests[-1].get("tools") is None
    assert "tool limit for this reply is reached" in fake.requests[-1]["messages"][-1]["content"]

    listing = (await client.get("/api/v1/models", headers=auth_headers)).json()
    assert listing["profiles"]["local-model"]["profile"] == "lite"
    registry = (await client.get("/api/v1/models/registry", headers=auth_headers)).json()
    assert registry[0]["notes"] == "Small 4B model"


async def test_admin_can_turn_tool_calling_off_for_a_model(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await set_registry(client, auth_headers, overrides={"tool_calling": False})
    await chat(
        client, auth_headers, "Hi", sessionmaker, gateway, settings, tool_profile="developer"
    )
    assert fake.requests[-1].get("tools") is None


async def test_a_tool_probe_is_stored_for_every_process(
    client, auth_headers, sessionmaker, gateway, settings, fake, monkeypatch
):
    async def probe(model_id):
        gateway._verified_tools[model_id] = False
        gateway._models_cache = None
        return False

    monkeypatch.setattr(gateway, "verify_tool_calling", probe)
    response = await client.post("/api/v1/models/local-model/verify-tools", headers=auth_headers)
    assert response.status_code == 200
    async with sessionmaker() as db:
        entry = await db.get(ModelCatalogEntry, (gateway.provider, "local-model"))
        assert entry.capabilities["tool_calling_verified"] is False
    # Another process (the worker) never saw the probe: it reads the registry.
    gateway._verified_tools.clear()
    gateway._models_cache = None
    await chat(
        client, auth_headers, "Hi", sessionmaker, gateway, settings, tool_profile="developer"
    )
    assert fake.requests[-1].get("tools") is None


async def test_registry_is_for_administrators(client, sessionmaker):
    from app.auth.passwords import hash_password
    from app.models import User
    from tests.conftest import PASSWORD

    async with sessionmaker() as db:
        db.add(User(username="member", password_hash=hash_password(PASSWORD)))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "member", "password": PASSWORD}
    )
    client.cookies.clear()
    member = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/models/registry", headers=member)).status_code == 403
    put = await client.put(
        "/api/v1/models/registry",
        json={"model_id": "local-model", "profile": "advanced"},
        headers=member,
    )
    assert put.status_code == 403


# ---------------------------------------------------------------------- structured output


async def test_memory_extraction_uses_json_mode_when_supported(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.memory_reply = '{"memories": []}'
    await chat(client, auth_headers, "I live in Oslo now", sessionmaker, gateway, settings)
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    assert "response_format" not in fake.extraction_requests[-1]

    await set_registry(client, auth_headers, overrides={"structured_output": True})
    await chat(client, auth_headers, "I work as a nurse", sessionmaker, gateway, settings)
    await drain_jobs(sessionmaker, settings, gateway=gateway)
    assert fake.extraction_requests[-1]["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------- evaluation harness


async def test_prompt_eval_harness_scores_scenarios_per_profile(fake):
    from devtools.prompt_eval import Scenario, evaluate, report

    fake.turns = [
        [{"name": "list_files", "arguments": {}}],
        "Here are your files.",
        "Nothing to list.",  # standard: answered without the tool
    ]
    scenarios = [Scenario("pick a tool", "List the files in my workspace.", "list_files")]
    results = await evaluate(
        "http://model.test:8080/v1",
        "local-model",
        ["lite", "standard"],
        scenarios,
        transport=httpx.MockTransport(fake.handler),
    )
    assert [(r.profile, r.passed) for r in results] == [("lite", True), ("standard", False)]
    assert results[0].tool_calls == ["list_files"]
    assert results[1].why.startswith("didn't call list_files")
    assert results[0].sections and results[0].prompt_tokens
    assert results[0].tools_offered < results[1].tools_offered  # lite: compact toolset
    text = report(results)
    assert "lite: 1/1 passed" in text and "standard: 0/1 passed" in text
    json.dumps([r.__dict__ for r in results])  # serialisable for --json


def test_tool_budget_keeps_core_tools_and_adds_the_cheapest_rest():
    from app.services.tools.toolset import BUILTIN, CORE_TOOLS, fit_to_budget, tool_tokens

    offered = dict(BUILTIN)
    core_cost = sum(tool_tokens(offered[n]) for n in CORE_TOOLS)
    left_out = fit_to_budget(offered, core_cost + 300)
    kept = set(offered) - set(left_out)
    assert CORE_TOOLS <= kept
    assert sum(tool_tokens(offered[n]) for n in kept) <= core_cost + 300
    extra = kept - CORE_TOOLS
    # Non-optional tools are added first, cheapest first: the cheapest one is kept, and
    # an optional tool is only kept once no left-out non-optional tool would still fit.
    others = [n for n in offered if n not in CORE_TOOLS and not offered[n].optional]
    assert min(others, key=lambda n: tool_tokens(offered[n])) in extra
    if any(offered[n].optional for n in extra):
        used = sum(tool_tokens(offered[n]) for n in kept if not offered[n].optional)
        skipped = [n for n in others if n in left_out]
        assert all(used + tool_tokens(offered[n]) > core_cost + 300 for n in skipped)
    assert fit_to_budget(offered, 10**6) == {}  # big contexts keep everything
    assert set(fit_to_budget(offered, 0)) == set(offered) - CORE_TOOLS  # core always stays


async def test_tool_definitions_leave_room_on_a_small_context(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    from app.services.context import estimate_tokens

    fake.context_tokens = 8192
    await chat(
        client, auth_headers, "Hi", sessionmaker, gateway, settings, tool_profile="developer"
    )
    tools = fake.requests[-1]["tools"]
    assert estimate_tokens(json.dumps(tools)) <= 8192 * settings.tool_context_share + 50
    names = {t["function"]["name"] for t in tools}
    assert {"read_file", "write_file", "task_add", "plan_start"} <= names
    # Left out for space, not for permission: not listed as missing.
    note = system_prompt(fake).split("Not available in this chat", 1)[-1]
    for name in ("task_update", "skill_create", "daily_briefing", "plan_status"):
        if name not in names:
            assert name not in note, name

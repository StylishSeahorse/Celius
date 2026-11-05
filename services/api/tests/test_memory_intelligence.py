"""Phase 1 (memory intelligence): categories, secrets, contradictions and corrections,
hybrid ranking, search, entities and relationships, re-embedding."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

import app.services.memory.embeddings as embeddings
from app.models import AuditEvent, Memory, MemoryChunk, MemoryStatus
from app.services.memory.ranking import Signals, Weights, combine, recency
from app.services.memory.sensitive import find_secret, redact
from app.services.memory.store import disagreement
from tests.fakes import FakeLlama, drain_jobs
from tests.test_memory import chat, remember, system_prompt


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


@pytest.fixture
def with_embeddings(settings, fake, monkeypatch):
    settings.model_enable_embeddings = True
    monkeypatch.setattr(embeddings, "TRANSPORT", httpx.MockTransport(fake.handler))


async def memories(client, headers, **params) -> list[dict]:
    response = await client.get("/api/v1/memories", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------- secrets


@pytest.mark.parametrize(
    "text",
    [
        "My password is hunter2!",
        "The API key is sk-proj-abcdefghijklmnopqrstuvwx",
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "db at postgres://admin:s3cret@db.local/app",
        "-----BEGIN OPENSSH PRIVATE KEY----- abc",
    ],
)
def test_secrets_are_detected(text):
    assert find_secret(text) is not None
    assert "[redacted secret]" in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "The user prefers metric units.",
        "The token is ready to use.",
        "The user's server runs Proxmox VE 8.2 at https://pve.example.com/ui",
        "Commit 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39 fixed the bug.",
    ],
)
def test_ordinary_text_is_not_a_secret(text):
    assert find_secret(text) is None
    assert redact(text) == text


async def test_api_refuses_to_remember_a_secret(client, auth_headers, sessionmaker):
    response = await client.post(
        "/api/v1/memories",
        json={"content": "The user's GitHub token is ghp_abcdefghijklmnopqrstuvwxyz012345"},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "sensitive"
    async with sessionmaker() as db:
        assert (await db.scalars(select(Memory))).all() == []
        actions = (await db.scalars(select(AuditEvent.action))).all()
        assert "memory.refused_secret" in actions


async def test_extraction_and_tool_never_store_secrets(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.memory_reply = json.dumps(
        {"memories": [{"text": "The user's password is Tr0ub4dor&3.", "kind": "user"}]}
    )
    fake.turns = [
        [
            {
                "name": "remember",
                "arguments": {"fact": "The user's API key is sk-abcdefghijklmnopqrst"},
            }
        ],
        "I won't keep that.",
    ]
    await chat(
        client,
        auth_headers,
        "My password is Tr0ub4dor&3 and key sk-abcdefghijklmnopqrst",
        sessionmaker,
        gateway,
        settings,
        tool_profile="read_only",
    )
    async with sessionmaker() as db:
        assert (await db.scalars(select(Memory))).all() == []
        chunks = (await db.scalars(select(MemoryChunk.text))).all()
    assert chunks and all("Tr0ub4dor" not in c and "sk-abcdef" not in c for c in chunks)
    tool_result = fake.requests[-1]["messages"][-1]["content"]
    assert "secret" in tool_result


# ---------------------------------------------------------------------- categories


async def test_categories_importance_and_filters(client, auth_headers):
    await remember(client, auth_headers, "The user is called Robin.", always_use=True)
    fact = await remember(
        client,
        auth_headers,
        "The home lab runs Proxmox VE on a NUC.",
        category="semantic",
        importance=0.9,
        entities=[{"kind": "service", "name": "Proxmox VE"}, {"kind": "machine", "name": "NUC"}],
    )
    assert fact["category"] == "semantic"
    assert fact["importance"] == 0.9
    assert fact["confidence"] == 1.0
    assert {e["name"] for e in fact["entities"]} == {"Proxmox VE", "NUC"}
    robin = (await memories(client, auth_headers, category="user"))[0]
    assert robin["importance"] == 0.8  # always-use defaults higher
    assert [m["content"] for m in await memories(client, auth_headers, category="semantic")] == [
        "The home lab runs Proxmox VE on a NUC."
    ]
    changed = await client.patch(
        f"/api/v1/memories/{fact['id']}",
        json={"category": "procedural", "entities": [{"kind": "tool", "name": "Ansible"}]},
        headers=auth_headers,
    )
    assert changed.json()["category"] == "procedural"
    assert [e["name"] for e in changed.json()["entities"]] == ["Ansible"]
    names = {
        e["name"]
        for e in (await client.get("/api/v1/memory/entities", headers=auth_headers)).json()
    }
    assert names == {"Ansible"}  # orphaned entities are pruned


async def test_extraction_proposes_all_four_kinds_with_details(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.memory_extraction_max_per_turn = 5
    fake.memory_reply = json.dumps(
        {
            "memories": [
                {"text": "The user prefers concise answers.", "kind": "user", "always": True},
                {
                    "text": "The user's backups go to a Synology NAS.",
                    "kind": "semantic",
                    "importance": 0.8,
                    "entities": [{"kind": "machine", "name": "Synology NAS"}],
                },
                {
                    "text": "Fixed the Proxmox boot loop by disabling the old initramfs hook.",
                    "kind": "episodic",
                    "stated": False,
                },
                {
                    "text": "The user rebuilds with make build then make deploy.",
                    "kind": "procedural",
                },
                {"text": "Ignored: not a kind", "kind": "gossip", "importance": "high"},
            ]
        }
    )
    await chat(
        client,
        auth_headers,
        "We fixed the Proxmox boot loop, now let's back up to the Synology NAS",
        sessionmaker,
        gateway,
        settings,
    )
    prompt = fake.extraction_requests[0]["messages"][0]["content"]
    assert "user, semantic, episodic, procedural" in prompt
    proposed = {m["content"]: m for m in await memories(client, auth_headers, status="proposed")}
    assert proposed["The user prefers concise answers."]["always_use"] is True
    assert proposed["The user's backups go to a Synology NAS."]["category"] == "semantic"
    assert proposed["The user's backups go to a Synology NAS."]["importance"] == 0.8
    assert proposed["The user's backups go to a Synology NAS."]["entities"][0]["name"] == (
        "Synology NAS"
    )
    episode = proposed["Fixed the Proxmox boot loop by disabling the old initramfs hook."]
    assert episode["category"] == "episodic" and episode["occurred_at"]
    assert episode["confidence"] == 0.5  # inferred, not stated
    # A malformed kind falls back to user; a malformed importance to the default.
    assert proposed["Ignored: not a kind"]["category"] == "user"
    assert proposed["Ignored: not a kind"]["importance"] == 0.5


async def test_extraction_only_uses_enabled_kinds(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await client.put(
        "/api/v1/memory/settings", json={"extract_categories": ["user"]}, headers=auth_headers
    )
    fake.memory_reply = json.dumps(
        {
            "memories": [
                {"text": "The user likes short answers.", "kind": "user"},
                {"text": "The router is at 10.0.0.1.", "kind": "semantic"},
            ]
        }
    )
    await chat(
        client,
        auth_headers,
        "Keep it short, my router is at 10.0.0.1",
        sessionmaker,
        gateway,
        settings,
    )
    assert "(use only these: user)" in fake.extraction_requests[0]["messages"][0]["content"]
    proposed = await memories(client, auth_headers, status="proposed")
    assert [m["content"] for m in proposed] == ["The user likes short answers."]
    got = (await client.get("/api/v1/memory/settings", headers=auth_headers)).json()
    assert got["extract_categories"] == ["user"]


async def test_episodic_and_unconfirmed_memories_are_labelled_in_the_prompt(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await remember(
        client,
        auth_headers,
        "Fixed the nginx certificate renewal by restarting certbot timer.",
        category="episodic",
    )
    await chat(
        client, auth_headers, "The nginx certificate expired again", sessionmaker, gateway, settings
    )
    prompt = system_prompt(fake)
    line = next(line for line in prompt.splitlines() if "certbot" in line)
    assert "(episodic, 20" in line


# ---------------------------------------------------------------------- contradictions


def test_disagreement_heuristic():
    assert disagreement("The user's NAS runs TrueNAS.", "The user's NAS runs Unraid.", None)
    assert disagreement("The server has 32 GB of RAM.", "The server has 64 GB of RAM.", None)
    assert disagreement("The user drinks coffee.", "The user does not drink coffee.", None)
    assert disagreement("The user lives in Oslo.", "The user lives in Bergen.", None)
    # Different things, not contradictions.
    assert disagreement("The user likes tea.", "The user likes cats.", None) is None
    assert disagreement("The user is a nurse.", "The user is called Robin.", None) is None
    assert disagreement("The user is called Robin.", "The user prefers metric units.", None) is None


async def test_conflicting_memory_is_flagged_and_resolved(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    old = await remember(client, auth_headers, "The user's NAS runs TrueNAS.")
    new = await remember(client, auth_headers, "The user's NAS runs Unraid.")
    assert new["open_conflicts"] == 1
    conflicts = (await client.get("/api/v1/memory/conflicts", headers=auth_headers)).json()
    assert len(conflicts) == 1
    assert conflicts[0]["memory"]["id"] == new["id"]
    assert conflicts[0]["other"]["id"] == old["id"]

    resolved = await client.post(
        f"/api/v1/memory/conflicts/{conflicts[0]['id']}/resolve",
        json={"decision": "keep_new"},
        headers=auth_headers,
    )
    assert resolved.status_code == 204
    assert (await client.get("/api/v1/memory/conflicts", headers=auth_headers)).json() == []
    superseded = await memories(client, auth_headers, status="superseded")
    assert [m["id"] for m in superseded] == [old["id"]]
    assert superseded[0]["superseded_by_id"] == new["id"]

    await chat(
        client, auth_headers, "Which NAS software do I run?", sessionmaker, gateway, settings
    )
    prompt = system_prompt(fake)
    assert "Unraid" in prompt and "TrueNAS" not in prompt
    # A superseded fact can become true again.
    again = await client.post(
        "/api/v1/memories", json={"content": "The user's NAS runs TrueNAS."}, headers=auth_headers
    )
    assert again.status_code == 201


async def test_keep_old_rejects_a_proposed_contradiction(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await remember(client, auth_headers, "The user lives in Oslo.")
    fake.memory_reply = '{"memories": [{"text": "The user lives in Bergen.", "kind": "user"}]}'
    await chat(client, auth_headers, "Visiting Bergen this week", sessionmaker, gateway, settings)
    conflict = (await client.get("/api/v1/memory/conflicts", headers=auth_headers)).json()[0]
    assert conflict["memory"]["status"] == "proposed"
    await client.post(
        f"/api/v1/memory/conflicts/{conflict['id']}/resolve",
        json={"decision": "keep_old"},
        headers=auth_headers,
    )
    assert await memories(client, auth_headers, status="proposed") == []
    assert [m["content"] for m in await memories(client, auth_headers)] == [
        "The user lives in Oslo."
    ]


async def test_extraction_correction_supersedes_on_approval(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    old = await remember(client, auth_headers, "The user works at Acme as a developer.")
    fake.memory_reply = json.dumps(
        {"memories": [{"text": "The user works at Initech now.", "kind": "user", "replaces": "M1"}]}
    )
    await chat(
        client, auth_headers, "I changed jobs, I'm at Initech now", sessionmaker, gateway, settings
    )
    assert "M1: The user works at Acme" in fake.extraction_requests[0]["messages"][1]["content"]
    proposed = await memories(client, auth_headers, status="proposed")
    assert proposed[0]["open_conflicts"] == 1
    await client.post(
        f"/api/v1/memories/{proposed[0]['id']}/approve", json={}, headers=auth_headers
    )
    active = await memories(client, auth_headers)
    assert [m["content"] for m in active] == ["The user works at Initech now."]
    assert active[0]["confidence"] == 1.0  # approved: confirmed
    superseded = await memories(client, auth_headers, status="superseded")
    assert [m["id"] for m in superseded] == [old["id"]]


async def test_user_correction_through_the_api_supersedes_at_once(client, auth_headers):
    old = await remember(client, auth_headers, "The user has two children.")
    await remember(client, auth_headers, "The user has three children.", replaces=old["id"])
    assert [m["content"] for m in await memories(client, auth_headers)] == [
        "The user has three children."
    ]


async def test_remember_tool_can_correct_with_an_id(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await client.put(
        "/api/v1/memory/settings", json={"require_review": False}, headers=auth_headers
    )
    old = await remember(client, auth_headers, "The user's editor is Vim.")
    fake.turns = [
        [
            {
                "name": "remember",
                "arguments": {
                    "fact": "The user's editor is Helix.",
                    "replaces": old["id"],
                    "about": ["tool:Helix"],
                },
            }
        ],
        "Updated.",
    ]
    await chat(
        client,
        auth_headers,
        "I switched to Helix",
        sessionmaker,
        gateway,
        settings,
        tool_profile="read_only",
    )
    active = await memories(client, auth_headers)
    assert [m["content"] for m in active] == ["The user's editor is Helix."]
    assert active[0]["entities"][0] == {**active[0]["entities"][0], "kind": "tool", "name": "Helix"}


# ---------------------------------------------------------------------- ranking


def test_recency_halves_every_half_life():
    now = datetime(2026, 11, 5, tzinfo=UTC)
    assert recency(now, now) == 1.0
    assert recency(now - timedelta(days=30), now, 30) == pytest.approx(0.5)
    assert recency(None, now) == 0.0


def test_combine_uses_configured_weights_and_missing_semantic():
    weights = Weights(semantic=0.5, keyword=0.25, recency=0.15, importance=0.10)
    full = combine(Signals(semantic=1, keyword=1, recency=1, importance=1), weights)
    assert full == pytest.approx(1.0)
    # Without embeddings the score is still on 0..1 (semantic weight left out).
    keyword_only = combine(Signals(semantic=None, keyword=1, recency=1, importance=1), weights)
    assert keyword_only == pytest.approx(1.0)
    low = combine(Signals(semantic=1, keyword=1, recency=1, importance=1, confidence=0), weights)
    assert low == pytest.approx(0.5)
    importance_only = Weights(semantic=0, keyword=0, recency=0, importance=1)
    assert combine(Signals(0.9, 0.9, 0.9, 0.2), importance_only) == pytest.approx(0.2)


async def test_importance_orders_equally_relevant_memories(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.memory_prompt_max_items = 1
    await remember(client, auth_headers, "The garden has tomatoes.", importance=0.1)
    await remember(client, auth_headers, "The garden has roses.", importance=1.0)
    await chat(client, auth_headers, "What grows in the garden?", sessionmaker, gateway, settings)
    prompt = system_prompt(fake)
    assert "roses" in prompt and "tomatoes" not in prompt


async def test_low_confidence_memories_are_never_given(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    memory = await remember(client, auth_headers, "The user maybe owns a kayak.")
    async with sessionmaker() as db:
        row = await db.get(Memory, uuid.UUID(memory["id"]))
        row.confidence = 0.1
        await db.commit()
    await chat(client, auth_headers, "Tell me about my kayak", sessionmaker, gateway, settings)
    assert "kayak" not in system_prompt(fake)
    settings.memory_min_confidence = 0.05
    await chat(client, auth_headers, "Tell me about my kayak", sessionmaker, gateway, settings)
    assert "kayak. (unconfirmed)" in system_prompt(fake)


async def test_hybrid_search_endpoint(client, auth_headers, with_embeddings):
    await remember(client, auth_headers, "The home lab runs Proxmox on a NUC.", category="semantic")
    await remember(client, auth_headers, "The user likes hiking in the mountains.")
    await remember(
        client, auth_headers, "Proxmox backups run nightly at 2am.", category="procedural"
    )
    hits = (
        await client.get("/api/v1/memories/search", params={"q": "proxmox"}, headers=auth_headers)
    ).json()
    contents = [h["memory"]["content"] for h in hits]
    assert "The user likes hiking in the mountains." not in contents
    assert set(contents) == {
        "The home lab runs Proxmox on a NUC.",
        "Proxmox backups run nightly at 2am.",
    }
    assert all(h["semantic"] is not None and h["score"] > 0 for h in hits)
    only = (
        await client.get(
            "/api/v1/memories/search",
            params={"q": "proxmox", "category": "procedural"},
            headers=auth_headers,
        )
    ).json()
    assert [h["memory"]["category"] for h in only] == ["procedural"]


async def test_search_memory_tool_filters_by_kind_and_entity(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    await remember(
        client,
        auth_headers,
        "Fixed the Proxmox boot loop by removing a stale initramfs hook.",
        category="episodic",
        entities=[{"kind": "service", "name": "Proxmox"}],
    )
    await remember(client, auth_headers, "The Proxmox host has 64 GB of RAM.", category="semantic")
    fake.turns = [
        [{"name": "search_memory", "arguments": {"query": "proxmox", "kinds": ["episodic"]}}],
        "Done.",
    ]
    await chat(
        client,
        auth_headers,
        "What have we tried on Proxmox?",
        sessionmaker,
        gateway,
        settings,
        tool_profile="read_only",
    )
    result = fake.requests[-1]["messages"][-1]["content"]
    assert "initramfs" in result and "64 GB" not in result
    assert "Memory ids (for corrections)" in result

    fake.turns = [
        [{"name": "search_memory", "arguments": {"query": "host", "about": "proxmox"}}],
        "Done.",
    ]
    await chat(
        client,
        auth_headers,
        "Proxmox host details?",
        sessionmaker,
        gateway,
        settings,
        tool_profile="read_only",
    )
    result = fake.requests[-1]["messages"][-1]["content"]
    assert "64 GB" not in result  # not linked to the Proxmox entity


# ---------------------------------------------------------------------- entities


async def test_entities_and_relations(client, auth_headers):
    await remember(
        client,
        auth_headers,
        "Celius API runs in an LXC container on Proxmox.",
        category="semantic",
        entities=[
            {"kind": "service", "name": "Celius API"},
            {"kind": "machine", "name": "Proxmox"},
        ],
    )
    edge = await client.post(
        "/api/v1/memory/relations",
        json={
            "source": {"kind": "service", "name": "celius api"},  # same entity, any case
            "relation": "runs on",
            "target": {"kind": "machine", "name": "Proxmox"},
        },
        headers=auth_headers,
    )
    assert edge.status_code == 201
    assert edge.json()["relation"] == "runs_on"
    assert edge.json()["source"]["name"] == "Celius API"
    entities = (await client.get("/api/v1/memory/entities", headers=auth_headers)).json()
    assert {(e["name"], e["memories"]) for e in entities} == {("Celius API", 1), ("Proxmox", 1)}
    proxmox = next(e for e in entities if e["name"] == "Proxmox")
    detail = (
        await client.get(f"/api/v1/memory/entities/{proxmox['id']}", headers=auth_headers)
    ).json()
    assert detail["memory_items"][0]["content"].startswith("Celius API runs")
    assert detail["relations"][0]["target"]["name"] == "Proxmox"
    removed = await client.delete(
        f"/api/v1/memory/relations/{edge.json()['id']}", headers=auth_headers
    )
    assert removed.status_code == 204


async def test_entities_are_private(client, auth_headers, sessionmaker):
    from app.auth.passwords import hash_password
    from app.models import User
    from tests.conftest import PASSWORD

    await remember(
        client,
        auth_headers,
        "The router is a UniFi gateway.",
        entities=[{"kind": "machine", "name": "UniFi"}],
    )
    entity = (await client.get("/api/v1/memory/entities", headers=auth_headers)).json()[0]
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password(PASSWORD)))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": PASSWORD}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/memory/entities", headers=other)).json() == []
    assert (
        await client.get(f"/api/v1/memory/entities/{entity['id']}", headers=other)
    ).status_code == 404


# ---------------------------------------------------------------------- re-embedding


async def test_reembed_after_the_embedding_model_changes(
    client, auth_headers, sessionmaker, settings, with_embeddings
):
    await remember(client, auth_headers, "The user plays the cello.")
    async with sessionmaker() as db:
        row = (await db.scalars(select(Memory))).one()
        assert row.embedding_model == "default"
    settings.embedding_model = "nomic-embed-v2"
    queued = await client.post("/api/v1/memory/reembed", headers=auth_headers)
    assert queued.status_code == 202
    await drain_jobs(sessionmaker, settings)
    async with sessionmaker() as db:
        row = (await db.scalars(select(Memory))).one()
        assert row.embedding_model == "nomic-embed-v2"
        assert row.status == MemoryStatus.active


async def test_reembed_needs_embeddings(client, auth_headers):
    response = await client.post("/api/v1/memory/reembed", headers=auth_headers)
    assert response.status_code == 409

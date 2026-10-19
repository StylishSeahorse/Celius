import uuid

import pytest
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.models import DeviceSyncState, User
from tests.fakes import FakeLlama, drain_runs
from tests.test_chat import new_conversation, send


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def changes(client, headers, cursor=0, limit=500) -> dict:
    response = await client.get(
        f"/api/v1/sync/changes?cursor={cursor}&limit={limit}", headers=headers
    )
    assert response.status_code == 200
    return response.json()


def kinds(feed: dict) -> list[tuple[str, str]]:
    return [(c["entity_type"], c["operation"]) for c in feed["changes"]]


async def test_every_durable_write_appears_in_the_feed(
    client, auth_headers, sessionmaker, gateway, settings
):
    start = (await client.get("/api/v1/sync/cursor", headers=auth_headers)).json()["cursor"]

    conv = await new_conversation(client, auth_headers)
    feed = await changes(client, auth_headers, start)
    assert kinds(feed) == [("conversation", "upsert")]
    assert feed["changes"][0]["entity_id"] == conv["id"]

    sent = (await send(client, auth_headers, conv["id"])).json()
    feed = await changes(client, auth_headers, feed["cursor"])
    assert sorted(kinds(feed)) == [
        ("conversation", "upsert"),
        ("message", "upsert"),
        ("message", "upsert"),
    ]
    for change in feed["changes"]:
        if change["entity_type"] == "message":
            assert change["payload"]["conversation_id"] == conv["id"]

    await drain_runs(sessionmaker, gateway, settings)
    feed = await changes(client, auth_headers, feed["cursor"])
    assert kinds(feed) == [("message", "upsert")]
    assert feed["changes"][0]["entity_id"] == sent["assistant_message"]["id"]

    await client.patch(
        f"/api/v1/conversations/{conv['id']}", json={"title": "x"}, headers=auth_headers
    )
    await client.delete(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)
    feed = await changes(client, auth_headers, feed["cursor"])
    assert kinds(feed) == [("conversation", "upsert"), ("conversation", "delete")]

    # Nothing new: the cursor stays put.
    again = await changes(client, auth_headers, feed["cursor"])
    assert again == {"changes": [], "cursor": feed["cursor"], "has_more": False}


async def test_cancel_and_failure_are_synced(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers)
    run = (await send(client, auth_headers, conv["id"])).json()["run"]
    cursor = (await client.get("/api/v1/sync/cursor", headers=auth_headers)).json()["cursor"]
    await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=auth_headers)
    assert ("message", "upsert") in kinds(await changes(client, auth_headers, cursor))

    fake.fail_status = 400
    await send(client, auth_headers, conv["id"])
    cursor = (await client.get("/api/v1/sync/cursor", headers=auth_headers)).json()["cursor"]
    await drain_runs(sessionmaker, gateway, settings)
    assert kinds(await changes(client, auth_headers, cursor)) == [("message", "upsert")]


async def test_feed_pages_and_is_private(client, auth_headers, sessionmaker):
    for _ in range(3):
        await new_conversation(client, auth_headers)
    page = await changes(client, auth_headers, 0, limit=2)
    assert len(page["changes"]) == 2 and page["has_more"] is True
    rest = await changes(client, auth_headers, page["cursor"], limit=2)
    assert len(rest["changes"]) == 1 and rest["has_more"] is False

    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password("other-password-1")))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": "other-password-1"}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await changes(client, other, 0))["changes"] == []
    assert (await client.get("/api/v1/sync/cursor", headers=other)).json() == {"cursor": 0}


async def test_ack_never_moves_backwards(client, auth_headers, sessionmaker):
    for cursor in (5, 9, 3):
        response = await client.post(
            "/api/v1/sync/ack", json={"device_id": "phone", "cursor": cursor}, headers=auth_headers
        )
        assert response.status_code == 204
    async with sessionmaker() as db:
        state = (await db.scalars(select(DeviceSyncState))).one()
    assert state.last_ack_cursor == 9


async def test_conversation_create_with_client_id_is_idempotent(client, auth_headers):
    conversation_id = str(uuid.uuid4())
    first = await client.post(
        "/api/v1/conversations",
        json={"id": conversation_id, "title": "Offline"},
        headers=auth_headers,
    )
    retry = await client.post(
        "/api/v1/conversations",
        json={"id": conversation_id, "title": "Offline"},
        headers=auth_headers,
    )
    assert first.status_code == 201 and retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"] == conversation_id
    listed = (await client.get("/api/v1/conversations", headers=auth_headers)).json()["items"]
    assert [c["id"] for c in listed] == [conversation_id]

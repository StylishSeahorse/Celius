"""Phase 10 (A-01, A-02, A-03): invitations, managing people, per-user limits."""

import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import AuditEvent, Conversation, StoredFile, User
from tests.fakes import FakeLlama, drain_runs
from tests.test_files import upload
from tests.test_sharing import PASSWORD, make_project, person


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def invite(client, headers, **body) -> dict:
    response = await client.post("/api/v1/admin/invitations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def token_of(invitation: dict) -> str:
    return invitation["url"].rsplit("/join/", 1)[1]


async def join(client, token, username="robin", password=PASSWORD, **extra):
    response = await client.post(
        "/api/v1/auth/register",
        json={"token": token, "username": username, "password": password, **extra},
    )
    client.cookies.clear()
    return response


# ---------------------------------------------------------------------- invitations


async def test_an_invitation_creates_one_account(client, auth_headers, sessionmaker):
    invitation = await invite(client, auth_headers, note="For Robin")
    assert invitation["state"] == "open" and "/#/join/" in invitation["url"]
    token = token_of(invitation)

    check = await client.get(f"/api/v1/auth/invitations/{token}")
    assert check.status_code == 200 and check.json()["note"] == "For Robin"

    joined = await join(client, token, username="Robin", display_name="Robin H")
    assert joined.status_code == 201, joined.text
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {joined.json()['access_token']}"}
    )
    assert me.json()["username"] == "robin" and me.json()["is_admin"] is False

    # Used: it can't be used again, or checked.
    again = await join(client, token, username="robin2")
    assert again.status_code == 404 and again.json()["error"]["code"] == "invitation_invalid"
    assert (await client.get(f"/api/v1/auth/invitations/{token}")).status_code == 404
    listed = (await client.get("/api/v1/admin/invitations", headers=auth_headers)).json()
    assert listed[0]["state"] == "used" and listed[0]["used_by"] == "robin"
    assert listed[0]["url"] is None  # the link is shown only once
    async with sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "auth.registered")
            )
            == 1
        )


async def test_bad_invitations_and_usernames_are_refused(client, auth_headers):
    assert (await join(client, "x" * 43)).status_code == 404  # unknown token
    revoked = await invite(client, auth_headers)
    await client.delete(f"/api/v1/admin/invitations/{revoked['id']}", headers=auth_headers)
    assert (await join(client, token_of(revoked))).status_code == 404

    token = token_of(await invite(client, auth_headers))
    assert (await join(client, token, username="owner")).status_code == 409  # taken
    assert (await join(client, token, username="OWNER")).status_code == 409  # case-insensitive
    assert (await join(client, token, username="a b")).status_code == 422
    assert (await join(client, token, password="short")).status_code == 422
    assert (await join(client, token, username="fine")).status_code == 201  # still usable


async def test_only_admins_manage_people(client, auth_headers, sessionmaker):
    sam = await person(client, sessionmaker, "sam")
    for method, url in [
        ("GET", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/invitations"),
        ("POST", "/api/v1/admin/invitations"),
    ]:
        response = await client.request(method, url, json={}, headers=sam)
        assert response.status_code == 403, (method, url)


async def test_an_admin_invitation_makes_an_admin(client, auth_headers):
    token = token_of(await invite(client, auth_headers, make_admin=True))
    joined = await join(client, token, username="second-admin")
    headers = {"Authorization": f"Bearer {joined.json()['access_token']}"}
    assert (await client.get("/api/v1/admin/users", headers=headers)).status_code == 200


# ---------------------------------------------------------------------- management


async def test_disable_enable_and_reset(client, auth_headers, sessionmaker):
    sam = await person(client, sessionmaker, "sam")
    people = (await client.get("/api/v1/admin/users", headers=auth_headers)).json()
    sam_id = next(p["id"] for p in people if p["username"] == "sam")

    off = await client.patch(
        f"/api/v1/admin/users/{sam_id}", json={"is_active": False}, headers=auth_headers
    )
    assert off.status_code == 200 and off.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me", headers=sam)).status_code == 401  # signed out
    login = await client.post("/api/v1/auth/login", json={"username": "sam", "password": PASSWORD})
    assert login.status_code == 401

    await client.patch(
        f"/api/v1/admin/users/{sam_id}", json={"is_active": True}, headers=auth_headers
    )
    reset = await client.post(
        f"/api/v1/admin/users/{sam_id}/password",
        json={"new_password": "a new password for sam"},
        headers=auth_headers,
    )
    assert reset.status_code == 204
    login = await client.post(
        "/api/v1/auth/login", json={"username": "sam", "password": "a new password for sam"}
    )
    assert login.status_code == 200


async def test_admins_cannot_lock_themselves_out(client, auth_headers):
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()["user_id"]
    for change in ({"is_admin": False}, {"is_active": False}):
        response = await client.patch(
            f"/api/v1/admin/users/{me}", json=change, headers=auth_headers
        )
        assert response.status_code == 409
    deleted = await client.delete(f"/api/v1/admin/users/{me}?confirm=owner", headers=auth_headers)
    assert deleted.status_code == 409


async def test_deleting_someone_removes_their_data_only(
    client, auth_headers, sessionmaker, settings, gateway
):
    sam = await person(client, sessionmaker, "sam")
    sam_id = (await client.get("/api/v1/auth/me", headers=sam)).json()["user_id"]
    # Sam has a chat, a file (same content as one of the owner's), and a project shared
    # with the owner, who has a chat in it.
    conv = (await client.post("/api/v1/conversations", json={}, headers=sam)).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Hi"},
        headers=sam,
    )
    await drain_runs(sessionmaker, gateway, settings)
    same = b"identical content"
    assert (await upload(client, sam, "a.txt", same)).status_code == 201
    assert (await upload(client, auth_headers, "b.txt", same)).status_code == 201
    unique = await upload(client, sam, "only-sam.txt", b"only sam has this")
    project = await make_project(client, sam)
    await client.put(
        f"/api/v1/projects/{project['id']}/members/owner", json={"role": "read"}, headers=sam
    )
    owner_chat = (
        await client.post(
            "/api/v1/conversations", json={"project_id": project["id"]}, headers=auth_headers
        )
    ).json()
    workspace = Path(settings.workspace_root) / conv["id"]
    workspace.mkdir(parents=True)
    (workspace / "out.txt").write_text("x")

    wrong = await client.delete(f"/api/v1/admin/users/{sam_id}?confirm=nope", headers=auth_headers)
    assert wrong.status_code == 422
    done = await client.delete(f"/api/v1/admin/users/{sam_id}?confirm=sam", headers=auth_headers)
    assert done.status_code == 200 and done.json()["files"] == 2

    async with sessionmaker() as db:
        assert await db.get(User, __import__("uuid").UUID(sam_id)) is None
        left = (await db.scalars(select(StoredFile.filename))).all()
        assert left == ["b.txt"]
        shas = (await db.scalars(select(StoredFile.sha256))).all()
    blobs = {p.name for p in (Path(settings.storage_root) / "blobs").rglob("*") if p.is_file()}
    assert set(shas) == blobs  # shared content kept, Sam's own content gone
    assert not workspace.exists()
    assert unique.status_code == 201
    # The owner's chat in Sam's project survives, unfiled.
    kept = await client.get(f"/api/v1/conversations/{owner_chat['id']}", headers=auth_headers)
    assert kept.status_code == 200 and kept.json()["project_id"] is None
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Conversation)) == 1


# ---------------------------------------------------------------------- limits


async def _sam_id(client, sam) -> str:
    return (await client.get("/api/v1/auth/me", headers=sam)).json()["user_id"]


async def test_storage_quota_per_person(client, auth_headers, sessionmaker):
    sam = await person(client, sessionmaker, "sam")
    sam_id = await _sam_id(client, sam)
    await client.patch(
        f"/api/v1/admin/users/{sam_id}", json={"storage_quota_bytes": 100}, headers=auth_headers
    )
    assert (await client.get("/api/v1/files/usage", headers=sam)).json()["limit_bytes"] == 100
    refused = await upload(client, sam, "big.txt", b"x" * 101)
    assert refused.status_code == 413 and refused.json()["error"]["code"] == "quota_exceeded"
    assert (await upload(client, auth_headers, "big.txt", b"x" * 101)).status_code == 201


async def test_daily_reply_limit(client, auth_headers, sessionmaker, settings, gateway):
    sam = await person(client, sessionmaker, "sam")
    sam_id = await _sam_id(client, sam)
    await client.patch(
        f"/api/v1/admin/users/{sam_id}", json={"daily_message_limit": 2}, headers=auth_headers
    )
    conv = (await client.post("/api/v1/conversations", json={}, headers=sam)).json()

    async def say(text):
        return await client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"client_message_id": os.urandom(8).hex(), "content": text},
            headers=sam,
        )

    for text in ("one", "two"):
        assert (await say(text)).status_code == 201
        await drain_runs(sessionmaker, gateway, settings)
    third = await say("three")
    assert third.status_code == 429 and third.json()["error"]["code"] == "daily_limit"

    await client.patch(
        f"/api/v1/admin/users/{sam_id}", json={"daily_message_limit": None}, headers=auth_headers
    )
    assert (await say("three")).status_code == 201


async def test_allowed_models(client, auth_headers, sessionmaker, settings, gateway, fake):
    sam = await person(client, sessionmaker, "sam")
    sam_id = await _sam_id(client, sam)
    await client.patch(
        f"/api/v1/admin/users/{sam_id}",
        json={"allowed_models": ["small-model"]},
        headers=auth_headers,
    )
    models = (await client.get("/api/v1/models", headers=sam)).json()
    assert models["default_model_id"] == "small-model"
    assert all(m["id"] == "small-model" for m in models["models"])

    conv = (await client.post("/api/v1/conversations", json={}, headers=sam)).json()
    asked = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Hi", "model": "local-model"},
        headers=sam,
    )
    assert asked.status_code == 403 and asked.json()["error"]["code"] == "model_not_allowed"
    default = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Hi"},
        headers=sam,
    )
    assert default.status_code == 201
    assert default.json()["run"]["model_id"] == "small-model"


async def test_allowed_tool_profiles(client, auth_headers, sessionmaker, settings, gateway, fake):
    sam = await person(client, sessionmaker, "sam")
    sam_id = await _sam_id(client, sam)
    bad = await client.patch(
        f"/api/v1/admin/users/{sam_id}",
        json={"allowed_tool_profiles": ["superuser"]},
        headers=auth_headers,
    )
    assert bad.status_code == 422
    await client.patch(
        f"/api/v1/admin/users/{sam_id}",
        json={"allowed_tool_profiles": ["standard"]},
        headers=auth_headers,
    )
    refused = await client.post(
        "/api/v1/conversations", json={"tool_profile": "developer"}, headers=sam
    )
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "profile_not_allowed"

    # A chat whose profile was set before the limit is narrowed when it runs.
    await client.patch(
        f"/api/v1/admin/users/{sam_id}", json={"allowed_tool_profiles": None}, headers=auth_headers
    )
    conv = (
        await client.post("/api/v1/conversations", json={"tool_profile": "developer"}, headers=sam)
    ).json()
    await client.patch(
        f"/api/v1/admin/users/{sam_id}",
        json={"allowed_tool_profiles": ["standard"]},
        headers=auth_headers,
    )
    fake.turns = ["ok"]
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Write a file"},
        headers=sam,
    )
    await drain_runs(sessionmaker, gateway, settings)
    offered = {t["function"]["name"] for t in fake.requests[-1].get("tools") or []}
    assert "write_file" in offered  # standard tools (asks before writing)
    assert "run_shell" not in offered  # developer-only

"""Phase 10 (A-04): sharing a project with other people."""

import os

import pytest

from app.auth.passwords import hash_password
from app.models import User
from tests.fakes import FakeLlama, drain_jobs, drain_runs
from tests.test_files import upload

PASSWORD = "a password for tests"


async def person(client, sessionmaker, username, **fields) -> dict:
    """Create a user and sign in; returns bearer headers."""
    async with sessionmaker() as db:
        db.add(User(username=username, password_hash=hash_password(PASSWORD), **fields))
        await db.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def make_project(client, headers, **body) -> dict:
    created = await client.post(
        "/api/v1/projects",
        json={"name": "Family trip", "instructions": "We travel by train.", **body},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_a_shared_project_is_usable_but_chats_stay_private(
    client, auth_headers, sessionmaker, settings, gateway, fake
):
    sam = await person(client, sessionmaker, "sam")
    project = await make_project(client, auth_headers)
    pid = project["id"]
    notes = await upload(client, auth_headers, "plan.md", b"Day 1: Lisbon", project_id=pid)
    assert notes.status_code == 201
    await drain_jobs(sessionmaker, settings)

    # Not shared yet: invisible.
    assert (await client.get(f"/api/v1/projects/{pid}", headers=sam)).status_code == 404
    assert (await client.get("/api/v1/projects", headers=sam)).json() == []

    shared = await client.put(
        f"/api/v1/projects/{pid}/members/sam", json={"role": "read"}, headers=auth_headers
    )
    assert shared.status_code == 200 and shared.json()["role"] == "read"
    (listed,) = (await client.get("/api/v1/projects", headers=sam)).json()
    assert listed["role"] == "read" and listed["owner"] == "owner" and listed["shared"]
    mine = (await client.get(f"/api/v1/projects/{pid}", headers=auth_headers)).json()
    assert mine["role"] == "owner" and mine["shared"]

    # Sam sees the project's files and can read them…
    files = (await client.get(f"/api/v1/files?project_id={pid}", headers=sam)).json()
    assert [f["filename"] for f in files] == ["plan.md"]
    text = await client.get(f"/api/v1/files/{files[0]['id']}/text", headers=sam)
    assert "Lisbon" in text.json()["text"]
    # …and chats in it get its instructions and files.
    conv = (
        await client.post("/api/v1/conversations", json={"project_id": pid}, headers=sam)
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Where first?"},
        headers=sam,
    )
    await drain_runs(sessionmaker, gateway, settings)
    system = fake.requests[-1]["messages"][0]["content"]
    assert "We travel by train." in system and "Day 1: Lisbon" in system

    # But each person's chats are their own.
    assert (
        await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)
    ).status_code == 404
    owner_list = (
        await client.get(f"/api/v1/conversations?project_id={pid}", headers=auth_headers)
    ).json()
    assert owner_list["items"] == []


async def test_read_members_cannot_change_write_members_can(client, auth_headers, sessionmaker):
    sam = await person(client, sessionmaker, "sam")
    alex = await person(client, sessionmaker, "alex")
    pid = (await make_project(client, auth_headers))["id"]
    await client.put(
        f"/api/v1/projects/{pid}/members/sam", json={"role": "read"}, headers=auth_headers
    )
    await client.put(
        f"/api/v1/projects/{pid}/members/alex", json={"role": "write"}, headers=auth_headers
    )

    change = {"instructions": "We fly."}
    denied = await client.patch(f"/api/v1/projects/{pid}", json=change, headers=sam)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "project_read_only"
    assert (await upload(client, sam, "x.md", b"x", project_id=pid)).status_code == 403

    allowed = await client.patch(f"/api/v1/projects/{pid}", json=change, headers=alex)
    assert allowed.status_code == 200 and allowed.json()["instructions"] == "We fly."
    added = await upload(client, alex, "tickets.md", b"Train 7:05", project_id=pid)
    assert added.status_code == 201
    # Owner-only: archiving, memory, deleting, sharing.
    for method, url, body in [
        ("PATCH", f"/api/v1/projects/{pid}", {"archived": True}),
        ("PATCH", f"/api/v1/projects/{pid}", {"memory_enabled": False}),
        ("PUT", f"/api/v1/projects/{pid}/members/sam", {"role": "write"}),
        ("DELETE", f"/api/v1/projects/{pid}/members/sam", None),
        ("DELETE", f"/api/v1/projects/{pid}", None),
    ]:
        response = await client.request(method, url, json=body, headers=alex)
        assert response.status_code == 403, (method, url, response.status_code)

    # A write member may delete project files (also others'); a read member may not.
    file_id = added.json()["id"]
    assert (await client.delete(f"/api/v1/files/{file_id}", headers=sam)).status_code == 403
    assert (
        await client.delete(f"/api/v1/files/{file_id}", headers=auth_headers)
    ).status_code == 204


async def test_unsharing_and_leaving(client, auth_headers, sessionmaker):
    sam = await person(client, sessionmaker, "sam")
    pid = (await make_project(client, auth_headers))["id"]
    await client.put(
        f"/api/v1/projects/{pid}/members/sam", json={"role": "read"}, headers=auth_headers
    )
    chat = (
        await client.post("/api/v1/conversations", json={"project_id": pid}, headers=sam)
    ).json()
    members = (await client.get(f"/api/v1/projects/{pid}/members", headers=sam)).json()
    assert members == [{"username": "sam", "display_name": None, "role": "read"}]

    # Sam leaves: the project disappears for them, their chat stays (unfiled).
    assert (
        await client.delete(f"/api/v1/projects/{pid}/members/sam", headers=sam)
    ).status_code == 204
    assert (await client.get(f"/api/v1/projects/{pid}", headers=sam)).status_code == 404
    kept = (await client.get(f"/api/v1/conversations/{chat['id']}", headers=sam)).json()
    assert kept["project_id"] is None

    # Unknown or disabled people can't be added; the owner isn't a member of their own.
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/members/nobody", json={"role": "read"}, headers=auth_headers
        )
    ).status_code == 404
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/members/owner", json={"role": "read"}, headers=auth_headers
        )
    ).status_code == 409
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/members/sam", json={"role": "admin"}, headers=auth_headers
        )
    ).status_code == 422


async def test_members_hear_about_changes(client, auth_headers, sessionmaker):
    sam = await person(client, sessionmaker, "sam")
    pid = (await make_project(client, auth_headers))["id"]
    cursor = (await client.get("/api/v1/sync/cursor", headers=sam)).json()["cursor"]
    await client.put(
        f"/api/v1/projects/{pid}/members/sam", json={"role": "read"}, headers=auth_headers
    )
    await client.patch(f"/api/v1/projects/{pid}", json={"name": "Trip 2027"}, headers=auth_headers)
    changes = (await client.get(f"/api/v1/sync/changes?cursor={cursor}", headers=sam)).json()[
        "changes"
    ]
    assert [c["entity_id"] for c in changes if c["entity_type"] == "project"].count(pid) >= 2

import uuid

import pytest

from tests.fakes import FakeLlama, drain_runs
from tests.test_chat import new_conversation, send


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def test_project_crud_and_counts(client, auth_headers):
    created = await client.post(
        "/api/v1/projects",
        json={"name": "  Garden  ", "instructions": "Answer like a gardener."},
        headers=auth_headers,
    )
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "Garden"

    await new_conversation(client, auth_headers, project_id=project["id"])
    await new_conversation(client, auth_headers, project_id=project["id"])
    await new_conversation(client, auth_headers)

    listed = (await client.get("/api/v1/projects", headers=auth_headers)).json()
    assert [(p["name"], p["conversation_count"]) for p in listed] == [("Garden", 2)]

    in_project = await client.get(
        f"/api/v1/conversations?project_id={project['id']}", headers=auth_headers
    )
    assert len(in_project.json()["items"]) == 2

    patched = await client.patch(
        f"/api/v1/projects/{project['id']}", json={"name": "Allotment"}, headers=auth_headers
    )
    assert patched.json()["name"] == "Allotment"
    assert patched.json()["instructions"] == "Answer like a gardener."

    archived = await client.patch(
        f"/api/v1/projects/{project['id']}", json={"archived": True}, headers=auth_headers
    )
    assert archived.json()["archived"] is True
    assert (await client.get("/api/v1/projects", headers=auth_headers)).json() == []


async def test_project_create_with_client_id_is_idempotent(client, auth_headers):
    project_id = str(uuid.uuid4())
    body = {"id": project_id, "name": "Offline project"}
    first = await client.post("/api/v1/projects", json=body, headers=auth_headers)
    retry = await client.post("/api/v1/projects", json=body, headers=auth_headers)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert len((await client.get("/api/v1/projects", headers=auth_headers)).json()) == 1


async def test_project_instructions_reach_the_model(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    project = (
        await client.post(
            "/api/v1/projects",
            json={"name": "Pirates", "instructions": "Talk like a pirate."},
            headers=auth_headers,
        )
    ).json()
    conv = await new_conversation(client, auth_headers, project_id=project["id"])
    await send(client, auth_headers, conv["id"])
    await drain_runs(sessionmaker, gateway, settings)
    system = fake.requests[0]["messages"][0]
    assert system["role"] == "system" and "\n\nTalk like a pirate." in system["content"]


async def test_deleting_a_project_keeps_its_conversations(client, auth_headers):
    project = (
        await client.post("/api/v1/projects", json={"name": "Temp"}, headers=auth_headers)
    ).json()
    conv = await new_conversation(client, auth_headers, project_id=project["id"])
    cursor = (await client.get("/api/v1/sync/cursor", headers=auth_headers)).json()["cursor"]

    deleted = await client.delete(f"/api/v1/projects/{project['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert detail["project_id"] is None

    feed = (await client.get(f"/api/v1/sync/changes?cursor={cursor}", headers=auth_headers)).json()
    assert [(c["entity_type"], c["operation"]) for c in feed["changes"]] == [
        ("conversation", "upsert"),
        ("project", "delete"),
    ]


async def test_unknown_or_foreign_project_rejected(client, auth_headers):
    missing = str(uuid.uuid4())
    assert (
        await client.get(f"/api/v1/projects/{missing}", headers=auth_headers)
    ).status_code == 404
    response = await client.post(
        "/api/v1/conversations", json={"project_id": missing}, headers=auth_headers
    )
    assert response.status_code == 404

"""Phase 10 exit gate (A-05): one person can never reach another's data.

Alice creates one of everything: a project, a chat with a reply and a tool call waiting for
approval, files, an open upload, an export, a memory, a tool profile, a schedule and a
device for notifications. Bob, an ordinary signed-in user, then calls every API route that
takes an id with Alice's ids, and every listing. Nothing may succeed, nothing may leak, and
Alice's data must be unchanged afterwards.
"""

import os
import re

import pytest

from app.main import create_app
from tests.fakes import FakeLlama, drain_jobs, drain_runs
from tests.test_files import upload
from tests.test_push import Device
from tests.test_sharing import person

# Bodies that pass validation, so the ownership check is what answers.
BODIES = {
    ("POST", "/api/v1/conversations/{conversation_id}/messages"): {
        "client_message_id": "isolation-test-01",
        "content": "hi",
    },
    ("POST", "/api/v1/messages/{message_id}/revisions"): {
        "client_message_id": "isolation-test-02",
        "content": "edited",
    },
    ("POST", "/api/v1/messages/{message_id}/regenerate"): {"client_message_id": "iso-03-regen"},
    ("POST", "/api/v1/tool-calls/{call_id}/approve"): {"args_hash": "0" * 64},
    ("POST", "/api/v1/tool-calls/{call_id}/deny"): {"reason": "no"},
    ("POST", "/api/v1/memories/{memory_id}/approve"): {},
    ("PATCH", "/api/v1/projects/{project_id}"): {"name": "taken over"},
    ("PUT", "/api/v1/projects/{project_id}/members/{username}"): {"role": "write"},
    ("PATCH", "/api/v1/conversations/{conversation_id}"): {"title": "taken over"},
    ("PATCH", "/api/v1/memories/{memory_id}"): {"content": "taken over"},
    ("PATCH", "/api/v1/schedules/{task_id}"): {"name": "taken over"},
    ("PATCH", "/api/v1/tool-profiles/{profile_id}"): {"name": "taken over"},
    ("PATCH", "/api/v1/admin/users/{user_id}"): {"is_admin": True},
    ("POST", "/api/v1/admin/users/{user_id}/password"): {"new_password": "x" * 20},
}
# Required query parameters.
QUERIES = {
    ("DELETE", "/api/v1/admin/users/{user_id}"): "?confirm=alice",
    ("GET", "/api/v1/conversations/{conversation_id}/workspace/file"): "?path=a.txt",
}
# Not about someone's data.
SKIP = {
    "/api/v1/auth/invitations/{token}",  # public: checked by the invitation tests
    "/api/v1/models/{model_id}/verify-tools",
}


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def alices_world(client, sessionmaker, settings, gateway, fake, admin_headers) -> tuple:
    alice = await person(client, sessionmaker, "alice")
    ids: dict[str, str] = {"username": "alice"}
    ids["user_id"] = (await client.get("/api/v1/auth/me", headers=alice)).json()["user_id"]

    project = (await client.post("/api/v1/projects", json={"name": "Secret"}, headers=alice)).json()
    ids["project_id"] = project["id"]
    conv = (
        await client.post(
            "/api/v1/conversations",
            json={"project_id": project["id"], "tool_profile": "standard"},
            headers=alice,
        )
    ).json()
    ids["conversation_id"] = conv["id"]
    fake.turns = [[{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]]
    sent = (
        await client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"client_message_id": os.urandom(8).hex(), "content": "Alice's secret plan"},
            headers=alice,
        )
    ).json()
    ids["message_id"] = sent["message"]["id"]
    ids["run_id"] = sent["run"]["id"]
    await drain_runs(sessionmaker, gateway, settings)  # stops at the approval
    calls = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=alice)).json()
    ids["call_id"] = calls["messages"][-1]["tool_calls"][0]["id"]

    stored = await upload(client, alice, "diary.txt", b"Alice's diary", project_id=project["id"])
    ids["file_id"] = stored.json()["id"]
    open_upload = await client.post(
        "/api/v1/files/uploads", json={"filename": "later.txt", "size": 5}, headers=alice
    )
    ids["upload_id"] = open_upload.json()["id"]
    export = await client.post(
        "/api/v1/exports",
        json={"scope": "conversation", "id": conv["id"], "format": "markdown"},
        headers=alice,
    )
    ids["export_id"] = ids["job_id"] = export.json()["id"]
    memory = await client.post(
        "/api/v1/memories", json={"content": "Alice is allergic to cats"}, headers=alice
    )
    ids["memory_id"] = memory.json()["id"]
    profile = await client.post("/api/v1/tool-profiles", json={"name": "Mine"}, headers=alice)
    ids["profile_id"] = profile.json()["key"].removeprefix("custom:")
    schedule = await client.post(
        "/api/v1/schedules",
        json={"name": "Daily", "prompt": "Summarise", "kind": "daily", "time_of_day": "08:00"},
        headers=alice,
    )
    ids["task_id"] = schedule.json()["id"]
    await client.post("/api/v1/push/subscriptions", json=Device().subscription(), headers=alice)
    invitation = await client.post("/api/v1/admin/invitations", json={}, headers=admin_headers)
    ids["invitation_id"] = invitation.json()["id"]
    ids["server_id"] = "00000000-0000-4000-8000-00000000000a"  # admin-only routes
    ids["token"] = "x" * 43
    await drain_jobs(sessionmaker, settings)
    return alice, ids


def routes():
    for path, operations in create_app().openapi()["paths"].items():
        if "{" not in path or path in SKIP:
            continue
        for method in operations:
            yield method.upper(), path


async def test_nobody_reaches_anyone_elses_data(
    client, auth_headers, sessionmaker, settings, gateway, fake
):
    alice, ids = await alices_world(client, sessionmaker, settings, gateway, fake, auth_headers)
    bob = await person(client, sessionmaker, "bob")
    secrets = [v for k, v in ids.items() if k.endswith("_id")]

    reached = []
    for method, path in routes():
        url = re.sub(r"\{([^}]+)\}", lambda m: ids[m.group(1)], path)
        url += QUERIES.get((method, path), "")
        body = BODIES.get((method, path))
        if path.endswith("/content"):
            response = await client.request(method, url, content=b"hello", headers=bob)
        else:
            response = await client.request(method, url, json=body, headers=bob)
        if response.status_code < 400 or response.status_code >= 500:
            reached.append((method, path, response.status_code))
        elif response.status_code not in (403, 404, 409):
            reached.append((method, path, response.status_code, response.text[:120]))
        assert "Alice" not in response.text, (method, path)
    assert reached == [], reached

    # Listings show Bob nothing of Alice's.
    for url in [
        "/api/v1/projects",
        "/api/v1/conversations",
        "/api/v1/files",
        "/api/v1/memories",
        "/api/v1/tool-profiles",
        "/api/v1/schedules",
        "/api/v1/tool-calls?status=pending_approval",
        "/api/v1/activity",
        "/api/v1/sync/changes?cursor=0",
        "/api/v1/push",
        f"/api/v1/files?project_id={ids['project_id']}",
        f"/api/v1/conversations?project_id={ids['project_id']}",
    ]:
        response = await client.get(url, headers=bob)
        assert response.status_code in (200, 404), (url, response.status_code)
        for secret in secrets:
            assert secret not in response.text, (url, secret)
        assert "Alice" not in response.text and "Secret" not in response.text, url
    assert (await client.get("/api/v1/push", headers=bob)).json()["devices"] == 0

    # Alice's world is untouched.
    conv = await client.get(f"/api/v1/conversations/{ids['conversation_id']}", headers=alice)
    assert conv.status_code == 200 and conv.json()["title"] != "taken over"
    assert conv.json()["messages"][-1]["tool_calls"][0]["status"] == "pending_approval"
    assert (await client.get(f"/api/v1/files/{ids['file_id']}", headers=alice)).status_code == 200
    project = (await client.get(f"/api/v1/projects/{ids['project_id']}", headers=alice)).json()
    assert project["name"] == "Secret" and project["shared"] is False
    memories = (await client.get("/api/v1/memories", headers=alice)).json()
    assert [m["content"] for m in memories] == ["Alice is allergic to cats"]
    me = (await client.get("/api/v1/auth/me", headers=alice)).json()
    assert me["is_admin"] is False
    upload_state = await client.get(f"/api/v1/files/uploads/{ids['upload_id']}", headers=alice)
    assert upload_state.json()["received_bytes"] == 0


async def test_memory_recall_never_crosses_people_in_a_shared_project(
    client, auth_headers, sessionmaker, settings, gateway, fake
):
    """Sharing a project shares instructions and files, not what each person said."""
    alice = await person(client, sessionmaker, "alice")
    bob = await person(client, sessionmaker, "bob")
    project = (await client.post("/api/v1/projects", json={"name": "Home"}, headers=alice)).json()
    await client.put(
        f"/api/v1/projects/{project['id']}/members/bob", json={"role": "write"}, headers=alice
    )
    for who, text in ((alice, "My PIN is 4711"), (bob, "What is the PIN?")):
        conv = (
            await client.post(
                "/api/v1/conversations", json={"project_id": project["id"]}, headers=who
            )
        ).json()
        await client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"client_message_id": os.urandom(8).hex(), "content": text},
            headers=who,
        )
        await drain_runs(sessionmaker, gateway, settings)
        await drain_jobs(sessionmaker, settings)
    prompt = str(fake.requests[-1]["messages"])
    assert "4711" not in prompt

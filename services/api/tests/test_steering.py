"""Steering: messages sent while a reply is being written (ADR C-16)."""

import os
import uuid

import pytest
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.models import RunStatus, RunSteer, User
from app.services import steering
from app.worker.executor import STEER_NOTE
from tests.fakes import FakeLlama, drain_runs
from tests.test_tools import detail, run_status, start


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def steer(client, headers, run_id, text, client_id=None):
    return await client.post(
        f"/api/v1/runs/{run_id}/steer",
        json={"content": text, "client_id": client_id or os.urandom(8).hex()},
        headers=headers,
    )


def kinds(reply: dict) -> list[str]:
    return [p["type"] for p in reply["parts"]]


async def test_a_message_sent_before_the_first_turn_joins_the_request(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.reply = "Using Python 3.12."
    conv, sent = await start(client, auth_headers, "Write a script")
    queued = await steer(client, auth_headers, sent["run"]["id"], "Use Python 3.12")
    assert queued.status_code == 201, queued.text
    assert queued.json()["status"] == "pending"
    await drain_runs(sessionmaker, gateway, settings)

    [request] = fake.requests
    last = request["messages"][-1]
    assert last["role"] == "user"  # merged: roles still alternate
    assert last["content"].startswith("Write a script") and last["content"].endswith(
        f"{STEER_NOTE}\nUse Python 3.12"
    )
    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert kinds(reply) == ["steer", "text"]
    assert reply["parts"][0]["text"] == "Use Python 3.12"
    listed = (
        await client.get(f"/api/v1/runs/{sent['run']['id']}/steers", headers=auth_headers)
    ).json()
    assert [(s["status"], s["step"]) for s in listed] == [("delivered", 0)]


async def test_between_tool_steps_and_while_waiting_for_approval(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        [{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}],
        "Saved: 1 byte.",
    ]
    conv, sent = await start(client, auth_headers, "Save a file", tool_profile="standard")
    await drain_runs(sessionmaker, gateway, settings)
    run_id = sent["run"]["id"]
    assert await run_status(sessionmaker, run_id) == RunStatus.awaiting_approval

    queued = await steer(client, auth_headers, run_id, "Also tell me the file size")
    assert queued.json()["waits_for_approval"] is True
    [call] = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    await client.post(
        f"/api/v1/tool-calls/{call['id']}/approve",
        json={"args_hash": call["args_hash"]},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, run_id) == RunStatus.completed

    resumed = fake.requests[-1]["messages"]
    # The tool result, then the user's message, then the model's next turn.
    assert [m["role"] for m in resumed[-3:]] == ["assistant", "tool", "user"]
    assert resumed[-1]["content"] == f"{STEER_NOTE}\nAlso tell me the file size"
    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert kinds(reply) == ["tool_call", "steer", "text"]
    assert reply["parts"][1]["data"]["step"] == 1


async def test_a_message_sent_while_the_answer_streams_continues_the_reply(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = ["Here is a long answer in English.", "Voici la réponse en français."]
    conv, sent = await start(client, auth_headers, "Explain it")
    run_id = uuid.UUID(sent["run"]["id"])

    async def meanwhile():
        async with sessionmaker() as db:
            user = await db.scalar(select(User))
            await steering.add(db, user.id, run_id, "Actually, answer in French", "mid-1234")
            await db.commit()

    fake.before_text = meanwhile
    await drain_runs(sessionmaker, gateway, settings)
    assert await run_status(sessionmaker, run_id) == RunStatus.completed

    first, second = fake.requests
    assert [m["role"] for m in second["messages"][-2:]] == ["assistant", "user"]
    assert second["messages"][-2]["content"] == "Here is a long answer in English."
    assert second["messages"][-1]["content"].endswith("Actually, answer in French")
    reply = (await detail(client, auth_headers, conv["id"]))["messages"][-1]
    assert kinds(reply) == ["text", "steer", "text"]
    assert reply["parts"][-1]["text"] == "Voici la réponse en français."

    # Later turns remember it.
    fake.turns = ["Ok."]
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Thanks"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    history = [m for m in fake.requests[-1]["messages"] if m["role"] == "assistant"]
    assert "the user added: “Actually, answer in French”" in history[-1]["content"]


async def test_a_message_after_the_reply_ended_is_refused(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    _conv, sent = await start(client, auth_headers)
    await drain_runs(sessionmaker, gateway, settings)
    late = await steer(client, auth_headers, sent["run"]["id"], "Too late")
    assert late.status_code == 409 and late.json()["error"]["code"] == "run_finished"


async def test_duplicates_limits_and_withdrawing(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    _conv, sent = await start(client, auth_headers)
    run_id = sent["run"]["id"]
    first = await steer(client, auth_headers, run_id, "One", client_id="same-client-id")
    again = await steer(client, auth_headers, run_id, "One", client_id="same-client-id")
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    for i in range(steering.MAX_PENDING - 1):
        assert (await steer(client, auth_headers, run_id, f"More {i}")).status_code == 201
    over = await steer(client, auth_headers, run_id, "Too many")
    assert over.status_code == 409 and over.json()["error"]["code"] == "too_many_steers"
    assert (await steer(client, auth_headers, run_id, " ")).status_code == 422

    withdrawn = await client.delete(
        f"/api/v1/runs/{run_id}/steers/{first.json()['id']}", headers=auth_headers
    )
    assert withdrawn.json()["status"] == "withdrawn"
    await drain_runs(sessionmaker, gateway, settings)
    user = fake.requests[0]["messages"][-1]["content"]
    assert "One" not in user.split(STEER_NOTE)[1] and "More 0" in user
    async with sessionmaker() as db:
        delivered = (await db.scalars(select(RunSteer).where(RunSteer.status == "delivered"))).all()
    assert len(delivered) == steering.MAX_PENDING - 1
    read = await client.delete(
        f"/api/v1/runs/{run_id}/steers/{delivered[0].id}", headers=auth_headers
    )
    assert read.status_code == 409 and read.json()["error"]["code"] == "already_read"


async def test_only_the_owner_can_steer(client, auth_headers, sessionmaker):
    _conv, sent = await start(client, auth_headers)
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password("other-password-123")))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": "other-password-123"}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    run_id = sent["run"]["id"]
    assert (await steer(client, other, run_id, "Hijack")).status_code == 404
    assert (await client.get(f"/api/v1/runs/{run_id}/steers", headers=other)).status_code == 404


async def test_a_message_read_before_a_pause_survives_the_resume(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        [{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}],
        "Saved.",
    ]
    _conv, sent = await start(client, auth_headers, "Save a file", tool_profile="standard")
    await steer(client, auth_headers, sent["run"]["id"], "Call it a.txt")
    await drain_runs(sessionmaker, gateway, settings)  # read at step 0, then paused
    [call] = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    await client.post(
        f"/api/v1/tool-calls/{call['id']}/approve",
        json={"args_hash": call["args_hash"]},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    resumed = fake.requests[-1]["messages"]  # rebuilt from the database on resume
    users = [m for m in resumed if m["role"] == "user"]
    assert users[-1]["content"].endswith(f"{STEER_NOTE}\nCall it a.txt")
    assert sum(STEER_NOTE in str(m.get("content")) for m in resumed) == 1

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.auth.passwords import hash_password
from app.db.base import utcnow
from app.models import Run, RunStatus, User
from app.worker.executor import claim_next_run, execute_run, recover_stale_runs
from app.worker.loop import run_worker
from tests.fakes import FakeLlama, drain_runs, parse_sse


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def new_conversation(client, headers, **body) -> dict:
    response = await client.post("/api/v1/conversations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def send(client, headers, conversation_id, content="Hi there", client_id=None, **extra):
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": client_id or str(uuid.uuid4()), "content": content, **extra},
        headers=headers,
    )


async def events_for(client, headers, run_id, last_event_id=None) -> list[dict]:
    extra = {"Last-Event-ID": str(last_event_id)} if last_event_id else {}
    response = await client.get(f"/api/v1/runs/{run_id}/events", headers={**headers, **extra})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


def assistant_text(conversation: dict) -> str:
    message = conversation["messages"][-1]
    return "".join(p["text"] or "" for p in message["parts"] if p["type"] == "text")


# ---------------------------------------------------------------------- CRUD


async def test_conversation_crud_and_versioning(client, auth_headers):
    conv = await new_conversation(client, auth_headers, title="First")
    assert conv["version"] == 1 and conv["messages"] == [] and conv["active_branch_id"]

    listed = (await client.get("/api/v1/conversations", headers=auth_headers)).json()
    assert [c["id"] for c in listed["items"]] == [conv["id"]]

    patched = await client.patch(
        f"/api/v1/conversations/{conv['id']}",
        json={"title": "Renamed", "expected_version": 1},
        headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed" and patched.json()["version"] == 2

    stale = await client.patch(
        f"/api/v1/conversations/{conv['id']}",
        json={"title": "Stale write", "expected_version": 1},
        headers=auth_headers,
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "version_conflict" and error["current_version"] == 2
    # The server's state comes back so the client can offer "keep theirs / use mine".
    assert error["current"]["title"] == "Renamed" and error["current"]["version"] == 2

    archived = await client.patch(
        f"/api/v1/conversations/{conv['id']}", json={"archived": True}, headers=auth_headers
    )
    assert archived.json()["archived"] is True
    assert (await client.get("/api/v1/conversations", headers=auth_headers)).json()["items"] == []
    in_archive = await client.get("/api/v1/conversations?archived=true", headers=auth_headers)
    assert len(in_archive.json()["items"]) == 1

    deleted = await client.delete(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)
    assert gone.status_code == 404


async def test_list_pagination_and_search(client, auth_headers, sessionmaker, gateway, settings):
    ids = [
        (await new_conversation(client, auth_headers, title=f"Chat {i}"))["id"] for i in range(5)
    ]
    await send(client, auth_headers, ids[0], content="Tell me about pelicans")

    page1 = (await client.get("/api/v1/conversations?limit=2", headers=auth_headers)).json()
    page2 = (
        await client.get(
            f"/api/v1/conversations?limit=2&cursor={page1['next_cursor']}", headers=auth_headers
        )
    ).json()
    page3 = (
        await client.get(
            f"/api/v1/conversations?limit=2&cursor={page2['next_cursor']}", headers=auth_headers
        )
    ).json()
    seen = [c["id"] for c in page1["items"] + page2["items"] + page3["items"]]
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 5
    assert page3["next_cursor"] is None

    found = (await client.get("/api/v1/conversations?q=PELICAN", headers=auth_headers)).json()
    assert [c["id"] for c in found["items"]] == [ids[0]]
    assert (await client.get("/api/v1/conversations?q=100%", headers=auth_headers)).json()[
        "items"
    ] == []


async def test_other_users_cannot_see_conversations_or_runs(client, auth_headers, sessionmaker):
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]

    async with sessionmaker() as db:
        db.add(User(username="intruder", password_hash=hash_password("another-password-1")))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "intruder", "password": "another-password-1"}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (
        await client.get(f"/api/v1/conversations/{conv['id']}", headers=other)
    ).status_code == 404
    assert (await client.get(f"/api/v1/runs/{run_id}", headers=other)).status_code == 404
    assert (await client.get(f"/api/v1/runs/{run_id}/events", headers=other)).status_code == 404
    assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=other)).status_code == 404
    assert (await send(client, other, conv["id"])).status_code == 404
    assert (await client.get("/api/v1/conversations", headers=other)).json()["items"] == []


# ---------------------------------------------------------------------- submission


async def test_submission_is_idempotent(client, auth_headers):
    conv = await new_conversation(client, auth_headers)
    first = await send(client, auth_headers, conv["id"], client_id="client-1")
    assert first.status_code == 201
    body = first.json()
    assert body["run"]["status"] == "queued"
    assert body["assistant_message"]["status"] == "pending"
    assert body["message"]["client_message_id"] == "client-1"

    retry = await send(client, auth_headers, conv["id"], client_id="client-1")
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert retry.json()["message"]["id"] == body["message"]["id"]
    assert retry.json()["run"]["id"] == body["run"]["id"]

    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert len(detail["messages"]) == 2  # user + pending assistant, not duplicated
    assert detail["title"] == "Hi there"
    assert detail["active_run"]["id"] == body["run"]["id"]


async def test_reused_client_id_in_other_conversation_rejected(client, auth_headers):
    a = await new_conversation(client, auth_headers)
    b = await new_conversation(client, auth_headers)
    await send(client, auth_headers, a["id"], client_id="same")
    response = await send(client, auth_headers, b["id"], client_id="same")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "client_message_id_reused"


async def test_one_active_run_per_conversation(client, auth_headers):
    conv = await new_conversation(client, auth_headers)
    await send(client, auth_headers, conv["id"])
    second = await send(client, auth_headers, conv["id"])
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "run_in_progress"


async def test_expected_version_and_parent_checks(client, auth_headers):
    conv = await new_conversation(client, auth_headers)
    stale = await send(client, auth_headers, conv["id"], expected_version=99)
    assert stale.json()["error"]["code"] == "version_conflict"
    fork = await send(client, auth_headers, conv["id"], parent_message_id=str(uuid.uuid4()))
    assert fork.json()["error"]["code"] == "parent_not_head"


# ---------------------------------------------------------------------- generation


async def test_end_to_end_chat(client, auth_headers, sessionmaker, gateway, settings, fake):
    fake.reasoning = "Let me think."
    conv = await new_conversation(client, auth_headers)
    sent = (await send(client, auth_headers, conv["id"], content="Hello?")).json()
    run_id = sent["run"]["id"]

    assert await drain_runs(sessionmaker, gateway, settings) == 1

    events = await events_for(client, auth_headers, run_id)
    types = [e["type"] for e in events]
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert "message.delta" in types
    streamed = "".join(e["data"].get("text", "") for e in events if e["type"] == "message.delta")
    assert streamed == "Hello there, world"
    assert events[-1]["data"]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert [e["event_id"] for e in events] == sorted(e["event_id"] for e in events)

    # Resuming from the middle replays only what came after.
    resumed = await events_for(client, auth_headers, run_id, last_event_id=events[1]["event_id"])
    assert resumed == events[2:]

    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assistant = detail["messages"][-1]
    assert assistant["status"] == "complete"
    assert [p["type"] for p in assistant["parts"]] == ["reasoning", "text"]
    assert assistant_text(detail) == "Hello there, world"
    assert detail["active_run"] is None

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "completed" and run["finished_at"]

    sent_payload = fake.requests[0]
    assert sent_payload["stream"] is True
    assert [m["role"] for m in sent_payload["messages"]] == ["system", "user"]


async def test_second_turn_includes_history(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers)
    await send(client, auth_headers, conv["id"], content="First question")
    await drain_runs(sessionmaker, gateway, settings)
    await send(client, auth_headers, conv["id"], content="Follow-up")
    await drain_runs(sessionmaker, gateway, settings)

    messages = fake.requests[1]["messages"]
    assert [(m["role"], m["content"]) for m in messages[1:]] == [
        ("user", "First question"),
        ("assistant", "Hello there, world"),
        ("user", "Follow-up"),
    ]
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]


async def test_model_override_and_params_forwarded(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    conv = await new_conversation(client, auth_headers, model_override="other-model")
    sent = await send(
        client, auth_headers, conv["id"], params={"temperature": 0.3, "max_tokens": 50}
    )
    assert sent.json()["run"]["model_id"] == "other-model"
    await drain_runs(sessionmaker, gateway, settings)
    assert fake.requests[0]["model"] == "other-model"
    assert fake.requests[0]["temperature"] == 0.3
    assert fake.requests[0]["max_tokens"] == 50


async def test_model_error_fails_run_and_unblocks_conversation(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.fail_status = 400
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    await drain_runs(sessionmaker, gateway, settings)

    events = await events_for(client, auth_headers, run_id)
    assert events[-1]["type"] == "run.failed"
    assert events[-1]["data"]["code"] == "model_request_rejected"
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed" and run["error_message"] == "context size exceeded"

    fake.fail_status = None
    retry = await send(client, auth_headers, conv["id"], content="Again")
    assert retry.status_code == 201
    await drain_runs(sessionmaker, gateway, settings)
    # The failed (empty) assistant turn is left out, and the two user turns are merged.
    assert [m["role"] for m in fake.requests[1]["messages"]] == ["system", "user"]


async def test_stream_error_keeps_partial_output(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.fail_after_chunks = 1
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    await drain_runs(sessionmaker, gateway, settings)

    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed" and run["error_code"] == "model_stream_error"
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert detail["messages"][-1]["status"] == "failed"
    assert assistant_text(detail) == "Hello "


async def test_context_overflow_fails_before_calling_model(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.context_tokens = 600
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"], content="word " * 2000)).json()["run"][
        "id"
    ]
    await drain_runs(sessionmaker, gateway, settings)
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed" and run["error_code"] == "context_overflow"
    assert fake.requests == []


# ---------------------------------------------------------------------- cancellation


async def test_cancel_queued_run(client, auth_headers, sessionmaker, gateway, settings, fake):
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]

    cancelled = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_headers)
    assert cancelled.json()["status"] == "cancelled"
    assert await drain_runs(sessionmaker, gateway, settings) == 0
    events = await events_for(client, auth_headers, run_id)
    assert [e["type"] for e in events] == ["run.cancelled"]
    assert fake.requests == []
    # Cancelling again is a no-op.
    again = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_headers)
    assert again.json()["status"] == "cancelled"


async def test_cancel_while_streaming(client, auth_headers, sessionmaker, gateway, settings, fake):
    fake.reply = " ".join(f"w{i}" for i in range(200))
    fake.chunk_delay = 0.01
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    claimed = await claim_next_run(sessionmaker, "w", settings)
    task = asyncio.create_task(
        execute_run(
            claimed, factory=sessionmaker, gateway=gateway, settings=settings, worker_id="w"
        )
    )
    while fake.chunks_sent < 5:
        await asyncio.sleep(0.01)
    await client.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_headers)
    await asyncio.wait_for(task, timeout=5)

    sent_at_cancel = fake.chunks_sent
    await asyncio.sleep(0.1)
    assert fake.chunks_sent == sent_at_cancel < 200  # nothing pulls more tokens
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "cancelled"
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert detail["messages"][-1]["status"] == "cancelled"
    assert assistant_text(detail).startswith("w0 w1 w2")
    events = await events_for(client, auth_headers, run_id)
    assert events[-1]["type"] == "run.cancelled"


async def test_cancel_during_prompt_processing(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    """No tokens arrive yet (long prompt eval); the heartbeat still notices cancellation."""
    fake.chunk_delay = 30
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    claimed = await claim_next_run(sessionmaker, "w", settings)
    task = asyncio.create_task(
        execute_run(
            claimed, factory=sessionmaker, gateway=gateway, settings=settings, worker_id="w"
        )
    )
    while not fake.requests:
        await asyncio.sleep(0.01)
    await client.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_headers)
    await asyncio.wait_for(task, timeout=5)
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "cancelled"


# ---------------------------------------------------------------------- restart recovery


async def expire_lease(sessionmaker, run_id):
    async with sessionmaker() as db:
        await db.execute(
            update(Run)
            .where(Run.id == uuid.UUID(run_id))
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
        await db.commit()


async def test_run_from_dead_worker_is_requeued_and_completed(
    client, auth_headers, sessionmaker, gateway, settings
):
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]

    # Worker A claims the run and dies without finishing it.
    assert await claim_next_run(sessionmaker, "worker-a", settings) is not None
    assert await recover_stale_runs(sessionmaker, settings) == 0  # lease still valid
    await expire_lease(sessionmaker, run_id)
    assert await recover_stale_runs(sessionmaker, settings) == 1

    # Worker B picks it up.
    assert await drain_runs(sessionmaker, gateway, settings, worker_id="worker-b") == 1
    events = await events_for(client, auth_headers, run_id)
    assert events[0]["type"] == "run.progress" and events[0]["data"]["stage"] == "restarted"
    assert events[-1]["type"] == "run.completed"
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert assistant_text(detail) == "Hello there, world"


async def test_run_fails_after_max_attempts(client, auth_headers, sessionmaker, settings):
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    for attempt in range(settings.run_max_attempts):
        assert await claim_next_run(sessionmaker, f"worker-{attempt}", settings) is not None
        await expire_lease(sessionmaker, run_id)
        await recover_stale_runs(sessionmaker, settings)
    run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
    assert run["status"] == "failed" and run["error_code"] == "worker_lost"


async def test_stale_worker_cannot_overwrite_recovered_run(
    client, auth_headers, sessionmaker, gateway, settings
):
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    stale_claim = await claim_next_run(sessionmaker, "worker-a", settings)
    await expire_lease(sessionmaker, run_id)
    await recover_stale_runs(sessionmaker, settings)
    await drain_runs(sessionmaker, gateway, settings, worker_id="worker-b")

    # Worker A wakes up and tries to execute its old claim: it must do nothing.
    await execute_run(
        stale_claim, factory=sessionmaker, gateway=gateway, settings=settings, worker_id="worker-a"
    )
    async with sessionmaker() as db:
        run = await db.scalar(select(Run).where(Run.id == uuid.UUID(run_id)))
    assert run.status == RunStatus.completed and run.worker_id == "worker-b"


async def test_worker_loop_processes_runs_queued_while_it_was_down(
    client, auth_headers, sessionmaker, gateway, settings
):
    conv_a = await new_conversation(client, auth_headers)
    conv_b = await new_conversation(client, auth_headers)
    run_a = (await send(client, auth_headers, conv_a["id"])).json()["run"]["id"]
    run_b = (await send(client, auth_headers, conv_b["id"])).json()["run"]["id"]

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker(factory=sessionmaker, gateway=gateway, settings=settings, stop=stop)
    )

    async def wait_done(run_id):
        while True:
            run = (await client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)).json()
            if run["status"] == "completed":
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(asyncio.gather(wait_done(run_a), wait_done(run_b)), timeout=10)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)


async def test_sse_streams_live_until_completion(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    """A client subscribed before generation starts receives the events as they happen."""
    fake.chunk_delay = 0.005
    conv = await new_conversation(client, auth_headers)
    run_id = (await send(client, auth_headers, conv["id"])).json()["run"]["id"]
    listener = asyncio.create_task(events_for(client, auth_headers, run_id))
    await asyncio.sleep(0.05)
    assert not listener.done()
    await drain_runs(sessionmaker, gateway, settings)
    events = await asyncio.wait_for(listener, timeout=5)
    assert events[0]["type"] == "run.started" and events[-1]["type"] == "run.completed"


async def test_concurrent_workers_never_claim_the_same_run(
    client, auth_headers, sessionmaker, settings
):
    convs = [await new_conversation(client, auth_headers) for _ in range(3)]
    for conv in convs:
        await send(client, auth_headers, conv["id"])
    claims = await asyncio.gather(
        *(claim_next_run(sessionmaker, f"worker-{i}", settings) for i in range(8))
    )
    claimed = [c for c in claims if c is not None]
    assert len(claimed) == len(set(claimed))
    async with sessionmaker() as db:
        attempts = (await db.scalars(select(Run.attempts))).all()
    assert all(a <= 1 for a in attempts)

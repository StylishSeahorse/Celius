"""Phase 8: earlier tool use in later turns, pruning streamed-token events."""

import hashlib
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.base import utcnow
from app.models import Run, RunEvent
from app.services.runs import prune_stream_events
from app.worker.executor import TOOL_NOTE_HEADER
from tests.fakes import FakeLlama, drain_runs, parse_sse


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def say(client, headers, conversation_id, text):
    sent = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    return sent.json()


async def test_later_turns_remember_which_tools_ran(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        [{"name": "list_files", "arguments": {"path": "."}}],
        "Nothing there yet.",
        "You asked me to look and it was empty.",
    ]
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "standard"}, headers=auth_headers
        )
    ).json()
    await say(client, auth_headers, conv["id"], "What files are there?")
    await drain_runs(sessionmaker, gateway, settings)
    await say(client, auth_headers, conv["id"], "What did you do before?")
    await drain_runs(sessionmaker, gateway, settings)

    replayed = [m for m in fake.requests[-1]["messages"] if m["role"] == "assistant"]
    assert len(replayed) == 1
    content = replayed[0]["content"]
    assert content.startswith(TOOL_NOTE_HEADER)
    assert '- list_files({"path":"."}) → ok:' in content
    assert content.endswith("Nothing there yet.")


async def test_tool_summaries_can_be_switched_off(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.tool_history_result_chars = 0
    fake.turns = [[{"name": "list_files", "arguments": {}}], "Empty.", "Ok."]
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "standard"}, headers=auth_headers
        )
    ).json()
    await say(client, auth_headers, conv["id"], "Files?")
    await drain_runs(sessionmaker, gateway, settings)
    await say(client, auth_headers, conv["id"], "And?")
    await drain_runs(sessionmaker, gateway, settings)
    replayed = [m for m in fake.requests[-1]["messages"] if m["role"] == "assistant"]
    assert replayed[0]["content"] == "Empty."


async def test_stream_events_of_finished_runs_are_pruned(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.reply = "A reply streamed in several pieces " * 5
    conv = (await client.post("/api/v1/conversations", json={}, headers=auth_headers)).json()
    sent = await say(client, auth_headers, conv["id"], "Hello")
    run_id = uuid.UUID(sent["run"]["id"])
    await drain_runs(sessionmaker, gateway, settings)

    async def count(kind=None):
        async with sessionmaker() as db:
            query = select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
            if kind:
                query = query.where(RunEvent.type == kind)
            return await db.scalar(query)

    assert await count("message.delta") > 0
    # Recently finished: kept, so a client still streaming it can catch up.
    assert await prune_stream_events(sessionmaker, 10) == 0

    async with sessionmaker() as db:
        await db.execute(
            update(Run).where(Run.id == run_id).values(finished_at=utcnow() - timedelta(hours=1))
        )
        await db.commit()
    others = await count() - await count("message.delta")
    assert await prune_stream_events(sessionmaker, 10) > 0
    assert await count("message.delta") == 0
    assert await count() == others  # started, completed etc. are kept

    # A late client still gets the outcome, and the reply is on the message.
    events = parse_sse(
        (await client.get(f"/api/v1/runs/{run_id}/events", headers=auth_headers)).text
    )
    assert events[-1]["type"] == "run.completed"
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()
    assert detail["messages"][-1]["parts"][-1]["text"] == fake.reply


# ---------------------------------------------------------------------- resumable uploads


async def _start_upload(client, headers, name, size):
    created = await client.post(
        "/api/v1/files/uploads", json={"filename": name, "size": size}, headers=headers
    )
    assert created.status_code == 201, created.text
    return created.json()


def _range(first, last, total):
    return {"Content-Range": f"bytes {first}-{last}/{total}"}


async def test_chunked_upload_resumes_after_a_lost_connection(client, auth_headers):
    data = ("line of text\n" * 4000).encode()  # 52 000 bytes
    total = len(data)
    session = await _start_upload(client, auth_headers, "notes.txt", total)
    url = session["upload_url"]
    progress_url = f"/api/v1/files/uploads/{session['id']}"

    sent = await client.put(
        url, content=data[:20000], headers={**auth_headers, **_range(0, 19999, total)}
    )
    assert sent.status_code == 200 and sent.json()["received_bytes"] == 20000

    # The connection drops during the next chunk: only part of it arrives.
    partial = await client.put(
        url, content=data[20000:30000], headers={**auth_headers, **_range(20000, 39999, total)}
    )
    assert partial.json()["received_bytes"] == 30000
    # The client asks where to continue…
    progress = (await client.get(progress_url, headers=auth_headers)).json()
    assert progress["received_bytes"] == 30000 and progress["status"] == "open"
    # …and a gap is refused with the right offset.
    gap = await client.put(
        url, content=data[40000:], headers={**auth_headers, **_range(40000, total - 1, total)}
    )
    assert gap.status_code == 409 and gap.json()["error"]["received_bytes"] == 30000
    # Re-sending overlapping bytes (a lost response) is fine: they replace, not duplicate.
    again = await client.put(
        url, content=data[25000:], headers={**auth_headers, **_range(25000, total - 1, total)}
    )
    assert again.json()["received_bytes"] == total

    done = await client.post(f"{progress_url}/complete", headers=auth_headers)
    assert done.status_code == 201, done.text
    assert done.json()["sha256"] == hashlib.sha256(data).hexdigest()
    download = await client.get(f"/api/v1/files/{done.json()['id']}/download", headers=auth_headers)
    assert download.content == data


async def test_incomplete_or_oversized_chunked_uploads_are_refused(client, auth_headers):
    data = b"hello world, " * 100
    total = len(data)
    session = await _start_upload(client, auth_headers, "a.txt", total)
    url, upload_id = session["upload_url"], session["id"]
    await client.put(url, content=data[:500], headers={**auth_headers, **_range(0, 499, total)})
    early = await client.post(f"/api/v1/files/uploads/{upload_id}/complete", headers=auth_headers)
    assert early.status_code == 422 and early.json()["error"]["received_bytes"] == 500

    too_much = await client.put(
        url,
        content=data[500:] + b"extra",
        headers={**auth_headers, **_range(500, total - 1, total)},
    )
    assert too_much.status_code == 413
    wrong_total = await client.put(
        url, content=data[500:], headers={**auth_headers, **_range(500, total, total + 1)}
    )
    assert wrong_total.status_code == 400
    garbled = await client.put(
        url, content=b"x", headers={**auth_headers, "Content-Range": "bytes=0-1"}
    )
    assert garbled.status_code == 400
    # Still resumable from where it really is.
    progress = await client.get(f"/api/v1/files/uploads/{upload_id}", headers=auth_headers)
    assert progress.json()["received_bytes"] == 500


async def test_upload_progress_is_private(client, auth_headers, sessionmaker):
    from app.auth.passwords import hash_password
    from app.models import User

    session = await _start_upload(client, auth_headers, "mine.txt", 10)
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password("other password 1")))
        await db.commit()
    client.cookies.clear()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": "other password 1"}
    )
    client.cookies.clear()
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.get(f"/api/v1/files/uploads/{session['id']}", headers=other)
    assert response.status_code == 404

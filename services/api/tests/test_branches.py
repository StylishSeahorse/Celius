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


def texts(detail: dict) -> list[tuple[str, str]]:
    return [
        (m["role"], "".join(p["text"] or "" for p in m["parts"] if p["type"] == "text"))
        for m in detail["messages"]
    ]


async def get_detail(client, headers, conversation_id) -> dict:
    return (await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)).json()


@pytest.fixture
async def two_turns(client, auth_headers, sessionmaker, gateway, settings, fake):
    """A conversation with two completed turns; the fake replies 'reply N'."""
    replies = iter(["reply one", "reply two", "reply three", "reply four", "reply five"])

    async def run_all():
        fake.reply = next(replies)
        await drain_runs(sessionmaker, gateway, settings)

    conv = await new_conversation(client, auth_headers)
    await send(client, auth_headers, conv["id"], content="first question")
    await run_all()
    await send(client, auth_headers, conv["id"], content="second question")
    await run_all()
    return conv["id"], run_all


async def test_regenerate_keeps_original_and_switches_between_them(client, auth_headers, two_turns):
    conv_id, run_all = two_turns
    before = await get_detail(client, auth_headers, conv_id)
    original_reply = before["messages"][-1]

    regen = await client.post(
        f"/api/v1/messages/{original_reply['id']}/regenerate", json={}, headers=auth_headers
    )
    assert regen.status_code == 201
    await run_all()

    after = await get_detail(client, auth_headers, conv_id)
    assert texts(after)[-1] == ("assistant", "reply three")
    assert after["messages"][-1]["sibling_ids"] == [
        original_reply["id"],
        regen.json()["assistant_message"]["id"],
    ]
    assert after["version"] > before["version"]

    # Switch back to the original reply: nothing was overwritten.
    selected = await client.post(
        f"/api/v1/messages/{original_reply['id']}/select", headers=auth_headers
    )
    assert selected.status_code == 200
    assert texts(selected.json())[-1] == ("assistant", "reply two")
    assert texts(await get_detail(client, auth_headers, conv_id)) == texts(before)


async def test_edit_forks_a_new_branch_with_only_the_shared_history(
    client, auth_headers, two_turns, fake
):
    conv_id, run_all = two_turns
    before = await get_detail(client, auth_headers, conv_id)
    second_question = before["messages"][2]

    edit = await client.post(
        f"/api/v1/messages/{second_question['id']}/revisions",
        json={"client_message_id": "edit-1", "content": "second question, edited"},
        headers=auth_headers,
    )
    assert edit.status_code == 201
    await run_all()

    after = await get_detail(client, auth_headers, conv_id)
    assert texts(after) == [
        ("user", "first question"),
        ("assistant", "reply one"),
        ("user", "second question, edited"),
        ("assistant", "reply three"),
    ]
    assert after["messages"][2]["sibling_ids"] == [
        second_question["id"],
        edit.json()["message"]["id"],
    ]
    # The model saw the edited question, not the original one.
    sent = [(m["role"], m["content"]) for m in fake.requests[-1]["messages"][1:]]
    assert sent == [
        ("user", "first question"),
        ("assistant", "reply one"),
        ("user", "second question, edited"),
    ]

    # Selecting the original question restores its whole continuation.
    restored = await client.post(
        f"/api/v1/messages/{second_question['id']}/select", headers=auth_headers
    )
    assert texts(restored.json()) == texts(before)


async def test_editing_the_first_message_forks_at_the_root(client, auth_headers, two_turns):
    conv_id, run_all = two_turns
    first = (await get_detail(client, auth_headers, conv_id))["messages"][0]
    await client.post(
        f"/api/v1/messages/{first['id']}/revisions",
        json={"client_message_id": "edit-root", "content": "a different start"},
        headers=auth_headers,
    )
    await run_all()
    after = await get_detail(client, auth_headers, conv_id)
    assert texts(after) == [("user", "a different start"), ("assistant", "reply three")]
    assert len(after["messages"][0]["sibling_ids"]) == 2


async def test_new_messages_append_to_the_selected_branch(client, auth_headers, two_turns):
    conv_id, run_all = two_turns
    detail = await get_detail(client, auth_headers, conv_id)
    reply = detail["messages"][-1]
    await client.post(f"/api/v1/messages/{reply['id']}/regenerate", json={}, headers=auth_headers)
    await run_all()
    await client.post(f"/api/v1/messages/{reply['id']}/select", headers=auth_headers)

    await send(client, auth_headers, conv_id, content="third question")
    await run_all()
    after = await get_detail(client, auth_headers, conv_id)
    assert [t for _, t in texts(after)][-3:] == ["reply two", "third question", "reply four"]


async def test_edit_and_regenerate_are_idempotent(client, auth_headers, two_turns):
    conv_id, _ = two_turns
    detail = await get_detail(client, auth_headers, conv_id)
    question, reply = detail["messages"][2], detail["messages"][3]

    body = {"client_message_id": "edit-x", "content": "edited"}
    first = await client.post(
        f"/api/v1/messages/{question['id']}/revisions", json=body, headers=auth_headers
    )
    retry = await client.post(
        f"/api/v1/messages/{question['id']}/revisions", json=body, headers=auth_headers
    )
    assert retry.status_code == 200 and retry.json()["duplicate"] is True
    assert retry.json()["run"]["id"] == first.json()["run"]["id"]

    await client.post(f"/api/v1/runs/{first.json()['run']['id']}/cancel", headers=auth_headers)
    await client.post(f"/api/v1/messages/{reply['id']}/select", headers=auth_headers)
    body = {"client_request_id": "regen-x"}
    first = await client.post(
        f"/api/v1/messages/{reply['id']}/regenerate", json=body, headers=auth_headers
    )
    retry = await client.post(
        f"/api/v1/messages/{reply['id']}/regenerate", json=body, headers=auth_headers
    )
    assert first.status_code == 201
    assert retry.status_code == 200 and retry.json()["run"]["id"] == first.json()["run"]["id"]


async def test_branch_operations_are_validated(client, auth_headers, two_turns):
    conv_id, _ = two_turns
    detail = await get_detail(client, auth_headers, conv_id)
    question, reply = detail["messages"][0], detail["messages"][1]

    wrong_edit = await client.post(
        f"/api/v1/messages/{reply['id']}/revisions",
        json={"client_message_id": "e", "content": "x"},
        headers=auth_headers,
    )
    assert wrong_edit.json()["error"]["code"] == "not_a_user_message"
    wrong_regen = await client.post(
        f"/api/v1/messages/{question['id']}/regenerate", json={}, headers=auth_headers
    )
    assert wrong_regen.json()["error"]["code"] == "not_an_assistant_message"
    missing = await client.post(f"/api/v1/messages/{uuid.uuid4()}/select", headers=auth_headers)
    assert missing.status_code == 404

    # Nothing may change branches while a reply is being generated.
    await send(client, auth_headers, conv_id, content="pending")
    for path, body in [
        (f"/messages/{reply['id']}/regenerate", {}),
        (f"/messages/{question['id']}/revisions", {"client_message_id": "e2", "content": "x"}),
        (f"/messages/{question['id']}/select", None),
    ]:
        response = await client.post(f"/api/v1{path}", json=body, headers=auth_headers)
        assert response.status_code == 409, path
        assert response.json()["error"]["code"] == "run_in_progress"


async def test_parent_not_head_reports_the_current_head(client, auth_headers, two_turns):
    conv_id, _ = two_turns
    detail = await get_detail(client, auth_headers, conv_id)
    stale_parent = detail["messages"][1]["id"]
    response = await send(client, auth_headers, conv_id, parent_message_id=stale_parent)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "parent_not_head"
    assert error["head_message_id"] == detail["messages"][-1]["id"]

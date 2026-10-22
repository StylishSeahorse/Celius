import json

import pytest

from app.services.model_gateway.textcalls import extract
from tests.fakes import FakeLlama, drain_runs

OFFERED = {"run_shell", "read_file"}


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


def test_qwen3_coder_xml_call():
    text = (
        "Let me check for ffmpeg.\n<tool_call> <function=run_shell> <parameter=command> "
        'find / -name "ffmpeg" 2>/dev/null | head -5 </parameter> '
        "<parameter=timeout_seconds> 10 </parameter> </function> </tool_call>"
    )
    calls, rest = extract(text, OFFERED)
    assert [c.name for c in calls] == ["run_shell"]
    assert json.loads(calls[0].arguments) == {
        "command": 'find / -name "ffmpeg" 2>/dev/null | head -5',
        "timeout_seconds": "10",
    }
    assert rest == "Let me check for ffmpeg."


def test_hermes_json_calls_and_unclosed_xml():
    calls, rest = extract(
        'ok <tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>', OFFERED
    )
    assert calls[0].name == "read_file" and json.loads(calls[0].arguments) == {"path": "a.txt"}
    assert rest == "ok"
    calls, _ = extract("<function=run_shell><parameter=command>ls</parameter>", OFFERED)
    assert json.loads(calls[0].arguments) == {"command": "ls"}


def test_mistral_and_unknown_tools():
    calls, _ = extract(
        '[TOOL_CALLS][{"name": "run_shell", "arguments": {"command": "ls"}}]', OFFERED
    )
    assert calls[0].name == "run_shell"
    assert (
        extract('<tool_call>{"name": "format_disk", "arguments": {}}</tool_call>', OFFERED)[0] == []
    )
    assert extract("Use <function=x> in templates.", OFFERED)[0] == []


async def test_a_call_written_as_text_still_runs(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        "Checking.\n<tool_call>\n<function=list_files>\n</function>\n</tool_call>",
        "Nothing there.",
    ]
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "standard"}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "t", "content": "files?"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    reply = (await client.get(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)).json()[
        "messages"
    ][-1]
    assert [c["tool_name"] for c in reply["tool_calls"]] == ["list_files"]
    texts = [p["text"] for p in reply["parts"] if p["type"] == "text"]
    assert texts == ["Checking.", "Nothing there."]
    assert fake.requests[-1]["messages"][-1]["role"] == "tool"


async def _reply(client, headers, sessionmaker, gateway, settings, profile="standard"):
    conv = (
        await client.post("/api/v1/conversations", json={"tool_profile": profile}, headers=headers)
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "t", "content": "go"},
        headers=headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    return (await client.get(f"/api/v1/conversations/{conv['id']}", headers=headers)).json()[
        "messages"
    ][-1]


def _texts(reply) -> str:
    return "".join(p["text"] for p in reply["parts"] if p["type"] == "text")


XML_CALL = (
    "The video is ready!\n\n<tool_call> <function=list_files> <parameter=path> . "
    "</parameter> </function> </tool_call>"
)


async def test_a_call_after_the_step_limit_is_not_shown_raw(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    """Tools are switched off once the limit is reached; a call written then is removed
    from the answer and the user is told how to carry on."""
    settings.tool_max_iterations = 1
    fake.turns = [[{"name": "list_files", "arguments": {}}], XML_CALL]
    reply = await _reply(client, auth_headers, sessionmaker, gateway, settings)
    text = _texts(reply)
    assert "<tool_call>" not in text and "<function=" not in text
    assert text.startswith("The video is ready!")
    assert "Stopped before running list_files" in text and "continue" in text
    assert len(reply["tool_calls"]) == 1  # only the call made within the limit


async def test_a_call_to_a_tool_that_isnt_offered_is_not_shown_raw(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    fake.turns = [
        "Saving it.\n<tool_call>\n<function=write_file>\n<parameter=path>a.txt</parameter>"
        "\n</function>\n</tool_call>"
    ]
    reply = await _reply(client, auth_headers, sessionmaker, gateway, settings, "read_only")
    text = _texts(reply)
    assert "<tool_call>" not in text and text.startswith("Saving it.")
    assert "Tried to use write_file, which isn't available in this chat" in text
    assert reply["tool_calls"] == []


async def test_a_call_left_in_the_reasoning_runs(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    """Qwen sometimes ends its turn inside the reasoning, with the call there."""
    fake.reasoning = "I should look.\n<tool_call>\n<function=list_files>\n</function>\n</tool_call>"
    fake.turns = ["", "Nothing there."]
    reply = await _reply(client, auth_headers, sessionmaker, gateway, settings)
    assert [c["tool_name"] for c in reply["tool_calls"]] == ["list_files"]
    assert _texts(reply) == "Nothing there."
    reasoning = "".join(p["text"] or "" for p in reply["parts"] if p["type"] == "reasoning")
    assert "<tool_call>" not in reasoning and "I should look." in reasoning


def test_markup_next_to_a_real_call_is_removed_not_run_twice():
    from app.worker.executor import _StreamState, _tidy_text_calls

    state = _StreamState(
        text=["Listing.\n<tool_call><function=list_files></function></tool_call>"],
        tool_calls={0: {"id": "c1", "name": "list_files", "arguments": "{}"}},
    )
    assert _tidy_text_calls(state, OFFERED | {"list_files"}) == ["list_files"]
    assert state.text == ["Listing."]
    assert list(state.tool_calls) == [0]


def test_strip_leaves_prose_about_the_syntax_alone():
    from app.services.model_gateway.textcalls import strip

    assert strip("Use <function=x> in templates.") == ("Use <function=x> in templates.", [])
    assert strip('done <tool_call>{"name": "y", ') == ("done", ["y"])

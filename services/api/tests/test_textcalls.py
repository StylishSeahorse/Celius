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

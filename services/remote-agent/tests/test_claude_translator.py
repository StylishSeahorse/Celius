"""The Claude Code translator, with stand-ins named like the Agent SDK's message classes
(claude_agent_sdk.types). A test against the real SDK classes runs when it is installed."""

from dataclasses import dataclass, field

import pytest

from celius_agent.adapters.claude_code import Translator


@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: object = None
    is_error: bool | None = None


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude"
    parent_tool_use_id: str | None = None
    error: str | None = None


@dataclass
class UserMessage:
    content: object
    parent_tool_use_id: str | None = None


@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: float | None = None
    usage: dict = field(default_factory=dict)
    result: str | None = None


def test_a_turn_translates_to_canonical_events():
    t = Translator()
    events = []
    events += t.translate(
        SystemMessage("init", {"session_id": "abc", "model": "m", "tools": ["Bash"]})
    )
    events += t.translate(
        AssistantMessage(
            [
                ThinkingBlock("planning"),
                TextBlock("I'll edit the file."),
                ToolUseBlock(
                    "toolu_1",
                    "Edit",
                    {"file_path": "/w/a.py", "old_string": "a\n", "new_string": "b\n"},
                ),
            ]
        )
    )
    events += t.translate(
        UserMessage([ToolResultBlock("toolu_1", [{"type": "text", "text": "ok"}], False)])
    )
    events += t.translate(
        ResultMessage("success", 1200, 900, False, 2, "abc", 0.01, {"output_tokens": 5}, "Done")
    )
    types = [e[0] for e in events]
    assert types == [
        "session.started",
        "assistant.reasoning",
        "assistant.text",
        "tool.started",
        "tool.completed",
        "file.changed",
        "turn.completed",
    ]
    data = dict(events)
    assert data["session.started"]["external_session_id"] == "abc"
    assert data["tool.started"]["name"] == "Edit"
    assert data["tool.completed"]["content"] == "ok"
    assert "-a" in data["file.changed"]["diff"] and "+b" in data["file.changed"]["diff"]
    assert data["turn.completed"]["result"] == "Done"
    assert data["turn.completed"]["total_cost_usd"] == 0.01


def test_failed_edits_produce_no_file_change_and_subagent_text_is_not_the_reply():
    t = Translator()
    t.translate(
        AssistantMessage([ToolUseBlock("t2", "Write", {"file_path": "/w/x", "content": "y"})])
    )
    events = t.translate(UserMessage([ToolResultBlock("t2", "denied", True)]))
    assert [e[0] for e in events] == ["tool.completed"]
    assert events[0][1]["is_error"] is True
    assert (
        t.translate(AssistantMessage([TextBlock("subagent says")], parent_tool_use_id="t9")) == []
    )


def test_unknown_messages_and_large_inputs():
    t = Translator()
    assert t.translate(object()) == []
    big = {"content": "x" * 100_000}
    started = t.translate(AssistantMessage([ToolUseBlock("t3", "Bash", big)]))[0][1]
    assert started["input"]["_truncated"] is True


def test_real_sdk_classes_translate():
    sdk = pytest.importorskip("claude_agent_sdk")
    t = Translator()
    events = t.translate(sdk.AssistantMessage(content=[sdk.TextBlock(text="hi")], model="m"))
    assert events == [("assistant.text", {"text": "hi"})]
    result = sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
    )
    assert t.translate(result)[0][0] == "turn.completed"

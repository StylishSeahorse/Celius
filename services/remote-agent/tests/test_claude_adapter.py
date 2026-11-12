"""The Claude Code adapter driven by the runtime, with a stand-in for the Agent SDK module
that behaves like ClaudeSDKClient (connect, query, receive_messages, can_use_tool,
interrupt, disconnect). Running real Claude Code needs an account and network access."""

import asyncio
import sys
import types
from dataclasses import dataclass, field

import pytest

from celius_agent.config import Policy
from celius_agent.runtime import Runtime
from celius_agent.state import StateStore
from tests.test_claude_translator import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


@dataclass
class PermissionResultAllow:
    behavior: str = "allow"
    updated_input: dict | None = None


@dataclass
class PermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False


@dataclass
class Context:
    tool_use_id: str | None = None
    suggestions: list = field(default_factory=list)


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.queue: asyncio.Queue = asyncio.Queue()
        self.permission_results: list = []
        self.interrupted = False
        FakeClient.instances.append(self)

    async def connect(self):
        await self.queue.put(SystemMessage("init", {"session_id": "cc-1", "model": "m"}))

    async def query(self, text):
        asyncio.get_running_loop().create_task(self._turn(text))

    async def _turn(self, text):
        await self.queue.put(
            AssistantMessage(
                [TextBlock(f"Working on: {text}"), ToolUseBlock("tu1", "Bash", {"command": "ls"})]
            )
        )
        # Like the CLI: a tool needing permission calls back before it runs.
        result = await self.options.can_use_tool("Bash", {"command": "ls"}, Context("tu1"))
        self.permission_results.append(result)
        allowed = result.behavior == "allow"
        await self.queue.put(
            UserMessage([ToolResultBlock("tu1", "a.py" if allowed else "denied", not allowed)])
        )
        await self.queue.put(ResultMessage("success", 10, 5, False, 1, "cc-1", 0.001, {}, "ok"))

    async def receive_messages(self):
        while True:
            yield await self.queue.get()

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        pass


@pytest.fixture
def fake_sdk(monkeypatch):
    module = types.ModuleType("claude_agent_sdk")
    module.ClaudeSDKClient = FakeClient
    module.ClaudeAgentOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)
    module.PermissionResultAllow = PermissionResultAllow
    module.PermissionResultDeny = PermissionResultDeny
    module.__version__ = "test"
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    FakeClient.instances.clear()
    return module


def _runtime(tmp_path, *, allow_shell=False) -> tuple[Runtime, str]:
    work = tmp_path / "code"
    work.mkdir()
    policy = Policy(
        roots=[{"path": str(work), "write": True}],
        allow_shell=allow_shell,
        adapters=["celius", "claude_code"],
    )
    return Runtime(StateStore(tmp_path / "state"), policy), str(work)


async def _until(predicate, wait=5):
    for _ in range(int(wait / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def _types(runtime, session_id="s1"):
    session = runtime.sessions.get(session_id)
    return [e["type"] for e in session.pending] if session else []


async def test_turn_with_permission_round_trip(tmp_path, fake_sdk):
    runtime, work = _runtime(tmp_path)
    await runtime.handle_command(
        {
            "id": "open",
            "kind": "session.open",
            "session_id": "s1",
            "payload": {
                "adapter": "claude_code",
                "mode": "external",
                "working_directory": work,
                "permissions": {"allow_shell": True, "allow_write": True},
            },
        }
    )
    await _until(lambda: "session.opened" in _types(runtime))
    client = FakeClient.instances[0]
    assert client.options.cwd.endswith("code")
    assert client.options.permission_mode == "default"
    # The machine's policy forbids the shell: Claude Code doesn't get Bash at all.
    assert "Bash" in client.options.disallowed_tools

    await runtime.handle_command(
        {
            "id": "m1",
            "kind": "session.message",
            "session_id": "s1",
            "run_id": "r1",
            "payload": {"text": "list files"},
        }
    )
    await _until(lambda: "permission.request" in _types(runtime))
    request = next(e for e in runtime.sessions["s1"].pending if e["type"] == "permission.request")
    assert request["run_id"] == "r1"
    assert request["data"]["tool"] == "Bash"
    assert request["data"]["tool_use_id"] == "tu1"
    assert runtime.sessions["s1"].external_session_id == "cc-1"

    await runtime.handle_command(
        {
            "id": "d1",
            "kind": "permission.decision",
            "session_id": "s1",
            "payload": {"request_id": request["data"]["request_id"], "behavior": "allow"},
        }
    )
    await _until(lambda: "turn.completed" in _types(runtime))
    assert isinstance(client.permission_results[0], PermissionResultAllow)
    types_ = _types(runtime)
    # Whether the permission request comes before or after the assistant message that
    # proposed the tool is up to the SDK; Celius matches them by tool_use_id either way.
    assert set(types_) == {
        "session.started",
        "session.opened",
        "turn.started",
        "assistant.text",
        "tool.started",
        "permission.request",
        "tool.completed",
        "turn.completed",
    }
    assert types_[-1] == "turn.completed"
    assert types_.index("turn.started") < types_.index("permission.request")
    await runtime.shutdown()


async def test_deny_interrupt_and_resume(tmp_path, fake_sdk):
    runtime, work = _runtime(tmp_path, allow_shell=True)
    payload = {
        "adapter": "claude_code",
        "working_directory": work,
        "permissions": {"allow_shell": True},
        "external_session_id": "cc-previous",
    }
    await runtime.handle_command(
        {"id": "open", "kind": "session.open", "session_id": "s1", "payload": payload}
    )
    await _until(lambda: "session.opened" in _types(runtime))
    client = FakeClient.instances[0]
    assert client.options.resume == "cc-previous"  # continues the same Claude Code session
    assert "Bash" not in client.options.disallowed_tools
    await runtime.handle_command(
        {"id": "m1", "kind": "session.message", "session_id": "s1", "payload": {"text": "go"}}
    )
    await _until(lambda: "permission.request" in _types(runtime))
    # Stopped from Celius while waiting: the pending request is denied and Claude interrupted.
    await runtime.handle_command({"id": "i1", "kind": "interrupt", "session_id": "s1"})
    await _until(lambda: "turn.completed" in _types(runtime))
    assert client.interrupted
    assert isinstance(client.permission_results[0], PermissionResultDeny)
    await runtime.shutdown()


async def test_missing_sdk_fails_the_session(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # import fails
    runtime, work = _runtime(tmp_path)
    await runtime.handle_command(
        {
            "id": "open",
            "kind": "session.open",
            "session_id": "s1",
            "payload": {"adapter": "claude_code", "working_directory": work},
        }
    )
    await _until(lambda: _types(runtime))
    failed = runtime.sessions["s1"].pending[0]
    assert failed["type"] == "session.failed"
    assert "claude-agent-sdk" in failed["data"]["reason"]


async def test_adapter_restarts_after_an_agent_restart(tmp_path, fake_sdk):
    runtime, work = _runtime(tmp_path)
    await runtime.handle_command(
        {
            "id": "open",
            "kind": "session.open",
            "session_id": "s1",
            "payload": {"adapter": "claude_code", "working_directory": work},
        }
    )
    await _until(lambda: "s1" in runtime.sessions and runtime.sessions["s1"].external_session_id)
    await runtime.shutdown()
    # A new agent process: same state directory, no adapter yet.
    restarted = Runtime(runtime.store.__class__(tmp_path / "state"), runtime.policy)
    await restarted.handle_command(
        {
            "id": "m2",
            "kind": "session.message",
            "session_id": "s1",
            "run_id": "r2",
            "payload": {"text": "again"},
        }
    )
    await _until(lambda: "permission.request" in _types(restarted))
    assert FakeClient.instances[-1].options.resume == "cc-1"  # the same Claude Code session
    await restarted.shutdown()

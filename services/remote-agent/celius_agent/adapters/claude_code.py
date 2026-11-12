"""Claude Code adapter (mode 2): runs a Claude Code session on this machine through the
official Claude Agent SDK (`pip install claude-agent-sdk`, which bundles the CLI) and
translates its messages into Celius's canonical session events.

Why the Agent SDK: it is Claude Code's supported programmatic interface. It yields typed
messages (assistant text, tool use, tool results, the final result with cost and the
session id) and offers `can_use_tool`, a callback invoked whenever Claude Code would
otherwise prompt for permission. Celius turns that into an approval in the conversation
and answers with the user's decision, so no terminal text is parsed.

Event mapping (SDK → Celius):
  SystemMessage(init)                 → session.started {external_session_id, model, tools}
  AssistantMessage TextBlock          → assistant.text
  AssistantMessage ThinkingBlock      → assistant.reasoning
  AssistantMessage ToolUseBlock       → tool.started {tool_use_id, name, input}
  UserMessage ToolResultBlock         → tool.completed {tool_use_id, content, is_error}
                                        (+ file.changed for successful Write/Edit/MultiEdit)
  can_use_tool(name, input, context)  → permission.request {request_id, tool_use_id, ...}
  ResultMessage                       → turn.completed {is_error, result, cost, usage...}
"""

from __future__ import annotations

import asyncio
import difflib
import importlib.util
import json
import logging
import shutil
import uuid
from typing import Any

log = logging.getLogger("celius_agent.claude_code")

MAX_INPUT_CHARS = 20_000
MAX_RESULT_CHARS = 20_000
FILE_TOOLS = {"Write", "Edit", "MultiEdit"}
WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
SHELL_TOOLS = ["Bash", "BashOutput", "KillShell", "KillBash"]


def probe() -> dict | bool:
    """What this machine offers for Claude Code (sent to Celius as a capability)."""
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return False
    try:
        from claude_agent_sdk import __version__ as sdk_version  # type: ignore
    except ImportError:  # pragma: no cover
        sdk_version = "unknown"
    return {"sdk": sdk_version, "cli": shutil.which("claude")}


def _bounded(value: Any, limit: int = MAX_INPUT_CHARS) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {"_truncated": True, "preview": text[:limit]}


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict):
                parts.append(f"[{item.get('type', 'content')}]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class Translator:
    """SDK messages → canonical events. Duck-typed on class names and attributes, so it
    is tested without the SDK and tolerates fields added by newer SDK versions."""

    def __init__(self) -> None:
        self.file_edits: dict[str, dict] = {}  # tool_use_id -> pending file change

    def translate(self, message: Any) -> list[tuple[str, dict]]:
        kind = type(message).__name__
        if kind == "SystemMessage":
            data = getattr(message, "data", {}) or {}
            if getattr(message, "subtype", None) != "init":
                return []
            return [
                (
                    "session.started",
                    {
                        "external_session_id": data.get("session_id"),
                        "model": data.get("model"),
                        "tools": (data.get("tools") or [])[:200],
                        "cwd": data.get("cwd"),
                        "permission_mode": data.get("permissionMode"),
                    },
                )
            ]
        if kind == "AssistantMessage":
            return self._assistant(message)
        if kind == "UserMessage":
            return self._user(message)
        if kind == "ResultMessage":
            result = getattr(message, "result", None)
            return [
                (
                    "turn.completed",
                    {
                        "external_session_id": getattr(message, "session_id", None),
                        "subtype": getattr(message, "subtype", None),
                        "is_error": bool(getattr(message, "is_error", False)),
                        "result": (result or "")[:MAX_RESULT_CHARS]
                        if isinstance(result, str)
                        else None,
                        "num_turns": getattr(message, "num_turns", None),
                        "duration_ms": getattr(message, "duration_ms", None),
                        "total_cost_usd": getattr(message, "total_cost_usd", None),
                        "usage": _bounded(getattr(message, "usage", None) or {}, 4000),
                        "errors": (getattr(message, "errors", None) or [])[:10],
                    },
                )
            ]
        return []  # stream deltas, rate-limit notices, task progress: not needed

    def _assistant(self, message: Any) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        parent = getattr(message, "parent_tool_use_id", None)
        for block in getattr(message, "content", None) or []:
            name = type(block).__name__
            if name == "TextBlock":
                text = getattr(block, "text", "")
                if text and not parent:  # a subagent's text is not the reply
                    events.append(("assistant.text", {"text": text}))
            elif name == "ThinkingBlock":
                text = getattr(block, "thinking", "")
                if text and not parent:
                    events.append(("assistant.reasoning", {"text": text}))
            elif name == "ToolUseBlock":
                tool_input = getattr(block, "input", {}) or {}
                tool_use_id = getattr(block, "id", "")
                events.append(
                    (
                        "tool.started",
                        {
                            "tool_use_id": tool_use_id,
                            "name": getattr(block, "name", ""),
                            "input": _bounded(tool_input),
                            "parent_tool_use_id": parent,
                        },
                    )
                )
                change = self._file_change(getattr(block, "name", ""), tool_input)
                if change is not None:
                    self.file_edits[tool_use_id] = change
        error = getattr(message, "error", None)
        if error:
            events.append(("error", {"message": str(error)[:2000]}))
        return events

    def _user(self, message: Any) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return events
        for block in content:
            if type(block).__name__ != "ToolResultBlock":
                continue
            tool_use_id = getattr(block, "tool_use_id", "")
            is_error = bool(getattr(block, "is_error", False))
            events.append(
                (
                    "tool.completed",
                    {
                        "tool_use_id": tool_use_id,
                        "content": _text_of(getattr(block, "content", None))[:MAX_RESULT_CHARS],
                        "is_error": is_error,
                    },
                )
            )
            change = self.file_edits.pop(tool_use_id, None)
            if change is not None and not is_error:
                events.append(("file.changed", change))
        return events

    @staticmethod
    def _file_change(tool: str, tool_input: dict) -> dict | None:
        if tool not in FILE_TOOLS or not isinstance(tool_input, dict):
            return None
        path = tool_input.get("file_path")
        if not isinstance(path, str):
            return None
        diff = None
        if tool == "Edit":
            diff = "".join(
                difflib.unified_diff(
                    str(tool_input.get("old_string", "")).splitlines(keepends=True),
                    str(tool_input.get("new_string", "")).splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )[:64_000]
        elif tool == "Write":
            content = str(tool_input.get("content", ""))
            diff = "".join(
                difflib.unified_diff(
                    [], content.splitlines(keepends=True), "/dev/null", f"b/{path}"
                )
            )[:64_000]
        return {
            "path": path,
            "action": "modified" if tool != "Write" else "written",
            "diff": diff,
            "tool": tool,
        }


class ClaudeCodeAdapter:
    """One Claude Code session (ClaudeSDKClient) per Celius remote session."""

    def __init__(self, runtime, session):
        self.runtime = runtime
        self.session = session
        self.translator = Translator()
        self.client = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.run_id: str | None = None

    def _options(self, sdk):
        permissions = self.session.permissions or {}
        policy = self.runtime.policy
        disallowed: list[str] = []
        if not (
            policy.allow_write and permissions.get("allow_write", True) and self.session.writable
        ):
            disallowed += WRITE_TOOLS
        if not (policy.allow_shell and permissions.get("allow_shell")):
            disallowed += SHELL_TOOLS
        return sdk.ClaudeAgentOptions(
            cwd=self.session.working_directory,
            # "default": anything not pre-approved by the user's own Claude Code settings
            # reaches can_use_tool, i.e. becomes a Celius approval.
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            disallowed_tools=disallowed,
            resume=self.session.external_session_id,
            setting_sources=["user", "project", "local"],
            stderr=lambda line: log.debug("claude: %s", line.rstrip()),
        )

    async def start(self) -> None:
        try:
            import claude_agent_sdk as sdk  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is not installed (pip install 'celius-agent[claude]')"
            ) from exc
        self.client = sdk.ClaudeSDKClient(options=self._options(sdk))
        await self.client.connect()
        self.reader = asyncio.create_task(self._read())

    async def _emit(self, type_: str, data: dict) -> None:
        await self.runtime.emit(self.session.id, type_, data, self.run_id)

    async def _read(self) -> None:
        try:
            async for message in self.client.receive_messages():
                for type_, data in self.translator.translate(message):
                    if type_ == "session.started" and data.get("external_session_id"):
                        self.session.external_session_id = data["external_session_id"]
                        self.runtime.store.save_session(self.session)
                    await self._emit(type_, data)
                    if type_ == "turn.completed":
                        self.run_id = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Claude Code session %s stopped", self.session.id)
            await self._emit("error", {"message": f"Claude Code stopped: {exc}"[:2000]})
            if self.run_id:
                await self._emit(
                    "turn.completed",
                    {"is_error": True, "subtype": "adapter_error", "result": str(exc)[:2000]},
                )
                self.run_id = None

    async def send(self, payload: dict, run_id: str | None) -> None:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        self.run_id = run_id
        await self._emit("turn.started", {"run_id": run_id})
        await self.client.query(text)

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context: Any):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny  # type: ignore

        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._emit(
            "permission.request",
            {
                "request_id": request_id,
                "tool_use_id": getattr(context, "tool_use_id", None),
                "tool": tool_name,
                "input": _bounded(tool_input),
                "title": getattr(context, "title", None),
                "reason": getattr(context, "decision_reason", None),
                "blocked_path": getattr(context, "blocked_path", None),
            },
        )
        try:
            decision = await future  # until Celius sends the user's decision (or expiry)
        finally:
            self.pending.pop(request_id, None)
        if decision.get("behavior") == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(
            message=str(decision.get("message") or "The user denied this in Celius"),
            interrupt=bool(decision.get("interrupt", False)),
        )

    async def decide(self, payload: dict) -> None:
        future = self.pending.get(str(payload.get("request_id")))
        if future is not None and not future.done():
            future.set_result(payload)

    def _deny_all(self, message: str) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_result({"behavior": "deny", "message": message})

    async def interrupt(self) -> None:
        self._deny_all("Interrupted from Celius")
        if self.client is not None:
            await self.client.interrupt()

    async def stop(self) -> None:
        self._deny_all("The session was closed in Celius")
        if self.reader is not None:
            self.reader.cancel()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                log.exception("Closing Claude Code failed")

"""`celius`: chat with Celius from this device's terminal; its tools work right here.

The conversation is an ordinary Celius conversation (also on your phone and in the web
app): Celius's models, memory and approvals, with the file, shell, Python and git tools
acting in the folder `celius` was started in. Requests to run something are answered here
(or in Celius on another device).

The terminal handling lives in TerminalUI; Chat only talks to Celius through a
DeviceClient and to the person through a UI object, so it can be driven by tests.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Protocol

from celius_agent.client import ApiError, DeviceClient
from celius_agent.config import Paths
from celius_agent.paths import is_within

OPEN_WAIT_SECONDS = 40
RESULT_LINES = 12


class UI(Protocol):
    def write(self, text: str) -> None: ...
    def note(self, text: str) -> None: ...
    def tool(self, text: str) -> None: ...
    def output(self, text: str) -> None: ...
    def error(self, text: str) -> None: ...
    def confirm(self, question: str) -> bool: ...
    def approve(
        self, call: dict
    ) -> str | None: ...  # once | always | deny | None (decide elsewhere)


# ---------------------------------------------------------------------- folder trust


def ensure_folder_allowed(paths: Paths, directory: str, ui: UI) -> bool:
    """The machine's policy file decides where Celius may work. Starting `celius` in a
    folder that isn't allowed asks the person at this keyboard, once, and records it."""
    raw = json.loads(paths.policy.read_text(encoding="utf-8")) if paths.policy.exists() else {}
    roots = raw.get("roots") or []
    paths_of = [r["path"] if isinstance(r, dict) else r for r in roots]
    changed = False
    if not any(
        isinstance(p, str) and is_within(directory, os.path.expanduser(p)) for p in paths_of
    ):
        if not ui.confirm(
            f"Let Celius work in {directory}? It can read and change files in this folder."
        ):
            return False
        roots.append({"path": directory, "write": True})
        raw["roots"] = roots
        changed = True
    if not raw.get("allow_shell"):
        if ui.confirm("Allow commands here (shell, Python, git)? Each one still asks you first."):
            raw["allow_shell"] = True
            changed = True
    if changed:
        paths.ensure()
        tmp = paths.policy.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, paths.policy)
        ui.note(f"Saved in {paths.policy}")
    return True


# ---------------------------------------------------------------------- chat


def _summary(call: dict) -> str:
    args = call.get("arguments") or {}
    for key in ("command", "code", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            first = value.strip().splitlines()[0] if value.strip() else ""
            more = " …" if len(value.strip().splitlines()) > 1 else ""
            return f"{first[:120]}{more}"
    if isinstance(args.get("args"), list):
        return "git " + " ".join(str(a) for a in args["args"])[:120]
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + "…"


def _result_text(call: dict) -> str:
    result = call.get("result") or {}
    pieces = []
    for key in ("stdout", "stderr"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            pieces.append(value.rstrip())
    if not pieces and result.get("error"):
        pieces.append(str(result["error"]))
    lines = "\n".join(pieces).splitlines()
    if len(lines) > RESULT_LINES:
        lines = [f"… {len(lines) - RESULT_LINES} more lines"] + lines[-RESULT_LINES:]
    return "\n".join(lines)


class Chat:
    def __init__(self, client: DeviceClient, ui: UI):
        self.client = client
        self.ui = ui
        self.conversation_id: str | None = None
        self.url: str | None = None

    # ------------------------------------------------------------------ chats

    def start(self, directory: str, title: str | None = None, model: str | None = None) -> None:
        chat = self.client.call(
            "POST",
            "/api/v1/remote/device/chats",
            {"working_directory": directory, "title": title, "model": model},
        )
        self.conversation_id, self.url = chat["conversation_id"], chat["url"]
        self._wait_open()

    def resume(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        chats = self.client.call("GET", "/api/v1/remote/device/chats?limit=100")["items"]
        found = next((c for c in chats if c["conversation_id"] == conversation_id), None)
        self.url = found["url"] if found else None

    def chats(self) -> list[dict]:
        return self.client.call("GET", "/api/v1/remote/device/chats?limit=20")["items"]

    def detail(self) -> dict:
        return self.client.call("GET", f"/api/v1/remote/device/chats/{self.conversation_id}")

    def _wait_open(self) -> None:
        deadline = time.monotonic() + OPEN_WAIT_SECONDS
        while time.monotonic() < deadline:
            remote = self.detail().get("remote") or {}
            if remote.get("status") == "idle":
                return
            if remote.get("status") in ("failed", "terminated"):
                raise ApiError(409, "session_failed", remote.get("status_detail") or "It ended")
            time.sleep(0.3)
        raise ApiError(
            504,
            "agent_not_connected",
            "The Celius agent on this device didn't answer. Is it running? (celius agent)",
        )

    # ------------------------------------------------------------------ a turn

    def send(self, text: str) -> str:
        """Send a message and follow the reply to its end. Returns the run's final status."""
        sent = self.client.call(
            "POST",
            f"/api/v1/remote/device/chats/{self.conversation_id}/messages",
            {"client_message_id": uuid.uuid4().hex, "content": text},
        )
        return self.follow(sent["run"]["id"])

    def cancel(self, run_id: str) -> None:
        try:
            self.client.call("POST", f"/api/v1/remote/device/runs/{run_id}/cancel")
        except ApiError:
            pass

    def follow(self, run_id: str) -> str:
        after = 0
        streamed = False
        while True:
            try:
                for event in self.client.stream(
                    f"/api/v1/remote/device/runs/{run_id}/events?after={after}"
                ):
                    after = max(after, int(event.get("event_id") or 0))
                    outcome = self._handle(event, streamed)
                    streamed = streamed or event.get("type") == "message.delta"
                    if outcome:
                        return outcome
            except (ApiError, OSError) as exc:
                if isinstance(exc, ApiError) and exc.status in (401, 403, 404):
                    raise
                self.ui.note("Connection to Celius lost; resuming…")
                time.sleep(2)
                continue
            # The stream ended without a terminal event: look at the run itself.
            status = (self.detail().get("active_run") or {}).get("status")
            if status in (None, "completed", "failed", "cancelled"):
                return status or "completed"

    def _handle(self, event: dict, streamed: bool) -> str | None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "message.delta":
            if data.get("text"):
                self.ui.write(data["text"])
        elif kind == "tool_call.proposed":
            call = data.get("tool_call") or {}
            self.ui.tool(f"{call.get('tool_name')}  {_summary(call)}")
        elif kind == "tool_call.awaiting_approval":
            self._decide(data.get("tool_call") or {})
        elif kind == "tool_call.completed":
            call = data.get("tool_call") or {}
            text = _result_text(call)
            if call.get("status") in ("denied", "expired", "refused", "cancelled"):
                self.ui.note(f"{call.get('tool_name')}: {call.get('status')}")
            elif text:
                self.ui.output(text)
        elif kind == "run.progress" and data.get("stage") == "restarted":
            self.ui.note("The reply restarted (the Celius worker restarted).")
        elif kind == "run.completed":
            self.ui.write("\n")
            return "completed"
        elif kind == "run.failed":
            self.ui.error(f"The reply failed: {data.get('message') or data.get('code')}")
            return "failed"
        elif kind == "run.cancelled":
            self.ui.note("Stopped.")
            return "cancelled"
        return None

    def _decide(self, call: dict) -> None:
        choice = self.ui.approve(call)
        if choice is None:
            self.ui.note("Waiting for a decision in Celius…")
            return
        try:
            if choice == "deny":
                self.client.call(
                    "POST", f"/api/v1/remote/device/tool-calls/{call['id']}/deny", {"reason": None}
                )
            else:
                self.client.call(
                    "POST",
                    f"/api/v1/remote/device/tool-calls/{call['id']}/approve",
                    {"args_hash": call["args_hash"], "remember": choice == "always"},
                )
        except ApiError as exc:
            if exc.code in ("not_pending", "approval_expired"):
                self.ui.note("Already decided in Celius.")
            else:
                raise


# ---------------------------------------------------------------------- terminal


class TerminalUI:
    def __init__(self, interactive: bool | None = None):
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        if self.color and sys.platform == "win32":  # pragma: no cover - Windows only
            os.system("")  # turns on ANSI colours in the Windows console

    def _paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def note(self, text: str) -> None:
        print(self._paint("2", f"  {text}"))

    def tool(self, text: str) -> None:
        print("\n" + self._paint("36", f"● {text}"))

    def output(self, text: str) -> None:
        for line in text.splitlines():
            print(self._paint("2", f"  │ {line}"))

    def error(self, text: str) -> None:
        print(self._paint("31", text))

    def confirm(self, question: str) -> bool:
        if not self.interactive:
            return False
        try:
            return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    def approve(self, call: dict) -> str | None:
        if not self.interactive:
            return None  # one-shot (-p) without a terminal: decide in Celius
        print(self._paint("33", f"  Allow {call.get('tool_name')}?"))
        args = call.get("arguments") or {}
        detail = args.get("command") or args.get("code")
        if isinstance(detail, str) and "\n" in detail.strip():
            for line in detail.strip().splitlines()[:30]:
                print(self._paint("2", f"  │ {line}"))
        while True:
            try:
                answer = input("  [y] yes  [a] always in this chat  [n] no  › ").strip().lower()
            except EOFError:
                return "deny"
            if answer in ("y", "yes"):
                return "once"
            if answer in ("a", "always"):
                return "always"
            if answer in ("n", "no", ""):
                return "deny"


HELP = """Commands:
  /new          start a new chat in this folder
  /chats        recent chats on this device
  /resume N     continue chat N from /chats
  /open         the link to this chat in Celius
  /exit         quit (also Ctrl+D)
Ctrl+C stops the current reply."""


def repl(chat: Chat, ui: TerminalUI, directory: str) -> int:
    listed: list[dict] = []
    while True:
        try:
            line = input("\n› ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return 0
        if text == "/help":
            print(HELP)
            continue
        if text == "/open":
            print(chat.url or "(not available)")
            continue
        if text == "/new":
            chat.start(directory)
            ui.note(f"New chat · {chat.url}")
            continue
        if text == "/chats":
            listed = chat.chats()
            for number, item in enumerate(listed, start=1):
                print(
                    f"  {number:>2}. {item['title'] or 'Untitled'}  ·  {item['working_directory']}"
                )
            continue
        if text.startswith("/resume"):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(listed):
                chat.resume(listed[int(parts[1]) - 1]["conversation_id"])
                ui.note(f"Continuing · {chat.url}")
            else:
                print("Use /chats, then /resume N.")
            continue
        if text.startswith("/"):
            print("Unknown command. /help lists them.")
            continue
        run_id = None
        try:
            sent = chat.client.call(
                "POST",
                f"/api/v1/remote/device/chats/{chat.conversation_id}/messages",
                {"client_message_id": uuid.uuid4().hex, "content": text},
            )
            run_id = sent["run"]["id"]
            print()
            chat.follow(run_id)
        except KeyboardInterrupt:
            if run_id:
                chat.cancel(run_id)
            ui.note("Stopped.")
        except ApiError as exc:
            ui.error(exc.message)

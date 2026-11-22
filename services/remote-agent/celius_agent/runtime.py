"""The execution runtime: sessions, commands and events (docs/remote/ARCHITECTURE.md §4).

Commands arrive at least once. Each id is written to the journal before anything happens;
an id seen again is ignored (its events are already in the outbox) and one left `started`
by a previous run of the agent is reported as interrupted, never re-run.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable

from celius_agent.config import Policy
from celius_agent.paths import PathRefused, check_working_directory
from celius_agent.state import SessionState, StateStore
from celius_agent.tools import TOOLS, ToolEnv, error, fetch_file, find_python, shell_name

log = logging.getLogger("celius_agent.runtime")

MAX_TOOL_TIMEOUT = 24 * 3600
MAX_RESULT_CHARS = 200_000


class Runtime:
    def __init__(
        self,
        store: StateStore,
        policy: Policy,
        policy_loader: Callable[[], Policy] | None = None,
    ):
        self.store = store
        self.policy = policy
        # Re-reads the machine's policy file (e.g. after `celius` allowed a new folder).
        self.policy_loader = policy_loader
        self.policy_changed = False  # tell Celius at the next chance
        self.sessions: dict[str, SessionState] = store.load_sessions()
        self.running: dict[str, ToolEnv] = {}  # command id -> env (for cancel)
        self.tasks: set[asyncio.Task] = set()
        self.semaphore = asyncio.Semaphore(policy.max_concurrent_commands)
        self.wakeup = asyncio.Event()  # set when there is something new to send
        self.python = find_python()
        self.disconnect_requested = False

    # ------------------------------------------------------------------ facts

    def capabilities(self) -> dict:
        return {
            "shell": shell_name(),
            "python": self.python is not None,
            "git": shutil.which("git") is not None,
            "tools": sorted(TOOLS),
        }

    def reload_policy(self) -> None:
        if self.policy_loader is None:
            return
        try:
            fresh = self.policy_loader()
        except (OSError, ValueError) as exc:
            log.error("Can't read the policy file, keeping the previous one: %s", exc)
            return
        if fresh.report() != self.policy.report():
            log.info("Policy changed: %s", fresh.report())
            self.policy = fresh
            self.policy_changed = True
            self.wakeup.set()

    def open_sessions(self) -> int:
        return sum(1 for s in self.sessions.values() if s.status != "closed")

    # ------------------------------------------------------------------ events

    async def emit(
        self, session_id: str, type_: str, data: dict, run_id: str | None = None
    ) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            log.warning("Dropping %s for unknown session %s", type_, session_id)
            return
        self.store.append(session, type_, data, run_id)
        self.wakeup.set()

    def on_ack(self, session_id: str, seq: int, error_code: str | None = None) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        if error_code == "unknown_session":
            # Celius no longer has this session (its conversation was deleted).
            log.warning("Celius doesn't know session %s; forgetting it", session_id)
            self._stop_session_work(session_id)
            self.store.forget_session(session)
            self.sessions.pop(session_id, None)
            return
        self.store.ack(session, seq)
        if session.status == "closed" and not session.pending:
            self.store.forget_session(session)
            self.sessions.pop(session_id, None)

    def on_welcome(self, server_sessions: dict) -> None:
        """Align with what Celius has: its last stored seq per session, and sessions this
        agent lost (reinstall) are restored from their payload."""
        for session_id, info in (server_sessions or {}).items():
            session = self.sessions.get(session_id)
            if session is None:
                if not isinstance(info, dict) or info.get("status") in ("failed", "terminated"):
                    continue
                session = self._session_from_payload(session_id, info)
                if session is None:
                    continue
            self.store.adopt_server_seq(session, int(info.get("last_seq") or 0))

    def _session_from_payload(self, session_id: str, payload: dict) -> SessionState | None:
        session = SessionState(
            id=session_id,
            origin=payload.get("origin") or "portal",
            working_directory=payload.get("working_directory") or "",
            permissions=payload.get("permissions") or {},
            next_seq=max(1, int(payload.get("next_seq") or 1)),
            acked=max(0, int(payload.get("next_seq") or 1) - 1),
            status="opening",
        )
        self.sessions[session_id] = session
        if payload.get("status") not in (None, "opening"):
            # It was open before this agent lost its state: check the folder again.
            try:
                session.working_directory, session.writable = check_working_directory(
                    session.working_directory, self.policy.roots
                )
                session.status = "open"
            except PathRefused:
                session.status = "closed"
        self.store.save_session(session)
        return session

    async def recover_interrupted(self) -> None:
        """Commands that were running when the agent stopped: report, never re-run."""
        for entry in self.store.journal_unfinished():
            session_id = entry.get("session_id")
            if entry.get("kind") in ("tool.execute", "file.fetch") and session_id in self.sessions:
                await self.emit(
                    session_id,
                    "command.completed",
                    {
                        "command_id": entry["id"],
                        "status": "failed",
                        "error": "The agent restarted while this command ran; it was not "
                        "run again. Check its effects before retrying.",
                    },
                )
            self.store.journal_put(entry["id"], {**entry, "state": "done"})

    # ------------------------------------------------------------------ commands

    async def handle_command(self, frame: dict) -> None:
        command_id = str(frame.get("id") or "")
        kind = frame.get("kind")
        session_id = frame.get("session_id")
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if not command_id:
            return
        try:
            entry = self.store.journal_get(command_id)
        except ValueError:
            log.warning("Ignoring a command with an invalid id")
            return
        if entry is not None:
            if entry.get("state") == "started" and command_id not in self.running:
                await self.recover_interrupted()
            return  # seen before: never run twice
        if kind == "agent.disconnect":
            self.disconnect_requested = True
            self.wakeup.set()
            return
        self.store.journal_put(
            command_id,
            {"id": command_id, "kind": kind, "session_id": session_id, "state": "started"},
        )
        task = asyncio.create_task(
            self._execute(command_id, kind, session_id, payload, frame.get("run_id"))
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _execute(
        self,
        command_id: str,
        kind: str | None,
        session_id: str | None,
        payload: dict,
        run_id: str | None,
    ) -> None:
        try:
            if kind == "session.open":
                await self._open(command_id, session_id, payload)
            elif kind == "session.close":
                await self._close(session_id, payload)
            elif kind in ("tool.execute", "file.fetch"):
                await self._tool(command_id, kind, session_id, payload, run_id)
            elif kind == "cancel":
                env = self.running.get(str(payload.get("command_id")))
                if env is not None:
                    env.cancel.set()
            elif kind == "interrupt":
                await self._interrupt(session_id)
            else:
                log.warning("Unknown command kind %r", kind)
        except Exception:
            log.exception("Command %s (%s) failed", command_id, kind)
            if kind in ("tool.execute", "file.fetch") and session_id in self.sessions:
                await self.emit(
                    session_id,
                    "command.completed",
                    {"command_id": command_id, "status": "failed", "error": "The agent failed"},
                    run_id,
                )
        finally:
            self.store.journal_put(
                command_id,
                {"id": command_id, "kind": kind, "session_id": session_id, "state": "done"},
            )

    # ------------------------------------------------------------------ sessions

    async def _open(self, command_id: str, session_id: str | None, payload: dict) -> None:
        if not session_id:
            return
        session = self.sessions.get(session_id)
        if session is not None and session.status == "open":
            return  # already open (a resent command after a lost ack)
        if session is None:
            session = SessionState(
                id=session_id,
                next_seq=max(1, int(payload.get("next_seq") or 1)),
                acked=max(0, int(payload.get("next_seq") or 1) - 1),
            )
            self.sessions[session_id] = session
        session.origin = payload.get("origin") or "portal"
        session.permissions = payload.get("permissions") or {}
        self.reload_policy()
        try:
            directory, writable = check_working_directory(
                str(payload.get("working_directory") or ""), self.policy.roots
            )
        except PathRefused as exc:
            session.status = "closed"
            self.store.save_session(session)
            await self.emit(
                session_id, "session.failed", {"command_id": command_id, "reason": str(exc)}
            )
            return
        session.working_directory, session.writable = directory, writable
        session.status = "open"
        self.store.save_session(session)
        await self.emit(
            session_id,
            "session.opened",
            {
                "command_id": command_id,
                "working_directory": directory,
                "writable": writable,
                "shell": shell_name(),
            },
        )

    def _stop_session_work(self, session_id: str) -> None:
        for env in self.running.values():
            if env.session_id == session_id:
                env.cancel.set()

    async def _close(self, session_id: str | None, payload: dict) -> None:
        session = self.sessions.get(session_id or "")
        if session is None or session.status == "closed":
            return
        self._stop_session_work(session.id)
        session.status = "closed"
        self.store.save_session(session)
        await self.emit(session.id, "session.closed", {"reason": payload.get("reason") or "closed"})

    async def _interrupt(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._stop_session_work(session_id)

    # ------------------------------------------------------------------ tools

    async def _tool(
        self,
        command_id: str,
        kind: str,
        session_id: str | None,
        payload: dict,
        run_id: str | None,
    ) -> None:
        session = self.sessions.get(session_id or "")
        if session is None:
            log.warning("Command %s for unknown session %s", command_id, session_id)
            return

        async def emit(type_: str, data: dict) -> None:
            await self.emit(session.id, type_, data, run_id)

        if session.status != "open":
            outcome = error("This session is not open on the machine")
        else:
            env = ToolEnv(
                workdir=session.working_directory,
                writable=session.writable,
                permissions=session.permissions,
                policy=self.policy,
                emit=emit,
                command_id=command_id,
                python=self.python,
                session_id=session.id,
            )
            name = payload.get("tool") if kind == "tool.execute" else "file.fetch"
            arguments = payload.get("arguments") if kind == "tool.execute" else payload
            handler = fetch_file if kind == "file.fetch" else TOOLS.get(str(name))
            if handler is None:
                outcome = error(f"There is no tool named {name!r} on this machine")
            elif not isinstance(arguments, dict):
                outcome = error("Arguments must be an object")
            else:
                seconds = payload.get("timeout_seconds")
                if isinstance(seconds, (int, float)) and "timeout_seconds" not in arguments:
                    arguments = {**arguments, "timeout_seconds": min(seconds, MAX_TOOL_TIMEOUT)}
                async with self.semaphore:
                    self.running[command_id] = env
                    await emit("command.started", {"command_id": command_id, "tool": name})
                    try:
                        outcome = await handler(env, arguments)
                    finally:
                        self.running.pop(command_id, None)
        content = outcome.get("content")
        if isinstance(content, str) and len(content) > MAX_RESULT_CHARS:
            # Celius gives the model far less than this; the rest only costs bandwidth.
            outcome = {**outcome, "content": content[:MAX_RESULT_CHARS] + "\n[…output cut]"}
        await emit(
            "command.completed",
            {"command_id": command_id, "status": "completed", "result": outcome},
        )

    async def shutdown(self) -> None:
        for env in self.running.values():
            env.cancel.set()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=10)

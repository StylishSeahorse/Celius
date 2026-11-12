"""Durable agent state: per-session event outboxes and the command journal.

Outbox: every event gets the session's next sequence number and is appended to
`state/outbox/<session>.jsonl` *before* it is sent. Celius acknowledges the highest
contiguous sequence it stored; acknowledged events are dropped. After a network drop or
a restart, everything not acknowledged is sent again (Celius ignores what it already has).

Journal: `state/commands/<id>.json` is written before a command runs. A command id that
is delivered again is never run again; one that was running when the agent stopped is
reported as interrupted, never re-run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
COMPACT_AFTER = 200  # acked lines in an outbox file before it is rewritten
# One event must always fit in one WebSocket frame (Celius accepts 16 MB): bigger ones are
# trimmed when they are written, never left to block the outbox.
MAX_EVENT_BYTES = 8 * 1024 * 1024
TRIMMED_TEXT_CHARS = 200_000
JOURNAL_TTL_SECONDS = 7 * 24 * 3600
# Events worth an fsync: losing one would leave Celius waiting.
DURABLE_TYPES = frozenset(
    {
        "session.opened",
        "session.failed",
        "session.closed",
        "command.completed",
        "permission.request",
        "turn.completed",
    }
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionState:
    id: str
    mode: str = "native"
    adapter: str = "celius"
    working_directory: str = ""
    writable: bool = False
    permissions: dict = field(default_factory=dict)
    external_session_id: str | None = None
    status: str = "opening"  # opening | open | closed
    next_seq: int = 1
    acked: int = 0
    pending: list[dict] = field(default_factory=list, repr=False)
    acked_lines: int = 0

    def meta(self) -> dict:
        data = asdict(self)
        data.pop("pending")
        data.pop("acked_lines")
        return data


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.sessions_dir = root / "sessions"
        self.outbox_dir = root / "outbox"
        self.commands_dir = root / "commands"
        for directory in (self.sessions_dir, self.outbox_dir, self.commands_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ sessions

    def load_sessions(self) -> dict[str, SessionState]:
        sessions: dict[str, SessionState] = {}
        for meta_path in self.sessions_dir.glob("*.json"):
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            known = {k: v for k, v in raw.items() if k in SessionState.__dataclass_fields__}
            session = SessionState(**known)
            for event in self._read_outbox(session.id):
                if event["seq"] > session.acked:
                    session.pending.append(event)
                session.next_seq = max(session.next_seq, event["seq"] + 1)
            session.pending.sort(key=lambda e: e["seq"])
            sessions[session.id] = session
        return sessions

    def _read_outbox(self, session_id: str) -> list[dict]:
        path = self.outbox_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    break  # a torn last line after a crash: everything before it is valid
                if isinstance(event, dict) and isinstance(event.get("seq"), int):
                    events.append(event)
        return events

    def save_session(self, session: SessionState) -> None:
        _atomic_write(self.sessions_dir / f"{session.id}.json", json.dumps(session.meta()))

    def append(
        self, session: SessionState, type_: str, data: dict, run_id: str | None = None
    ) -> dict:
        event = {
            "seq": session.next_seq,
            "type": type_,
            "occurred_at": now_iso(),
            "data": data,
        }
        if run_id:
            event["run_id"] = run_id
        line = json.dumps(event, separators=(",", ":"))
        if len(line.encode()) > MAX_EVENT_BYTES:
            event["data"] = _trim(data)
            line = json.dumps(event, separators=(",", ":"))
        session.next_seq += 1
        path = self.outbox_dir / f"{session.id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            if type_ in DURABLE_TYPES:
                os.fsync(handle.fileno())
        session.pending.append(event)
        return event

    def ack(self, session: SessionState, seq: int) -> None:
        if seq <= session.acked:
            return
        dropped = sum(1 for e in session.pending if e["seq"] <= seq)
        session.pending = [e for e in session.pending if e["seq"] > seq]
        session.acked = seq
        session.next_seq = max(session.next_seq, seq + 1)
        session.acked_lines += dropped
        self.save_session(session)
        if not session.pending or session.acked_lines >= COMPACT_AFTER:
            self._rewrite_outbox(session)

    def adopt_server_seq(self, session: SessionState, server_last_seq: int) -> None:
        """Celius already has events up to `server_last_seq` (e.g. after a reinstall)."""
        if server_last_seq > session.acked:
            self.ack(session, server_last_seq)

    def _rewrite_outbox(self, session: SessionState) -> None:
        path = self.outbox_dir / f"{session.id}.jsonl"
        lines = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in session.pending)
        _atomic_write(path, lines)
        session.acked_lines = 0

    def forget_session(self, session: SessionState) -> None:
        for path in (
            self.sessions_dir / f"{session.id}.json",
            self.outbox_dir / f"{session.id}.jsonl",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ journal

    def journal_get(self, command_id: str) -> dict | None:
        path = self.commands_dir / f"{_safe_id(command_id)}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def journal_put(self, command_id: str, entry: dict) -> None:
        path = self.commands_dir / f"{_safe_id(command_id)}.json"
        _atomic_write(path, json.dumps({**entry, "updated": time.time()}), fsync=True)

    def journal_unfinished(self) -> list[dict]:
        unfinished = []
        cutoff = time.time() - JOURNAL_TTL_SECONDS
        for path in self.commands_dir.glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if entry.get("updated", 0) < cutoff:
                path.unlink(missing_ok=True)
                continue
            if entry.get("state") == "started":
                unfinished.append(entry)
        return unfinished


def _trim(value, depth: int = 0):
    """Shorten long strings (and drop embedded file contents) until the event fits."""
    if isinstance(value, dict):
        return {
            k: ("[removed: too large to send]" if k == "content_b64" else _trim(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_trim(v, depth + 1) for v in value[:1000]]
    if isinstance(value, str) and len(value) > TRIMMED_TEXT_CHARS:
        return value[:TRIMMED_TEXT_CHARS] + f"\n[…{len(value) - TRIMMED_TEXT_CHARS} characters cut]"
    return value


def _safe_id(command_id: str) -> str:
    cleaned = "".join(c for c in command_id if c.isalnum() or c == "-")
    if not cleaned or len(cleaned) > 64:
        raise ValueError("invalid command id")
    return cleaned


def _atomic_write(path: Path, text: str, *, fsync: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    if not IS_WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)

"""The server side of an agent's WebSocket (ADR C-16, docs/remote/ARCHITECTURE.md §4).

One connection per machine. Accepting a connection writes a fresh `connection_id` on the
machine's row: an older socket that sees a different id closes itself (newest wins, which
also clears half-open sockets after a network drop). Two loops share the socket:

- reader: `heartbeat`, `events` (stored, acked), `bye`;
- writer: delivers pending commands, expires stale ones, and watches for revocation and
  supersession.

Commands are delivered at least once: `delivered` ones are re-sent on every new
connection; the agent never runs an id twice.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import or_, select, update

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.models import RemoteAgent, RemoteCommand, RemoteSession
from app.models.remote import ENDED_SESSION_STATUSES
from app.services.audit import audit
from app.services.remote.ingest import ingest_events
from app.services.remote.sessions import session_payload

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
HELLO_TIMEOUT_SECONDS = 15
CLOSE_REVOKED = 4401
CLOSE_SUPERSEDED = 4409
CLOSE_PROTOCOL = 4400
COMMAND_BATCH = 50


class Socket(Protocol):
    async def receive_text(self) -> str: ...
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class _Closed(Exception):
    def __init__(self, code: int, reason: str):
        self.code, self.reason = code, reason


def _bounded_dict(value: Any, limit: int = 64_000) -> dict:
    if not isinstance(value, dict):
        return {}
    return value if len(json.dumps(value, default=str)) <= limit else {}


def clean_local_policy(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    roots = []
    for root in raw.get("roots") or []:
        if isinstance(root, dict) and isinstance(root.get("path"), str):
            roots.append({"path": root["path"][:1000], "write": bool(root.get("write", True))})
        elif isinstance(root, str):
            roots.append({"path": root[:1000], "write": True})
    adapters = [a for a in raw.get("adapters") or [] if a in ("celius", "claude_code")]
    return {
        "roots": roots[:50],
        "allow_shell": bool(raw.get("allow_shell", False)),
        "allow_write": bool(raw.get("allow_write", False)),
        "adapters": adapters,
        "max_concurrent_commands": raw.get("max_concurrent_commands"),
    }


class AgentConnection:
    def __init__(
        self,
        socket: Socket,
        factory: SessionFactory,
        settings: Settings,
        agent_id: uuid.UUID,
        ip_address: str | None,
    ):
        self.socket = socket
        self.factory = factory
        self.settings = settings
        self.agent_id = agent_id
        self.ip_address = ip_address
        self.connection_id = uuid.uuid4().hex
        self.send_lock = asyncio.Lock()
        self.clean = False
        self.close_reason = "lost"

    async def send(self, frame: dict) -> None:
        async with self.send_lock:
            await self.socket.send_text(json.dumps(frame, default=str))

    # ------------------------------------------------------------------ lifecycle

    async def serve(self) -> None:
        try:
            hello = await asyncio.wait_for(self._receive(), HELLO_TIMEOUT_SECONDS)
        except (TimeoutError, _Closed):
            await self._close(CLOSE_PROTOCOL, "expected hello")
            return
        if hello.get("type") != "hello":
            await self._close(CLOSE_PROTOCOL, "expected hello")
            return
        welcome = await self._accept(hello)
        if welcome is None:
            await self._close(CLOSE_REVOKED, "machine not authorised")
            return
        await self.send(welcome)
        reader = asyncio.create_task(self._reader())
        writer = asyncio.create_task(self._writer())
        try:
            done, pending = await asyncio.wait(
                {reader, writer}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if isinstance(exc, _Closed):
                    self.close_reason = exc.reason
                    await self._close(exc.code, exc.reason)
                elif exc is not None and not isinstance(exc, ConnectionError):
                    log.warning("Agent %s connection error: %r", self.agent_id, exc)
        finally:
            await self._detach()

    async def _accept(self, hello: dict) -> dict | None:
        now = utcnow()
        async with self.factory() as db:
            agent = await db.scalar(
                select(RemoteAgent).where(RemoteAgent.id == self.agent_id).with_for_update()
            )
            if agent is None or agent.status != "active":
                return None
            agent.connection_id = self.connection_id
            agent.connected_at = now
            agent.last_seen_at = now
            agent.disconnected_at = None
            agent.disconnect_reason = None
            agent.last_ip = self.ip_address
            agent.agent_version = str(hello.get("agent_version") or "")[:32] or agent.agent_version
            agent.protocol_version = (
                hello.get("protocol") if isinstance(hello.get("protocol"), int) else None
            )
            for field in ("hostname", "os", "os_version", "arch"):
                value = hello.get(field)
                if isinstance(value, str) and value:
                    setattr(agent, field, value[:200])
            agent.capabilities = _bounded_dict(hello.get("capabilities"))
            agent.local_policy = clean_local_policy(hello.get("local_policy"))
            if isinstance(hello.get("resources"), dict):
                agent.resources = _bounded_dict(hello["resources"])
            # Anything sent to an earlier connection and not finished is sent again; the
            # agent recognises ids it already ran.
            await db.execute(
                update(RemoteCommand)
                .where(RemoteCommand.agent_id == agent.id, RemoteCommand.status == "delivered")
                .values(status="pending")
            )
            sessions = (
                await db.scalars(
                    select(RemoteSession).where(
                        RemoteSession.agent_id == agent.id,
                        RemoteSession.status.not_in(ENDED_SESSION_STATUSES),
                    )
                )
            ).all()
            audit(
                db,
                agent.owner_id,
                "remote_agent.connected",
                "remote_agent",
                agent.id,
                self.ip_address,
                version=agent.agent_version,
            )
            await db.commit()
        return {
            "type": "welcome",
            "protocol": PROTOCOL_VERSION,
            "agent_id": str(self.agent_id),
            "connection_id": self.connection_id,
            "heartbeat_seconds": self.settings.remote_heartbeat_seconds,
            "server_time": now.isoformat(),
            # Enough for an agent that lost its state (reinstalled) to restore its sessions.
            "sessions": {
                str(s.id): {
                    "last_seq": s.last_seq,
                    "status": s.status,
                    **session_payload(s, agent),
                }
                for s in sessions
            },
        }

    async def _detach(self) -> None:
        async with self.factory() as db:
            agent = await db.scalar(
                select(RemoteAgent).where(RemoteAgent.id == self.agent_id).with_for_update()
            )
            if agent is not None and agent.connection_id == self.connection_id:
                agent.connection_id = None
                agent.disconnected_at = utcnow()
                agent.disconnect_reason = "clean" if self.clean else self.close_reason
                audit(
                    db,
                    agent.owner_id,
                    "remote_agent.disconnected",
                    "remote_agent",
                    agent.id,
                    self.ip_address,
                    reason=agent.disconnect_reason,
                )
            await db.commit()

    async def _close(self, code: int, reason: str) -> None:
        try:
            await self.socket.close(code=code, reason=reason)
        except Exception:
            pass

    async def _receive(self) -> dict:
        try:
            text = await self.socket.receive_text()
        except Exception as exc:  # disconnect
            raise ConnectionError("socket closed") from exc
        if len(text) > self.settings.remote_max_frame_bytes:
            raise _Closed(CLOSE_PROTOCOL, "frame too large")
        try:
            frame = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _Closed(CLOSE_PROTOCOL, "invalid JSON") from exc
        if not isinstance(frame, dict):
            raise _Closed(CLOSE_PROTOCOL, "frame must be an object")
        return frame

    # ------------------------------------------------------------------ reader

    async def _reader(self) -> None:
        while True:
            frame = await self._receive()
            kind = frame.get("type")
            if kind == "heartbeat":
                await self._heartbeat(frame)
            elif kind == "events":
                await self._events(frame)
            elif kind == "bye":
                self.clean = True
                return
            elif kind == "ping":
                await self.send({"type": "pong"})
            else:
                await self.send({"type": "error", "code": "unknown_frame", "frame": kind})

    async def _heartbeat(self, frame: dict) -> None:
        async with self.factory() as db:
            values: dict = {"last_seen_at": utcnow()}
            if isinstance(frame.get("resources"), dict):
                values["resources"] = _bounded_dict(frame["resources"])
            await db.execute(
                update(RemoteAgent)
                .where(
                    RemoteAgent.id == self.agent_id,
                    RemoteAgent.connection_id == self.connection_id,
                )
                .values(**values)
            )
            await db.commit()

    async def _events(self, frame: dict) -> None:
        try:
            session_id = uuid.UUID(str(frame.get("session_id")))
        except ValueError:
            await self.send({"type": "error", "code": "invalid_session"})
            return
        events = frame.get("events")
        if not isinstance(events, list):
            await self.send({"type": "error", "code": "invalid_events"})
            return
        events = events[: self.settings.remote_max_events_per_batch]
        async with self.factory() as db:
            agent = await db.get(RemoteAgent, self.agent_id)
            if agent is None or agent.connection_id != self.connection_id:
                raise _Closed(CLOSE_SUPERSEDED, "superseded")
            result = await ingest_events(db, agent, session_id, events)
            agent.last_seen_at = utcnow()
            await db.commit()
        frame_out = {
            "type": "ack",
            "session_id": str(session_id),
            "seq": result.last_seq,
        }
        if result.error:
            frame_out["error"] = result.error
        await self.send(frame_out)

    # ------------------------------------------------------------------ writer

    async def _writer(self) -> None:
        while True:
            await self._deliver()
            await asyncio.sleep(self.settings.remote_poll_interval_seconds)

    async def _deliver(self) -> None:
        now = utcnow()
        async with self.factory() as db:
            agent = await db.get(RemoteAgent, self.agent_id)
            if agent is None or agent.status != "active":
                raise _Closed(CLOSE_REVOKED, "revoked")
            if agent.connection_id != self.connection_id:
                raise _Closed(CLOSE_SUPERSEDED, "superseded")
            # Never deliver what nobody waits for any more.
            await db.execute(
                update(RemoteCommand)
                .where(
                    RemoteCommand.agent_id == self.agent_id,
                    RemoteCommand.status == "pending",
                    RemoteCommand.expires_at.is_not(None),
                    RemoteCommand.expires_at < now,
                )
                .values(status="expired", completed_at=now, error="Not delivered in time")
            )
            commands = (
                await db.scalars(
                    select(RemoteCommand)
                    .where(
                        RemoteCommand.agent_id == self.agent_id,
                        RemoteCommand.status == "pending",
                        or_(RemoteCommand.expires_at.is_(None), RemoteCommand.expires_at >= now),
                    )
                    .order_by(RemoteCommand.created_at, RemoteCommand.id)
                    .limit(COMMAND_BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for command in commands:
                command.status = "delivered"
                command.delivered_at = now
                if command.kind == "session.open" and command.session_id:
                    # Permissions as they are now (the machine's policy may have arrived or
                    # changed since the session was created).
                    session = await db.get(RemoteSession, command.session_id)
                    if session is not None:
                        command.payload = session_payload(session, agent)
                if command.kind == "agent.disconnect":
                    # Nothing to report back; it must not be re-sent on the next connection.
                    command.status = "completed"
                    command.completed_at = now
            await db.commit()
        for command in commands:
            await self.send(
                {
                    "type": "command",
                    "id": str(command.id),
                    "kind": command.kind,
                    "session_id": str(command.session_id) if command.session_id else None,
                    "run_id": str(command.run_id) if command.run_id else None,
                    "payload": command.payload,
                }
            )
            if command.kind == "agent.disconnect":
                raise _Closed(1000, "clean")

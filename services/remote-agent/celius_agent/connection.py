"""The connection to Celius: token exchange over HTTPS, then one outbound WebSocket.

Reconnects forever with exponential backoff and full jitter (1 s → 60 s, reset after a
connection that lasted a minute). Close codes from Celius: 4401 the machine or its token
is not (or no longer) valid; 4409 a newer connection with this identity replaced this one
(two agents running with the same credential: waits longer to avoid flapping).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.request

from celius_agent import PROTOCOL_VERSION, __version__
from celius_agent.config import AgentConfig, websocket_url
from celius_agent.monitor import Monitor, machine_facts
from celius_agent.runtime import Runtime

log = logging.getLogger("celius_agent.connection")

BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
STABLE_SECONDS = 60
SEND_BATCH = 200
# Bytes per events frame (one event may exceed it alone; state.py caps single events).
SEND_BATCH_BYTES = 2 * 1024 * 1024
MAX_IN_FLIGHT = 2000
TOKEN_MARGIN_SECONDS = 60


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def ssl_context(config: AgentConfig) -> ssl.SSLContext:
    return (
        ssl.create_default_context(cafile=config.ca_file)
        if config.ca_file
        else ssl.create_default_context()
    )


def post_json(
    config: AgentConfig, path: str, body: dict, token: str | None = None, timeout: float = 20
) -> dict:
    """Blocking JSON POST (run in a thread). Raises AuthError on 4xx."""
    request = urllib.request.Request(
        config.server_url.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"celius-agent/{__version__}",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    context = ssl_context(config) if config.server_url.startswith("https") else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("error", {})
        except (json.JSONDecodeError, AttributeError):
            detail = {}
        raise AuthError(
            exc.code, detail.get("code", "http_error"), detail.get("message") or str(exc)
        ) from exc


class Connection:
    def __init__(self, config: AgentConfig, runtime: Runtime, credential: str, monitor: Monitor):
        self.config = config
        self.runtime = runtime
        self.credential = credential
        self.monitor = monitor
        self.token: str | None = None
        self.token_expires = 0
        self.sent_upto: dict[str, int] = {}
        self.heartbeat_seconds = 15
        self.connected = asyncio.Event()

    async def fetch_token(self) -> str:
        if self.token and self.token_expires - TOKEN_MARGIN_SECONDS > time.time():
            return self.token
        data = await asyncio.to_thread(
            post_json, self.config, "/api/v1/remote/token", {"credential": self.credential}
        )
        self.token, self.token_expires = data["access_token"], int(data["expires_at"])
        return self.token

    async def run_forever(self, stop: asyncio.Event) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        backoff = BACKOFF_START
        url = websocket_url(self.config.server_url, "/api/v1/remote/agent/connect")
        while not stop.is_set():
            started = time.monotonic()
            wait_hint = None
            try:
                token = await self.fetch_token()
                async with connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    user_agent_header=f"celius-agent/{__version__}",
                    ssl=ssl_context(self.config) if url.startswith("wss") else None,
                    open_timeout=20,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    log.info("Connected to %s", self.config.server_url)
                    await self._session(ws, stop)
            except AuthError as exc:
                self.token = None
                if exc.code == "agent_not_authorized":
                    log.warning("Waiting for this machine to be authorised in Celius")
                    wait_hint = 15
                elif exc.status == 401:
                    log.error("Celius rejected this machine's credential (%s): %s", exc.code, exc)
                    wait_hint = 300
                else:
                    log.warning("Token request failed: %s", exc)
            except InvalidStatus as exc:
                log.warning("WebSocket refused: HTTP %s", exc.response.status_code)
                self.token = None
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                log.warning("Connection closed (%s %s)", code, exc.rcvd.reason if exc.rcvd else "")
                if code == 4401:
                    self.token = None
                elif code == 4409:
                    log.error(
                        "Another agent with this machine's identity connected; is the agent "
                        "running twice?"
                    )
                    wait_hint = 30
            except (OSError, asyncio.TimeoutError) as exc:
                log.warning("Can't reach %s: %s", self.config.server_url, exc)
            except Exception:
                log.exception("Connection error")
            finally:
                self.connected.clear()
            if stop.is_set():
                break
            if self.runtime.disconnect_requested:
                self.runtime.disconnect_requested = False
                wait_hint = 2
            if time.monotonic() - started >= STABLE_SECONDS:
                backoff = BACKOFF_START
            delay = wait_hint if wait_hint is not None else random.uniform(0, backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _resources(self) -> dict:
        return self.monitor.snapshot(
            running_commands=len(self.runtime.running),
            open_sessions=self.runtime.open_sessions(),
        )

    async def _session(self, ws, stop: asyncio.Event) -> None:
        facts = machine_facts()
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": PROTOCOL_VERSION,
                    **facts,
                    "capabilities": self.runtime.capabilities(),
                    "local_policy": self.runtime.policy.report(),
                    "resources": self._resources(),
                    "sessions": {s.id: s.acked for s in self.runtime.sessions.values()},
                }
            )
        )
        welcome = json.loads(await asyncio.wait_for(ws.recv(), 20))
        if welcome.get("type") != "welcome":
            raise ConnectionError(f"expected welcome, got {welcome.get('type')}")
        self.heartbeat_seconds = int(welcome.get("heartbeat_seconds") or 15)
        self.runtime.on_welcome(welcome.get("sessions") or {})
        # Everything not acknowledged is sent again on this connection.
        self.sent_upto = {s.id: s.acked for s in self.runtime.sessions.values()}
        self.connected.set()
        self.runtime.wakeup.set()
        tasks = [
            asyncio.create_task(self._receiver(ws)),
            asyncio.create_task(self._sender(ws)),
            asyncio.create_task(self._heartbeat(ws)),
            asyncio.create_task(stop.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception():
                    raise task.exception()
            if stop.is_set():
                await self._flush(ws)
                await ws.send(json.dumps({"type": "bye"}))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = frame.get("type")
            if kind == "command":
                await self.runtime.handle_command(frame)
                if self.runtime.disconnect_requested:
                    log.info("Celius asked this agent to reconnect")
                    await ws.close()
                    return
            elif kind == "ack":
                session_id = str(frame.get("session_id"))
                seq = int(frame.get("seq") or 0)
                self.runtime.on_ack(session_id, seq, frame.get("error"))
                if self.sent_upto.get(session_id, 0) > seq:
                    # Celius held back after a gap, or stored less: resend from the ack.
                    self.sent_upto[session_id] = seq
                    self.runtime.wakeup.set()
            elif kind == "error":
                log.warning("Celius reported: %s", frame)

    async def _flush(self, ws) -> bool:
        sent = False
        for session in list(self.runtime.sessions.values()):
            upto = max(self.sent_upto.get(session.id, session.acked), session.acked)
            if upto - session.acked >= MAX_IN_FLIGHT:
                continue  # wait for acks before sending more
            batch: list[dict] = []
            size = 0
            for event in session.pending:
                if event["seq"] <= upto:
                    continue
                event_size = len(json.dumps(event, separators=(",", ":")))
                if batch and (len(batch) >= SEND_BATCH or size + event_size > SEND_BATCH_BYTES):
                    break
                batch.append(event)
                size += event_size
            if not batch:
                continue
            await ws.send(json.dumps({"type": "events", "session_id": session.id, "events": batch}))
            self.sent_upto[session.id] = batch[-1]["seq"]
            sent = True
        return sent

    async def _sender(self, ws) -> None:
        while True:
            self.runtime.wakeup.clear()
            if await self._flush(ws):
                continue
            # Within one connection every frame arrives and is acked; after a drop the
            # next connection resends from the last ack (see _session).
            try:
                await asyncio.wait_for(self.runtime.wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await ws.send(json.dumps({"type": "heartbeat", "resources": self._resources()}))

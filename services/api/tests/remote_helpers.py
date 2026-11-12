"""Helpers for Celius Remote tests: an in-process ASGI WebSocket client and shortcuts to
register and authorise a machine."""

import asyncio
import json
import uuid

from sqlalchemy import select

from app.models import RemoteAgent


class WSClosed(Exception):
    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"closed {code} {reason}")
        self.code, self.reason = code, reason


class ASGIWebSocket:
    """Drives an ASGI app's WebSocket endpoint directly, on the test's event loop."""

    def __init__(self, app, path: str, headers: dict[str, str] | None = None):
        self.app = app
        self.path = path
        self.headers = headers or {}
        self.to_app: asyncio.Queue = asyncio.Queue()
        self.from_app: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def __aenter__(self) -> "ASGIWebSocket":
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": self.path,
            "raw_path": self.path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in self.headers.items()],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
            "subprotocols": [],
        }
        self.task = asyncio.create_task(self.app(scope, self.to_app.get, self.from_app.put))
        await self.to_app.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self.from_app.get(), 5)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            raise WSClosed(self.close_code, message.get("reason") or "")
        assert message["type"] == "websocket.accept", message
        self.accepted = True
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def send_json(self, frame: dict) -> None:
        await self.to_app.put({"type": "websocket.receive", "text": json.dumps(frame)})

    async def receive_json(self, wait: float = 5) -> dict:
        message = await asyncio.wait_for(self.from_app.get(), wait)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            raise WSClosed(self.close_code, message.get("reason") or "")
        return json.loads(message["text"])

    async def receive_until(self, kind: str, wait: float = 5) -> dict:
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            left = deadline - asyncio.get_running_loop().time()
            frame = await self.receive_json(max(left, 0.01))
            if frame.get("type") == kind:
                return frame

    async def disconnect(self) -> None:
        if self.task is None:
            return
        if not self.task.done():
            await self.to_app.put({"type": "websocket.disconnect", "code": 1000})
            try:
                await asyncio.wait_for(self.task, 5)
            except (TimeoutError, Exception):
                self.task.cancel()
        self.task = None


async def register_machine(
    client, headers, *, authorize: bool = True, name: str = "Workstation", os: str = "linux"
) -> dict:
    """Pairing code → enroll → (authorise). Returns ids, credential and an access token."""
    response = await client.post("/api/v1/remote/enrollments", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    code = response.json()["code"]
    response = await client.post(
        "/api/v1/remote/enroll",
        json={
            "code": code,
            "hostname": f"{name.lower()}-host",
            "os": os,
            "os_version": "test",
            "arch": "x86_64",
            "agent_version": "0.1.0",
            "protocol_version": 1,
        },
    )
    assert response.status_code == 201, response.text
    enrolled = response.json()
    result = {"agent_id": enrolled["agent_id"], "credential": enrolled["credential"]}
    if authorize:
        response = await client.post(
            f"/api/v1/remote/agents/{enrolled['agent_id']}/authorize", headers=headers
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/v1/remote/token", json={"credential": enrolled["credential"]}
        )
        assert response.status_code == 200, response.text
        result["token"] = response.json()["access_token"]
    return result


def hello(**overrides) -> dict:
    frame = {
        "type": "hello",
        "protocol": 1,
        "agent_version": "0.1.0",
        "os": "linux",
        "hostname": "workstation-host",
        "capabilities": {"python": "3.12", "shell": "bash"},
        "local_policy": {
            "roots": [{"path": "/srv/work", "write": True}],
            "allow_shell": True,
            "allow_write": True,
            "adapters": ["celius"],
        },
        "resources": {"cpu_percent": 5.0},
    }
    frame.update(overrides)
    return frame


async def machine_row(sessionmaker, agent_id: str) -> RemoteAgent:
    async with sessionmaker() as db:
        return await db.scalar(select(RemoteAgent).where(RemoteAgent.id == uuid.UUID(agent_id)))

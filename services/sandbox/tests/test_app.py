import asyncio
import os
import socket
import subprocess
import sys

import httpx
import pytest

from sandbox.app import create_app
from sandbox.config import SandboxConfig

from .conftest import TOKEN

AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as c:
            yield c


def test_refuses_to_start_without_a_token(config):
    config.token = "short"
    with pytest.raises(RuntimeError):
        create_app(config)


async def test_health_needs_no_token(client):
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["mode"] in ("bwrap", "process")


@pytest.mark.parametrize(
    "header", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
async def test_executions_need_the_token(client, header):
    response = await client.post(
        "/v1/executions",
        json={"workspace": "w", "kind": "python", "code": "print(1)"},
        headers=header,
    )
    assert response.status_code == 401


async def test_execute(client):
    response = await client.post(
        "/v1/executions",
        json={"workspace": "w", "kind": "python", "code": "print(6 * 7)"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] == "42\n"
    assert body["exit_code"] == 0


async def test_bad_requests(client):
    bad_kind = await client.post(
        "/v1/executions", json={"workspace": "w", "kind": "ruby", "code": ""}, headers=AUTH
    )
    assert bad_kind.status_code == 422
    bad_workspace = await client.post(
        "/v1/executions", json={"workspace": "../x", "kind": "shell", "code": ""}, headers=AUTH
    )
    assert bad_workspace.status_code == 422


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_closing_the_connection_kills_the_code(config, tmp_path):
    """A real server: the caller (the worker) cancelling its request stops the code."""
    port = _free_port()
    env = {
        **os.environ,
        "SANDBOX_TOKEN": TOKEN,
        "SANDBOX_WORKSPACES": str(config.workspaces_root),
        "SANDBOX_SCRATCH": str(config.scratch_root),
    }
    server = subprocess.Popen(  # noqa: ASYNC220 -- test harness
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sandbox.app:create_app",
            "--factory",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base) as c:
            for _ in range(100):
                try:
                    await c.get("/v1/health")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
            request = asyncio.create_task(
                c.post(
                    "/v1/executions",
                    json={
                        "workspace": "w",
                        "kind": "python",
                        "code": "import time\ntime.sleep(3)\nopen('finished', 'w')",
                    },
                    headers=AUTH,
                    timeout=30,
                )
            )
            await asyncio.sleep(1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        await asyncio.sleep(3.5)
        assert not (config.workspaces_root / "w" / "finished").exists()
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "x" * 20)
    monkeypatch.setenv("SANDBOX_MAX_TIMEOUT_SECONDS", "7")
    config = SandboxConfig()
    assert config.token == "x" * 20
    assert config.max_timeout_seconds == 7

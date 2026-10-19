"""Shared fixtures for tool tests: the real sandbox service, in-process."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

SANDBOX_SRC = Path(__file__).resolve().parents[2] / "sandbox"
SANDBOX_TOKEN = "sandbox-test-token-0123456789"


@asynccontextmanager
async def sandbox_client(workspace_root: str, scratch: Path):
    """A SandboxClient talking to services/sandbox through ASGI (process mode)."""
    if str(SANDBOX_SRC) not in sys.path:
        sys.path.insert(0, str(SANDBOX_SRC))
    from sandbox.app import create_app
    from sandbox.config import SandboxConfig
    from sandbox.runner import become_subreaper

    from app.services.tools.sandbox_client import SandboxClient

    become_subreaper()
    Path(workspace_root).mkdir(parents=True, exist_ok=True)
    config = SandboxConfig(
        token=SANDBOX_TOKEN,
        workspaces_root=Path(workspace_root),
        scratch_root=scratch,
        mode="process",
        max_timeout_seconds=60,
        max_memory_mb=1024,
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        client = SandboxClient(
            "http://sandbox", SANDBOX_TOKEN, transport=httpx.ASGITransport(app=app)
        )
        try:
            yield client
        finally:
            await client.aclose()

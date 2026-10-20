"""Client for services/sandbox. Cancelling a call closes the request, which kills the code."""

from typing import Any

import httpx

from app.config import Settings


class SandboxUnavailable(Exception):
    pass


class SandboxClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            trust_env=False,
            timeout=httpx.Timeout(30, connect=5),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "SandboxClient | None":
        if not settings.sandbox_url or not settings.sandbox_token:
            return None
        return cls(settings.sandbox_url, settings.sandbox_token.get_secret_value())

    async def execute(
        self,
        workspace: str,
        kind: str,
        code: str,
        *,
        timeout_seconds: float,
        memory_mb: int,
        output_limit: int,
        network: bool | None = None,
    ) -> dict[str, Any]:
        seconds = max(1, int(timeout_seconds))
        try:
            response = await self._client.post(
                "/v1/executions",
                json={
                    "workspace": workspace,
                    "kind": kind,
                    "code": code,
                    "timeout_seconds": seconds,
                    "memory_mb": memory_mb,
                    "output_limit": output_limit,
                    "network": network,
                },
                # The sandbox enforces the timeout; this only guards against it hanging.
                timeout=httpx.Timeout(seconds + 30, connect=5),
            )
        except httpx.TransportError as exc:
            raise SandboxUnavailable("The sandbox is unreachable") from exc
        if response.status_code == 401:
            raise SandboxUnavailable("The sandbox rejected the worker's token (SANDBOX_TOKEN)")
        if response.status_code >= 400:
            raise SandboxUnavailable(f"The sandbox returned HTTP {response.status_code}")
        return response.json()

    async def health(self) -> dict[str, Any] | None:
        try:
            response = await self._client.get("/v1/health", timeout=5)
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    async def aclose(self) -> None:
        await self._client.aclose()

"""HTTPS client for the device's own API (the `celius` command line).

Authenticates as this machine: the stored credential is exchanged for a short-lived
access token, refreshed shortly before it expires. Blocking (urllib): the chat runs it on
its own thread.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from celius_agent import __version__
from celius_agent.config import AgentConfig
from celius_agent.connection import AuthError, post_json, ssl_context

TOKEN_MARGIN_SECONDS = 60


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class DeviceClient:
    def __init__(self, config: AgentConfig, credential: str):
        self.config = config
        self.credential = credential
        self._token: str | None = None
        self._expires = 0

    def token(self) -> str:
        if self._token and self._expires - TOKEN_MARGIN_SECONDS > time.time():
            return self._token
        try:
            data = post_json(self.config, "/api/v1/remote/token", {"credential": self.credential})
        except AuthError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc
        self._token, self._expires = data["access_token"], int(data["expires_at"])
        return self._token

    def _open(self, method: str, path: str, body: dict | None, accept: str, timeout: float):
        request = urllib.request.Request(
            self.config.server_url.rstrip("/") + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Accept": accept,
                "User-Agent": f"celius/{__version__}",
                "Authorization": f"Bearer {self.token()}",
            },
            method=method,
        )
        context = ssl_context(self.config) if self.config.server_url.startswith("https") else None
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error", {})
            except (json.JSONDecodeError, AttributeError):
                detail = {}
            if exc.code == 401:
                self._token = None
            raise ApiError(
                exc.code, detail.get("code", "http_error"), detail.get("message") or str(exc)
            ) from exc

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        with self._open(method, path, body, "application/json", timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def stream(self, path: str, timeout: float = 600) -> Iterator[dict]:
        """Server-Sent Events: yields each event's JSON data until the server closes."""
        with self._open("GET", path, None, "text/event-stream", timeout) as response:
            data: list[str] = []
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data:
                        yield json.loads("\n".join(data))
                        data = []
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())

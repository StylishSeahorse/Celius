"""Minimal MCP client (streamable HTTP transport, JSON-RPC 2.0).

Servers are registered by an administrator through the API, never by the model. Every
request goes through the SSRF guard (private addresses only when the administrator
allowed it for that server), and every tool call through the normal policy.
"""

import json
import re
from typing import Any

import httpx

from app.config import Settings
from app.models import McpServer
from app.services.secrets import unseal
from app.services.tools.netguard import (
    BlockedDestination,
    Resolver,
    check_url,
    guarded_client,
    system_resolver,
)
from app.services.tools.registry import ToolContext, ToolOutput, ToolSpec

PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PREFIX = "mcp__"


class McpError(Exception):
    pass


class McpClient:
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float = 30,
        allow_private: bool = False,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: str | None = None
        self._next_id = 0
        self._client = guarded_client(
            timeout=timeout, resolver=resolver, allow_private=allow_private, transport=transport
        )

    async def __aenter__(self) -> "McpClient":
        try:
            check_url(self.url)
        except BlockedDestination as exc:
            await self._client.aclose()
            raise McpError(str(exc)) from exc
        try:
            await self.initialize()
        except BaseException:
            await self._client.aclose()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self.session_id:
            try:
                await self._client.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            **self.headers,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._client.post(self.url, json=payload, headers=self._headers())
        except BlockedDestination as exc:
            raise McpError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise McpError(f"The MCP server is unreachable ({type(exc).__name__})") from exc
        if response.is_redirect:
            raise McpError("The MCP server answered with a redirect; register its final URL")
        if response.status_code in (401, 403):
            raise McpError("The MCP server rejected the credentials")
        if response.status_code >= 400:
            raise McpError(f"The MCP server returned HTTP {response.status_code}")
        return response

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        response = await self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        if session := response.headers.get("mcp-session-id"):
            self.session_id = session
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise McpError("The MCP server's response is too large")
        content_type = response.headers.get("content-type", "")
        messages = (
            _sse_messages(response.text)
            if content_type.startswith("text/event-stream")
            else [_json(response.text)]
        )
        for message in messages:
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    error = message["error"] or {}
                    raise McpError(f"MCP error: {str(error.get('message', 'unknown'))[:300]}")
                return message.get("result")
        raise McpError("The MCP server sent no response to the request")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "celius", "version": "0.5"},
            },
        )
        await self.notify("notifications/initialized")
        return result or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            for tool in (result or {}).get("tools") or []:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    tools.append(
                        {
                            "name": tool["name"][:200],
                            "description": str(tool.get("description") or "")[:2000],
                            "input_schema": tool.get("inputSchema")
                            or {"type": "object", "properties": {}},
                        }
                    )
            cursor = (result or {}).get("nextCursor")
            if not cursor:
                break
        return tools[:200]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = await self.request("tools/call", {"name": name, "arguments": arguments}) or {}
        texts = []
        for item in result.get("content") or []:
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "text":
                texts.append(str(item.get("text", "")))
            elif kind == "resource":
                resource = item.get("resource") or {}
                texts.append(str(resource.get("text") or f"[resource {resource.get('uri')}]"))
            elif kind:
                texts.append(f"[{kind} content]")
        if not texts and result.get("structuredContent") is not None:
            texts.append(json.dumps(result["structuredContent"])[:100_000])
        return "\n".join(texts), bool(result.get("isError"))


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError("The MCP server sent invalid JSON") from exc


def _sse_messages(body: str) -> list[Any]:
    messages = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if data:
            try:
                messages.append(json.loads(data))
            except json.JSONDecodeError:
                continue
    return messages


# ---------------------------------------------------------------------- tools


def tool_name(server: str, tool: str) -> str:
    """OpenAI-style function names allow [A-Za-z0-9_-]{1,64}."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    return f"{PREFIX}{server}__{safe}"[:64]


def server_headers(settings: Settings, server: McpServer) -> dict[str, str]:
    if not server.credentials:
        return {}
    headers = unseal(settings, server.credentials, "mcp")
    return {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}


def specs_for(server: McpServer, settings: Settings, headers: dict[str, str]) -> list[ToolSpec]:
    specs = []
    for tool in server.tools or []:
        original = tool["name"]

        async def handler(ctx: ToolContext, arguments: dict, _original=original) -> ToolOutput:
            try:
                async with McpClient(
                    server.url,
                    headers,
                    timeout=min(settings.mcp_timeout_seconds, ctx.time_limit),
                    allow_private=server.allow_private_network,
                    resolver=ctx.resolver,
                    transport=ctx.web_transport,
                ) as client:
                    text, is_error = await client.call_tool(_original, arguments)
            except McpError as exc:
                return ToolOutput(content=f"Error: {exc}", data={"error": str(exc)}, is_error=True)
            return ToolOutput(content=text, data={"server": server.name}, is_error=is_error)

        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        specs.append(
            ToolSpec(
                name=tool_name(server.name, original),
                label=f"{server.name}: {original}",
                description=(tool.get("description") or original)[:1000]
                + f" (MCP server {server.name})",
                risk="external",
                handler=handler,
                schema=schema,
            )
        )
    return specs

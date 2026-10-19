"""Adapter for OpenAI-compatible chat servers, with llama.cpp-specific probing.

llama.cpp's `llama-server` exposes `/v1/models`, `/v1/chat/completions` and
`/v1/embeddings`, plus non-OpenAI endpoints at the server root (`/health`,
`/props`, `/tokenize`) that give better health and capability information.
Those extras are used only when the provider is `llama_cpp`, and every one is
optional: a missing endpoint degrades to "unknown", never to an error.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import Settings
from app.services.model_gateway.base import (
    ChatMessage,
    ChatRequest,
    ModelCapabilities,
    ModelGatewayError,
    ModelHealth,
    ModelInfo,
    StreamChunk,
    ToolCallDelta,
)

log = logging.getLogger(__name__)

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Return the current time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _server_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


class OpenAICompatibleGateway:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str | None,
        connect_timeout: float,
        read_timeout: float,
        max_retries: int,
        configured: ModelCapabilities,
        default_context_tokens: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.root_url = _server_root(self.base_url)
        self.max_retries = max_retries
        self.configured = configured
        self.default_context_tokens = default_context_tokens
        self._verified_tools: dict[str, bool] = {}
        self._models_cache: tuple[float, list[ModelInfo]] | None = None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> "OpenAICompatibleGateway":
        return cls(
            provider=settings.model_provider,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key.get_secret_value() if settings.model_api_key else None,
            connect_timeout=settings.model_connect_timeout_seconds,
            read_timeout=settings.model_read_timeout_seconds,
            max_retries=settings.model_max_retries,
            configured=ModelCapabilities(
                streaming=True,
                tool_calling=settings.model_enable_tool_calling,
                vision=settings.model_enable_vision,
                embeddings=settings.model_enable_embeddings,
                source="configured",
            ),
            default_context_tokens=settings.model_default_context_tokens,
            transport=transport,
        )

    @property
    def _is_llama_cpp(self) -> bool:
        return self.provider == "llama_cpp"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ requests

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                error = ModelGatewayError("model_timeout", "Model server timed out", retryable=True)
                cause: Exception = exc
            except httpx.TransportError as exc:
                error = ModelGatewayError(
                    "model_unreachable", "Model server is unreachable", retryable=True
                )
                cause = exc
            else:
                if response.status_code < 400:
                    return response
                error = _http_error(response)
                cause = None  # type: ignore[assignment]
            if not error.retryable or attempt >= self.max_retries:
                raise error from cause
            attempt += 1
            log.warning("Retrying %s %s after %s (attempt %d)", method, url, error.code, attempt)

    async def _get_json(self, url: str) -> dict[str, Any] | None:
        """GET an optional endpoint; returns None when it doesn't exist or isn't JSON."""
        try:
            response = await self._client.get(url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    # ------------------------------------------------------------------ discovery

    async def list_models(self, *, max_age_seconds: float = 0) -> list[ModelInfo]:
        """List served models. Callers on hot paths pass `max_age_seconds` to reuse a
        recent result instead of querying the server again."""
        if self._models_cache and max_age_seconds > 0:
            fetched_at, cached = self._models_cache
            if time.monotonic() - fetched_at < max_age_seconds:
                return cached
        body = (await self._request("GET", f"{self.base_url}/models")).json()
        props = await self._get_json(f"{self.root_url}/props") if self._is_llama_cpp else None
        server_ctx = _props_context(props)
        server_vision = _props_vision(props)

        models = []
        for entry in body.get("data", []):
            model_id = entry.get("id")
            if not model_id:
                continue
            capabilities = self.configured.model_copy()
            if server_vision is not None:
                capabilities.vision = server_vision
                capabilities.source = "reported"
            if self._verified_tools.get(model_id) is not None:
                capabilities.tool_calling = self._verified_tools[model_id]
                capabilities.source = "verified"
            models.append(
                ModelInfo(
                    id=model_id,
                    provider=self.provider,
                    context_tokens=server_ctx or self.default_context_tokens,
                    capabilities=capabilities,
                    raw=entry,
                )
            )
        self._models_cache = (time.monotonic(), models)
        return models

    async def find_model(self, model_id: str, *, max_age_seconds: float = 60) -> ModelInfo | None:
        """The named model, or the only served model (llama-server ignores the name)."""
        models = await self.list_models(max_age_seconds=max_age_seconds)
        for model in models:
            if model.id == model_id:
                return model
        return models[0] if len(models) == 1 else None

    async def get_capabilities(self, model_id: str) -> ModelCapabilities:
        for model in await self.list_models():
            if model.id == model_id:
                return model.capabilities
        raise ModelGatewayError("model_not_found", f"Model {model_id!r} is not available")

    async def verify_tool_calling(self, model_id: str) -> bool:
        """Send a minimal tool-use prompt and record whether a tool call came back.

        llama.cpp only emits tool calls when started with a suitable chat template
        (e.g. `--jinja`), so configuration alone is not trusted.
        """
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "What time is it? Use the available tool."}],
            "tools": [_PROBE_TOOL],
            "max_tokens": 64,
            "temperature": 0,
        }
        try:
            response = await self._request(
                "POST", f"{self.base_url}/chat/completions", json=payload
            )
            choice = (response.json().get("choices") or [{}])[0]
            supported = bool((choice.get("message") or {}).get("tool_calls"))
        except ModelGatewayError as exc:
            if exc.retryable:
                raise
            supported = False
        self._verified_tools[model_id] = supported
        self._models_cache = None
        return supported

    async def health_check(self) -> ModelHealth:
        started = time.perf_counter()
        try:
            if self._is_llama_cpp:
                response = await self._client.get(f"{self.root_url}/health")
                if response.status_code != 404:
                    elapsed = (time.perf_counter() - started) * 1000
                    if response.status_code == 200:
                        return ModelHealth(ok=True, provider=self.provider, latency_ms=elapsed)
                    detail = (
                        "loading model"
                        if response.status_code == 503
                        else (f"health endpoint returned HTTP {response.status_code}")
                    )
                    return ModelHealth(
                        ok=False, provider=self.provider, latency_ms=elapsed, detail=detail
                    )
            response = await self._client.get(f"{self.base_url}/models")
            elapsed = (time.perf_counter() - started) * 1000
            if response.status_code == 200:
                return ModelHealth(ok=True, provider=self.provider, latency_ms=elapsed)
            return ModelHealth(
                ok=False,
                provider=self.provider,
                latency_ms=elapsed,
                detail=f"models endpoint returned HTTP {response.status_code}",
            )
        except httpx.TimeoutException:
            return ModelHealth(ok=False, provider=self.provider, detail="timed out")
        except httpx.HTTPError:
            return ModelHealth(ok=False, provider=self.provider, detail="unreachable")

    # ------------------------------------------------------------------ generation

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        if request.tools and not self.configured.tool_calling:
            raise ModelGatewayError("tools_unsupported", "Tool calling is disabled for this model")
        payload = _chat_payload(request)
        url = f"{self.base_url}/chat/completions"

        attempt = 0
        yielded = False
        while True:
            # Retries are only safe before anything has been yielded to the caller;
            # after that, a retry would duplicate the text already delivered.
            try:
                async with self._client.stream("POST", url, json=payload) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise _http_error(response)
                    async for chunk in _parse_sse(response):
                        yielded = True
                        yield chunk
                    return
            except ModelGatewayError as exc:
                if yielded or not exc.retryable or attempt >= self.max_retries:
                    raise
            except httpx.TimeoutException as exc:
                if yielded or attempt >= self.max_retries:
                    raise ModelGatewayError(
                        "model_timeout", "Model server timed out", retryable=True
                    ) from exc
            except httpx.TransportError as exc:
                if yielded or attempt >= self.max_retries:
                    raise ModelGatewayError(
                        "model_unreachable", "Model server connection lost", retryable=True
                    ) from exc
            attempt += 1

    async def count_tokens(self, text: str) -> int | None:
        if not self._is_llama_cpp:
            return None
        try:
            response = await self._client.post(f"{self.root_url}/tokenize", json={"content": text})
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        tokens = response.json().get("tokens")
        return len(tokens) if isinstance(tokens, list) else None

    async def create_embeddings(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        if not self.configured.embeddings:
            raise ModelGatewayError("embeddings_unsupported", "No embedding model is configured")
        payload: dict[str, Any] = {"input": texts}
        if model:
            payload["model"] = model
        body = (await self._request("POST", f"{self.base_url}/embeddings", json=payload)).json()
        data = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in data]


# ---------------------------------------------------------------------- helpers


def _chat_payload(request: ChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [_dump_message(m) for m in request.messages],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    for field in ("temperature", "top_p", "max_tokens", "stop"):
        value = getattr(request, field)
        if value is not None:
            payload[field] = value
    if request.tools:
        payload["tools"] = request.tools
    return payload


def _dump_message(message: ChatMessage) -> dict[str, Any]:
    return message.model_dump(exclude_none=True)


async def _parse_sse(response: httpx.Response) -> AsyncIterator[StreamChunk]:
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data:
            continue
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            log.warning("Skipping malformed stream line from model server")
            continue
        if "error" in event:
            message = (event["error"] or {}).get("message", "Model server error")
            raise ModelGatewayError("model_stream_error", str(message))
        chunk = _chunk_from_event(event)
        if chunk is not None:
            yield chunk


def _chunk_from_event(event: dict[str, Any]) -> StreamChunk | None:
    choices = event.get("choices") or []
    usage = event.get("usage")
    if not choices:
        return StreamChunk(usage=usage) if usage else None
    choice = choices[0]
    delta = choice.get("delta") or {}
    tool_calls = [
        ToolCallDelta(
            index=call.get("index", i),
            id=call.get("id"),
            name=(call.get("function") or {}).get("name"),
            arguments=(call.get("function") or {}).get("arguments"),
        )
        for i, call in enumerate(delta.get("tool_calls") or [])
    ]
    return StreamChunk(
        text=delta.get("content") or None,
        reasoning=delta.get("reasoning_content") or None,
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason"),
        usage=usage,
    )


def _http_error(response: httpx.Response) -> ModelGatewayError:
    status = response.status_code
    if status == 503:
        return ModelGatewayError("model_loading", "Model server is loading", retryable=True)
    if status in (401, 403):
        return ModelGatewayError("model_auth_failed", "Model server rejected the credentials")
    if status == 404:
        return ModelGatewayError("model_not_found", "Model or endpoint not found")
    if 400 <= status < 500:
        return ModelGatewayError("model_request_rejected", _error_message(response))
    return ModelGatewayError(
        "model_server_error", f"Model server returned HTTP {status}", retryable=status in (502, 504)
    )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"Model server returned HTTP {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:500]
    return f"Model server returned HTTP {response.status_code}"


def _props_context(props: dict[str, Any] | None) -> int | None:
    if not props:
        return None
    settings = props.get("default_generation_settings") or {}
    for value in (settings.get("n_ctx"), props.get("n_ctx")):
        if isinstance(value, int) and value > 0:
            return value
    return None


def _props_vision(props: dict[str, Any] | None) -> bool | None:
    if not props:
        return None
    modalities = props.get("modalities")
    if isinstance(modalities, dict) and isinstance(modalities.get("vision"), bool):
        return modalities["vision"]
    return None

import json

import httpx
import pytest

from app.config import Settings
from app.services.model_gateway import (
    ChatMessage,
    ChatRequest,
    ModelGatewayError,
    OpenAICompatibleGateway,
)


def make_gateway(handler, **overrides) -> OpenAICompatibleGateway:
    settings = Settings(
        app_env="test",
        model_base_url="http://model.test:8080/v1",
        model_max_retries=1,
        **overrides,
    )
    return OpenAICompatibleGateway.from_settings(settings, transport=httpx.MockTransport(handler))


def sse(*events) -> bytes:
    lines = [f"data: {json.dumps(e)}\n\n" for e in events]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def chat_request(**kwargs) -> ChatRequest:
    return ChatRequest(
        model="local-model", messages=[ChatMessage(role="user", content="hi")], **kwargs
    )


async def test_list_models_merges_llama_cpp_props():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen-14b-q4"}]})
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "default_generation_settings": {"n_ctx": 16384},
                    "modalities": {"vision": True},
                },
            )
        return httpx.Response(404)

    gateway = make_gateway(handler)
    [model] = await gateway.list_models()
    assert model.id == "qwen-14b-q4"
    assert model.context_tokens == 16384
    assert model.capabilities.vision is True
    assert model.capabilities.source == "reported"


async def test_list_models_without_props_uses_configuration():
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404)

    gateway = make_gateway(handler, model_default_context_tokens=8192)
    [model] = await gateway.list_models()
    assert model.context_tokens == 8192
    assert model.capabilities.vision is False
    assert model.capabilities.source == "configured"


async def test_stream_chat_parses_text_reasoning_tools_and_usage():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            content=sse(
                {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
                {"choices": [{"delta": {"content": "Hel"}}]},
                {"choices": [{"delta": {"content": "lo"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "t", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            ),
            headers={"content-type": "text/event-stream"},
        )

    gateway = make_gateway(handler, model_api_key="secret")
    chunks = [c async for c in gateway.stream_chat(chat_request(temperature=0.2))]

    assert captured["body"]["stream"] is True
    assert captured["body"]["temperature"] == 0.2
    assert "max_tokens" not in captured["body"]
    assert "".join(c.text or "" for c in chunks) == "Hello"
    assert chunks[0].reasoning == "thinking"
    assert chunks[3].tool_calls[0].name == "t"
    assert chunks[4].finish_reason == "stop"
    assert chunks[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2}


async def test_stream_chat_retries_connection_failure_once():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}}]}))

    gateway = make_gateway(handler)
    chunks = [c async for c in gateway.stream_chat(chat_request())]
    assert calls["n"] == 2
    assert chunks[0].text == "ok"


async def test_stream_chat_gives_up_after_max_retries():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    gateway = make_gateway(handler)
    with pytest.raises(ModelGatewayError) as exc:
        [c async for c in gateway.stream_chat(chat_request())]
    assert exc.value.code == "model_unreachable"


async def test_stream_chat_does_not_retry_client_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "context too long"}})

    gateway = make_gateway(handler)
    with pytest.raises(ModelGatewayError) as exc:
        [c async for c in gateway.stream_chat(chat_request())]
    assert exc.value.code == "model_request_rejected"
    assert str(exc.value) == "context too long"
    assert calls["n"] == 1


async def test_stream_error_event_raises():
    def handler(request):
        return httpx.Response(200, content=b'data: {"error": {"message": "OOM"}}\n\n')

    gateway = make_gateway(handler)
    with pytest.raises(ModelGatewayError, match="OOM"):
        [c async for c in gateway.stream_chat(chat_request())]


async def test_tools_rejected_when_disabled():
    gateway = make_gateway(lambda r: httpx.Response(500), model_enable_tool_calling=False)
    with pytest.raises(ModelGatewayError) as exc:
        [c async for c in gateway.stream_chat(chat_request(tools=[{"type": "function"}]))]
    assert exc.value.code == "tools_unsupported"


@pytest.mark.parametrize(
    ("status", "ok", "detail"),
    [
        (200, True, None),
        (503, False, "loading model"),
        (500, False, "health endpoint returned HTTP 500"),
    ],
)
async def test_health_check_uses_llama_cpp_health(status, ok, detail):
    gateway = make_gateway(lambda r: httpx.Response(status, json={}))
    health = await gateway.health_check()
    assert (health.ok, health.detail) == (ok, detail)


async def test_health_check_falls_back_to_models_endpoint():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": []})

    assert (await make_gateway(handler).health_check()).ok is True


async def test_verify_tool_calling_records_result():
    def handler(request):
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200, json={"choices": [{"message": {"tool_calls": [{"id": "x"}]}}]}
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404)

    gateway = make_gateway(handler, model_enable_tool_calling=False)
    assert await gateway.verify_tool_calling("m") is True
    caps = await gateway.get_capabilities("m")
    assert caps.tool_calling is True and caps.source == "verified"


async def test_count_tokens_and_embeddings():
    def handler(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.2]},
                        {"index": 0, "embedding": [0.1]},
                    ]
                },
            )
        return httpx.Response(404)

    assert await make_gateway(handler).count_tokens("abc") == 3
    with pytest.raises(ModelGatewayError):
        await make_gateway(handler).create_embeddings(["a"])
    vectors = await make_gateway(handler, model_enable_embeddings=True).create_embeddings(
        ["a", "b"]
    )
    assert vectors == [[0.1], [0.2]]

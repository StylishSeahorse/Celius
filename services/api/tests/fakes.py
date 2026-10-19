import asyncio
import json
import re
from typing import Any

import httpx

from app.worker.executor import claim_next_run, execute_run


class FakeLlama:
    """In-process stand-in for llama-server, used through httpx.MockTransport."""

    def __init__(self) -> None:
        self.reply = "Hello there, world"
        self.reasoning: str | None = None
        self.context_tokens: int | None = None
        self.chunk_delay = 0.0
        self.fail_status: int | None = None
        self.fail_after_chunks: int | None = None
        self.requests: list[dict[str, Any]] = []
        self.chunks_sent = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        if path == "/props":
            if self.context_tokens is None:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": self.context_tokens}}
            )
        if path == "/v1/chat/completions":
            self.requests.append(json.loads(request.content))
            if self.fail_status:
                return httpx.Response(
                    self.fail_status, json={"error": {"message": "context size exceeded"}}
                )
            return httpx.Response(
                200, content=self._stream(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404)

    async def _stream(self):
        if self.reasoning:
            yield _sse({"choices": [{"delta": {"reasoning_content": self.reasoning}}]})
        for piece in re.findall(r"\S+\s*", self.reply):
            if self.fail_after_chunks is not None and self.chunks_sent >= self.fail_after_chunks:
                yield _sse({"error": {"message": "slot crashed"}})
                return
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            self.chunks_sent += 1
            yield _sse({"choices": [{"delta": {"content": piece}}]})
        yield _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        yield _sse({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        yield b"data: [DONE]\n\n"


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


async def drain_runs(sessionmaker, gateway, settings, worker_id: str = "test-worker") -> int:
    """Execute queued runs synchronously, like one worker would."""
    count = 0
    while (run_id := await claim_next_run(sessionmaker, worker_id, settings)) is not None:
        await execute_run(
            run_id, factory=sessionmaker, gateway=gateway, settings=settings, worker_id=worker_id
        )
        count += 1
    return count


def parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("".join(data)))
    return events

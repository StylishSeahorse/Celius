import asyncio
import json
import re
from typing import Any

import httpx

import app.services.exports  # noqa: F401 - registers job handlers
import app.services.files.processing  # noqa: F401 - registers job handlers
from app.services.jobs import claim_next_job, run_job
from app.worker.executor import claim_next_run, execute_run


class FakeLlama:
    """In-process stand-in for llama-server, used through httpx.MockTransport."""

    def __init__(self) -> None:
        self.reply = "Hello there, world"
        self.reasoning: str | None = None
        self.context_tokens: int | None = None
        self.vision = False
        self.chat_template: str | None = None
        self.chunk_delay = 0.0
        self.fail_status: int | None = None
        self.fail_after_chunks: int | None = None
        self.requests: list[dict[str, Any]] = []
        self.chunks_sent = 0
        # Scripted turns: each is a reply text, or a list of tool calls
        # ({"name": ..., "arguments": {...} or "raw json"}). Used in order, then `reply`.
        self.turns: list[str | list[dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        if path == "/props":
            if self.context_tokens is None and not self.vision and self.chat_template is None:
                return httpx.Response(404)
            props: dict = {"modalities": {"vision": self.vision}}
            if self.context_tokens:
                props["default_generation_settings"] = {"n_ctx": self.context_tokens}
            if self.chat_template is not None:
                props["chat_template"] = self.chat_template
            return httpx.Response(200, json=props)
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
        if self.turns:
            turn = self.turns.pop(0)
            if isinstance(turn, list):
                for index, call in enumerate(turn):
                    arguments = call.get("arguments", {})
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments)
                    # Split the arguments to exercise delta assembly.
                    head, tail = arguments[: len(arguments) // 2], arguments[len(arguments) // 2 :]
                    yield _sse(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": index,
                                                "id": f"call_{index}_{len(self.requests)}",
                                                "type": "function",
                                                "function": {
                                                    "name": call["name"],
                                                    "arguments": head,
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    )
                    yield _sse(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": index,
                                                "function": {"arguments": tail},
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    )
                yield _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
                yield _sse({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
                yield b"data: [DONE]\n\n"
                return
            reply = turn
        else:
            reply = self.reply
        kwargs = (self.requests[-1].get("chat_template_kwargs") or {}) if self.requests else {}
        if self.reasoning and kwargs.get("enable_thinking") is not False:
            yield _sse({"choices": [{"delta": {"reasoning_content": self.reasoning}}]})
        for piece in re.findall(r"\S+\s*", reply):
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


async def drain_jobs(sessionmaker, settings, worker_id: str = "test-worker") -> int:
    """Run queued background jobs (file processing, exports) synchronously."""
    count = 0
    while (job_id := await claim_next_job(sessionmaker, worker_id, settings)) is not None:
        await run_job(job_id, factory=sessionmaker, settings=settings, worker_id=worker_id)
        count += 1
    return count


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


async def drain_runs(
    sessionmaker, gateway, settings, worker_id: str = "test-worker", **tools
) -> int:
    """Execute queued runs synchronously, like one worker would. `tools` passes `sandbox`
    and `tool_overrides` through to the executor."""
    count = 0
    while (run_id := await claim_next_run(sessionmaker, worker_id, settings)) is not None:
        await execute_run(
            run_id,
            factory=sessionmaker,
            gateway=gateway,
            settings=settings,
            worker_id=worker_id,
            **tools,
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

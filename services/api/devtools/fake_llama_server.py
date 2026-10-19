"""A tiny stand-in for llama-server, for development and end-to-end tests without a GPU.

    uv run python -m devtools.fake_llama_server --port 8080 --delay 0.03

Implements /health, /props, /v1/models and streaming /v1/chat/completions. The reply
echoes the last user message so tests can assert on it. A message containing
"[fail]" returns HTTP 400; "[slow]" streams very slowly (for cancellation tests).
"""

import argparse
import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
DELAY = 0.03
MODEL_ID = "fake-model"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/props")
async def props():
    return {"default_generation_settings": {"n_ctx": 8192}, "modalities": {"vision": False}}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}


def _reply_for(messages: list[dict]) -> str:
    last = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    content = (last.get("content") or "").split("\n\n")[-1]
    turns = sum(1 for m in messages if m.get("role") == "user")
    return (
        f"You said: **{content.strip()[:200]}**\n\n"
        f"This is turn {turns}. Here is a list:\n\n- one\n- two\n- three\n\n"
        "```python\nprint('hello from the fake model')\n```"
    )


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    # Only the newest paragraph: the app merges a failed turn into the next user message.
    last = (messages[-1].get("content", "") if messages else "").split("\n\n")[-1]
    if "[fail]" in last:
        return JSONResponse({"error": {"message": "fake failure requested"}}, status_code=400)
    delay = 0.5 if "[slow]" in last else DELAY
    words = _reply_for(messages).split(" ")

    async def stream():
        created = int(time.time())
        for i, word in enumerate(words):
            await asyncio.sleep(delay)
            piece = word if i == len(words) - 1 else word + " "
            chunk = {
                "id": "fake",
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {"content": piece}}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(done)}\n\n"
        usage = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": len(words)}}
        yield f"data: {json.dumps(usage)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--delay", type=float, default=0.03)
    args = parser.parse_args()
    DELAY = args.delay
    uvicorn.run(app, host=args.host, port=args.port)

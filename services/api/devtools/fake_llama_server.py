"""A tiny stand-in for llama-server, for development and end-to-end tests without a GPU.

    uv run python -m devtools.fake_llama_server --port 8080 --delay 0.03

Implements /health, /props, /v1/models and streaming /v1/chat/completions. The reply
echoes the last user message so tests can assert on it, names any attachments and
images it received, and "thinks" (reasoning_content) unless the request turns thinking
off with chat_template_kwargs.enable_thinking=false. "[link]" adds a markdown link to the
reply. A message containing "[fail]" returns
HTTP 400; "[slow]" streams very slowly (for cancellation tests).

Tool calling (when the request offers tools): "[tool:write]" calls write_file,
"[tool:python]" calls run_python, "[tool:list]" calls list_files. After a tool result the
reply quotes it ("Tool result: …").

Memory extraction requests (recognised by their prompt) propose whatever follows
"[remember:" in the user's message, e.g. "[remember:The user likes green tea]".
"""

import argparse
import asyncio
import json
import re
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
    return {
        "default_generation_settings": {"n_ctx": 8192},
        "modalities": {"vision": True},
        "chat_template": "{%- if enable_thinking is defined and not enable_thinking %}{%- endif %}",
    }


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}


def _text_of(content) -> tuple[str, int]:
    """(text, number of images) for string or multi-part content."""
    if isinstance(content, list):
        text = "\n\n".join(p.get("text", "") for p in content if p.get("type") == "text")
        return text, sum(1 for p in content if p.get("type") == "image_url")
    return content or "", 0


def _reply_for(messages: list[dict]) -> str:
    last = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    full, images = _text_of(last.get("content"))
    content = full.split("\n\n")[-1]
    turns = sum(1 for m in messages if m.get("role") == "user")
    seen = re.findall(r'<attachment name="([^"]+)"', full)
    system = next((m for m in messages if m.get("role") == "system"), {})
    project_files = re.findall(r'<project_file name="([^"]+)"', _text_of(system.get("content"))[0])
    extra = ""
    if seen:
        extra += f"I can see these attachments: {', '.join(seen)}.\n\n"
    if "[link]" in content:
        extra += "See [the Celius docs](https://example.com/celius-docs).\n\n"
    if project_files:
        extra += f"Project files I know: {', '.join(project_files)}.\n\n"
    if images:
        extra += f"I can see {images} image(s).\n\n"
    return (
        extra + f"You said: **{content.strip()[:200]}**\n\n"
        f"This is turn {turns}. Here is a list:\n\n- one\n- two\n- three\n\n"
        "```python\nprint('hello from the fake model')\n```"
    )


TOOL_TRIGGERS = {
    "[tool:write]": ("write_file", {"path": "hello.txt", "content": "Hello from Celius"}),
    "[tool:python]": ("run_python", {"code": "print(6 * 7)"}),
    "[tool:list]": ("list_files", {}),
}


def _tool_stream(name: str, arguments: dict):
    async def stream():
        call = {
            "index": 0,
            "id": "call_fake",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        chunk = {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}
        yield f"data: {json.dumps(chunk)}\n\n"
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def _text_stream(text: str):
    async def stream():
        for word in text.split(" "):
            await asyncio.sleep(DELAY)
            chunk = {"choices": [{"index": 0, "delta": {"content": word + " "}}]}
            yield f"data: {json.dumps(chunk)}\n\n"
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    offered = {t["function"]["name"] for t in body.get("tools") or []}
    system = messages[0].get("content") if messages else ""
    if isinstance(system, str) and system.startswith("You decide what an assistant"):
        facts = re.findall(r"\[remember:([^\]]+)\]", _text_of(messages[-1].get("content"))[0])
        return _text_stream(json.dumps({"memories": [f.strip() for f in facts]}))
    if messages and messages[-1].get("role") == "tool":
        result = str(messages[-1].get("content") or "")[:200]
        return _text_stream(f"Tool result: {result}")
    if messages and messages[-1].get("role") == "user":
        last_user = _text_of(messages[-1].get("content"))[0]
        for trigger, (name, arguments) in TOOL_TRIGGERS.items():
            if trigger in last_user and name in offered:
                return _tool_stream(name, arguments)
    # Only the newest paragraph: the app merges a failed turn into the next user message.
    last = _text_of(messages[-1].get("content") if messages else "")[0].split("\n\n")[-1]
    thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking") is not False
    if "[fail]" in last:
        return JSONResponse({"error": {"message": "fake failure requested"}}, status_code=400)
    delay = 0.5 if "[slow]" in last else DELAY
    words = _reply_for(messages).split(" ")

    async def stream():
        created = int(time.time())
        if thinking:
            think = {
                "choices": [{"index": 0, "delta": {"reasoning_content": "Thinking it through…"}}]
            }
            yield f"data: {json.dumps(think)}\n\n"
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

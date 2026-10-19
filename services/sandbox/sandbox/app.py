"""HTTP front of the sandbox. One endpoint does work; it requires the shared token.

POST /v1/executions   run code in a workspace (blocks until done; closing the
                      connection kills the code)
GET  /v1/health       mode and limits (no token needed; reveals nothing sensitive)
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from sandbox.config import SandboxConfig
from sandbox.runner import Execution, InvalidRequest, become_subreaper, bwrap_available, run

log = logging.getLogger("sandbox")


class ExecutionRequest(BaseModel):
    workspace: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(python|shell)$")
    code: str = Field(max_length=1_000_000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    memory_mb: int = Field(default=1024, ge=64, le=65536)
    output_limit: int = Field(default=64 * 1024, ge=1024, le=64 * 1024 * 1024)


def create_app(config: SandboxConfig | None = None) -> FastAPI:
    config = config or SandboxConfig()
    if len(config.token) < 16:
        raise RuntimeError("SANDBOX_TOKEN must be set (at least 16 characters)")
    state: dict = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        config.scratch_root.mkdir(parents=True, exist_ok=True)
        if config.mode == "auto":
            state["mode"] = "bwrap" if bwrap_available(config.scratch_root) else "process"
        else:
            state["mode"] = config.mode
        state["slots"] = asyncio.Semaphore(config.max_concurrent)
        if not become_subreaper():
            log.warning("could not become a child subreaper: daemonised strays may survive")
        if state["mode"] == "process":
            log.warning(
                "bubblewrap unavailable: per-execution namespaces disabled, "
                "relying on the container boundary and process limits"
            )
        log.info("sandbox ready (mode=%s)", state["mode"])
        yield

    app = FastAPI(title="Celius sandbox", lifespan=lifespan, docs_url=None, openapi_url=None)

    def authorise(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, config.token):
            raise HTTPException(401, "Invalid sandbox token")

    @app.get("/v1/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "mode": state.get("mode"),
            "max_timeout_seconds": config.max_timeout_seconds,
            "max_memory_mb": config.max_memory_mb,
        }

    @app.post("/v1/executions", dependencies=[Depends(authorise)])
    async def execute(body: ExecutionRequest, request: Request) -> dict:
        async with state["slots"]:
            task = asyncio.create_task(run(Execution(**body.model_dump()), config, state["mode"]))

            async def watch_disconnect() -> None:
                while not task.done():
                    if await request.is_disconnected():
                        task.cancel()
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(watch_disconnect())
            try:
                result = await task
            except InvalidRequest as exc:
                raise HTTPException(422, str(exc)) from exc
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # this handler itself is being cancelled (shutdown): propagate
                raise HTTPException(499, "Cancelled") from None
            finally:
                watcher.cancel()
        return asdict(result)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8100, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()

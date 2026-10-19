import asyncio
import logging
import os
import socket
import time
import uuid

from app.config import Settings
from app.db import SessionFactory
from app.services.model_gateway import ModelGateway
from app.worker.executor import claim_next_run, execute_run, recover_stale_runs

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 20


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


async def run_worker(
    *,
    factory: SessionFactory,
    gateway: ModelGateway,
    settings: Settings,
    stop: asyncio.Event,
    worker_id: str | None = None,
) -> None:
    """Claim and execute runs until `stop` is set.

    Concurrency is capped at MODEL_PARALLEL_REQUESTS, which should match the number of
    llama-server slots (`--parallel`); extra requests would only queue on the GPU.
    """
    worker_id = worker_id or make_worker_id()
    tasks: set[asyncio.Task] = set()
    recovery_interval = settings.run_lease_seconds / 2
    last_recovery = 0.0
    log.info("Worker %s started (parallel=%d)", worker_id, settings.model_parallel_requests)

    while not stop.is_set():
        try:
            if time.monotonic() - last_recovery >= recovery_interval:
                if recovered := await recover_stale_runs(factory, settings):
                    log.info("Recovered %d stale run(s)", recovered)
                last_recovery = time.monotonic()
            while len(tasks) < settings.model_parallel_requests:
                run_id = await claim_next_run(factory, worker_id, settings)
                if run_id is None:
                    break
                log.info("Executing run %s", run_id)
                task = asyncio.create_task(
                    execute_run(
                        run_id,
                        factory=factory,
                        gateway=gateway,
                        settings=settings,
                        worker_id=worker_id,
                    )
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception:
            # Database unavailable, etc. Keep the process alive and try again.
            log.exception("Worker loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_seconds)
        except TimeoutError:
            pass

    if tasks:
        log.info("Waiting up to %ds for %d run(s)", SHUTDOWN_GRACE_SECONDS, len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()  # their leases expire and another worker requeues them
        await asyncio.gather(*pending, return_exceptions=True)
    log.info("Worker %s stopped", worker_id)

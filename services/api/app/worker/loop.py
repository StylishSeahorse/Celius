import asyncio
import logging
import os
import socket
import time
import uuid

import app.services.exports  # noqa: F401 - registers job handlers
import app.services.files.processing  # noqa: F401 - registers job handlers
from app.config import Settings
from app.db import SessionFactory
from app.services.files.service import cleanup_expired_uploads
from app.services.jobs import claim_next_job, recover_stale_jobs, run_job
from app.services.model_gateway import ModelGateway
from app.worker.executor import claim_next_run, execute_run, recover_stale_runs

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 20
MAINTENANCE_INTERVAL_SECONDS = 60


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
    """Claim and execute generation runs and background jobs until `stop` is set.

    Run concurrency is capped at MODEL_PARALLEL_REQUESTS, which should match the number of
    llama-server slots (`--parallel`); extra requests would only queue on the GPU. Jobs
    (file processing, exports) use the CPU and have their own limit, JOB_PARALLEL, so a
    long generation never delays an upload becoming usable.
    """
    worker_id = worker_id or make_worker_id()
    runs: set[asyncio.Task] = set()
    jobs: set[asyncio.Task] = set()
    recovery_interval = settings.run_lease_seconds / 2
    last_recovery = 0.0
    last_maintenance = 0.0
    log.info(
        "Worker %s started (runs=%d, jobs=%d)",
        worker_id,
        settings.model_parallel_requests,
        settings.job_parallel,
    )

    def track(tasks: set[asyncio.Task], coroutine) -> None:
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    while not stop.is_set():
        try:
            now = time.monotonic()
            if now - last_recovery >= recovery_interval:
                if recovered := await recover_stale_runs(factory, settings):
                    log.info("Recovered %d stale run(s)", recovered)
                if recovered := await recover_stale_jobs(factory, settings):
                    log.info("Recovered %d stale job(s)", recovered)
                last_recovery = now
            if now - last_maintenance >= MAINTENANCE_INTERVAL_SECONDS:
                if expired := await cleanup_expired_uploads(factory, settings):
                    log.info("Expired %d abandoned upload(s)", expired)
                last_maintenance = now
            while len(runs) < settings.model_parallel_requests:
                run_id = await claim_next_run(factory, worker_id, settings)
                if run_id is None:
                    break
                log.info("Executing run %s", run_id)
                track(
                    runs,
                    execute_run(
                        run_id,
                        factory=factory,
                        gateway=gateway,
                        settings=settings,
                        worker_id=worker_id,
                    ),
                )
            while len(jobs) < settings.job_parallel:
                job_id = await claim_next_job(factory, worker_id, settings)
                if job_id is None:
                    break
                track(
                    jobs, run_job(job_id, factory=factory, settings=settings, worker_id=worker_id)
                )
        except Exception:
            # Database unavailable, etc. Keep the process alive and try again.
            log.exception("Worker loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_seconds)
        except TimeoutError:
            pass

    pending_all = runs | jobs
    if pending_all:
        log.info("Waiting up to %ds for %d task(s)", SHUTDOWN_GRACE_SECONDS, len(pending_all))
        _, pending = await asyncio.wait(pending_all, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()  # their leases expire and another worker requeues them
        await asyncio.gather(*pending, return_exceptions=True)
    log.info("Worker %s stopped", worker_id)

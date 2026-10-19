"""Run the generation worker: `python -m app.worker`."""

import asyncio
import logging
import signal

from app.config import get_settings
from app.db import get_sessionmaker
from app.services.model_gateway import get_model_gateway
from app.worker.loop import run_worker


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    gateway = get_model_gateway()
    try:
        await run_worker(
            factory=get_sessionmaker(), gateway=gateway, settings=get_settings(), stop=stop
        )
    finally:
        await gateway.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())

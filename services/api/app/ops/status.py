"""The status page's health checks, from the command line (inside the api container):

    docker compose -f infra/compose.yaml --env-file .env exec api python -m app.ops.status

Exits 0 when everything is ok, 1 on a warning, 2 on a critical problem, so it can drive
an external monitor too.
"""

import asyncio

from app.config import get_settings
from app.db import get_sessionmaker
from app.services import monitoring
from app.services.model_gateway import OpenAICompatibleGateway

MARK = {"ok": "ok  ", "warning": "WARN", "critical": "CRIT"}


async def _collect() -> list[monitoring.Check]:
    settings = get_settings()
    gateway = OpenAICompatibleGateway.from_settings(settings)
    try:
        async with get_sessionmaker()() as db:
            return await monitoring.collect(db, settings, gateway)
    finally:
        await gateway.aclose()


def main() -> None:
    checks = asyncio.run(_collect())
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"{MARK[check.level]}  {check.name.ljust(width)}  {check.summary}")
    raise SystemExit(monitoring.ORDER[monitoring.overall(checks)])


if __name__ == "__main__":
    main()

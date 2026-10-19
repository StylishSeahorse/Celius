from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.model_gateway import ModelGateway, get_model_gateway

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    """Process is up. Used by the container healthcheck."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> dict:
    """Dependencies reachable. The model server is reported but does not gate readiness,
    since the app must stay usable (history, files) while inference is offline."""
    checks: dict[str, dict] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - report any DB failure as not ready
        checks["database"] = {"ok": False, "detail": type(exc).__name__}
    checks["model_server"] = (await gateway.health_check()).model_dump()

    ready_ = checks["database"]["ok"]
    if not ready_:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready_ else "not_ready", "checks": checks}

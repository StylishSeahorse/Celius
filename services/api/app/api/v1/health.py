import hmac
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.services import monitoring
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


@router.get("/status")
async def deployment_status(
    auth: CurrentAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """Every health check, for the status page (administrators)."""
    if not auth.user.is_admin:
        raise AppError(403, "forbidden", "Only an administrator can see the system status")
    checks = await monitoring.collect(db, settings, gateway)
    return {
        "status": monitoring.overall(checks),
        "checks": [asdict(c) for c in checks],
        "alerts": {
            "webhook": bool(settings.alert_webhook_url),
            "format": settings.alert_webhook_format,
        },
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Prometheus text format. Needs METRICS_TOKEN as a bearer token."""
    expected = settings.metrics_token.get_secret_value() if settings.metrics_token else ""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
        raise AppError(404, "not_found", "Not found")
    return monitoring.prometheus(await monitoring.collect(db, settings, gateway))

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.schemas.models import ModelListResponse
from app.services.limits import model_allowed
from app.services.model_gateway import (
    ModelCapabilities,
    ModelGateway,
    ModelGatewayError,
    OpenAICompatibleGateway,
    get_model_gateway,
)

router = APIRouter(prefix="/models", tags=["models"])

Gateway = Annotated[ModelGateway, Depends(get_model_gateway)]


@router.get("", response_model=ModelListResponse)
async def list_models(
    auth: CurrentAuth, gateway: Gateway, settings: Annotated[Settings, Depends(get_settings)]
) -> ModelListResponse:
    health = await gateway.health_check()
    models = []
    if health.ok:
        try:
            models = await gateway.list_models()
        except ModelGatewayError as exc:
            health.ok = False
            health.detail = exc.code
    # Only the models an administrator allows this user (A-03).
    models = [m for m in models if model_allowed(auth.user, m.id)]
    default = settings.model_default_id
    if not model_allowed(auth.user, default) and auth.user.allowed_models:
        default = auth.user.allowed_models[0]
    return ModelListResponse(default_model_id=default, models=models, health=health)


@router.post("/{model_id}/verify-tools", response_model=ModelCapabilities)
async def verify_tools(model_id: str, auth: CurrentAuth, gateway: Gateway) -> ModelCapabilities:
    """Run a live tool-calling probe against the model (admin only; uses inference time)."""
    if not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator access required")
    if not isinstance(gateway, OpenAICompatibleGateway):
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Probe not supported by provider")
    try:
        await gateway.verify_tool_calling(model_id)
        return await gateway.get_capabilities(model_id)
    except ModelGatewayError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.code) from exc

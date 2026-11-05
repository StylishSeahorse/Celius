from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.models import ModelCatalogEntry
from app.schemas.models import (
    ModelListResponse,
    ModelProfileOut,
    ModelRegistryEntry,
    ModelRegistryUpdate,
)
from app.services import model_registry
from app.services.audit import audit
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
DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _require_admin(auth) -> None:
    if not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator access required")


@router.get("", response_model=ModelListResponse)
async def list_models(
    auth: CurrentAuth, gateway: Gateway, db: DB, settings: AppSettings
) -> ModelListResponse:
    health = await gateway.health_check()
    models = []
    if health.ok:
        try:
            models = await gateway.list_models()
        except ModelGatewayError as exc:
            health.ok = False
            health.detail = exc.code
    # Only the models an administrator allows this user (A-03). Copies: the gateway caches
    # its list, and the registry's view is applied below.
    models = [m.model_copy(deep=True) for m in models if model_allowed(auth.user, m.id)]
    default = settings.model_default_id
    if not model_allowed(auth.user, default) and auth.user.allowed_models:
        default = auth.user.allowed_models[0]
    profiles = {}
    for model in models:
        profile = await model_registry.resolve(db, gateway, model.id, settings, info=model)
        profiles[model.id] = ModelProfileOut(**profile.as_dict())
        # Capabilities as the registry sees them (an administrator's word, a verification).
        model.capabilities.tool_calling = profile.tool_calling
        model.capabilities.vision = profile.vision
        model.capabilities.reasoning_toggle = profile.reasoning_toggle
        model.context_tokens = profile.context_tokens
    await db.commit()
    return ModelListResponse(
        default_model_id=default, models=models, health=health, profiles=profiles
    )


@router.post("/{model_id}/verify-tools", response_model=ModelCapabilities)
async def verify_tools(
    model_id: str, auth: CurrentAuth, gateway: Gateway, db: DB
) -> ModelCapabilities:
    """Run a live tool-calling probe against the model (admin only; uses inference time).
    The result is stored in the model registry, so the worker uses it too."""
    _require_admin(auth)
    if not isinstance(gateway, OpenAICompatibleGateway):
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Probe not supported by provider")
    try:
        supported = await gateway.verify_tool_calling(model_id)
        capabilities = await gateway.get_capabilities(model_id)
    except ModelGatewayError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.code) from exc
    await model_registry.record_verified_tools(db, gateway.provider, model_id, supported)
    audit(db, auth.user.id, "model.tools_verified", "model", None, model=model_id, ok=supported)
    await db.commit()
    return capabilities


async def _entry_out(
    db: AsyncSession, gateway, entry: ModelCatalogEntry, settings: Settings
) -> ModelRegistryEntry:
    info = None
    try:
        info = await gateway.find_model(entry.model_id)
    except ModelGatewayError:
        info = None
    if info is not None and info.id != entry.model_id:
        info = None  # a different model answered (single-model servers)
    effective = model_registry.build_profile(entry.model_id, entry.provider, info, entry, settings)
    return ModelRegistryEntry(
        model_id=entry.model_id,
        provider=entry.provider,
        available=entry.is_available,
        detected=entry.capabilities or {},
        profile_setting=entry.profile,
        overrides=entry.overrides or {},
        limits=entry.limits or {},
        notes=entry.notes,
        effective=ModelProfileOut(**effective.as_dict()),
    )


@router.get("/registry", response_model=list[ModelRegistryEntry])
async def model_registry_list(
    auth: CurrentAuth, gateway: Gateway, db: DB, settings: AppSettings
) -> list[ModelRegistryEntry]:
    """Every model Celius has seen, with what was detected and what an administrator set."""
    _require_admin(auth)
    try:
        for info in await gateway.list_models():
            await model_registry.resolve(db, gateway, info.id, settings, info=info)
        await db.commit()
    except ModelGatewayError:
        pass  # the server is down: show what is stored
    return [
        await _entry_out(db, gateway, entry, settings)
        for entry in await model_registry.all_entries(db)
    ]


@router.put("/registry", response_model=ModelRegistryEntry)
async def model_registry_update(
    body: ModelRegistryUpdate, auth: CurrentAuth, gateway: Gateway, db: DB, settings: AppSettings
) -> ModelRegistryEntry:
    """Set a model's profile, capability overrides and limits (administrators)."""
    _require_admin(auth)
    provider = getattr(gateway, "provider", "unknown")
    entry = await model_registry.catalog_entry(db, provider, body.model_id)
    if entry is None:
        entry = ModelCatalogEntry(
            provider=provider,
            model_id=body.model_id,
            capabilities={},
            overrides={},
            limits={},
            is_available=False,
        )
        db.add(entry)
    entry.profile = body.profile
    entry.overrides = body.overrides.model_dump(exclude_none=True)
    entry.limits = body.limits.model_dump(exclude_none=True)
    entry.notes = body.notes
    audit(
        db,
        auth.user.id,
        "model.registry_updated",
        "model",
        None,
        model=body.model_id,
        profile=body.profile,
        overrides=entry.overrides,
        limits=entry.limits,
    )
    await db.commit()
    return await _entry_out(db, gateway, entry, settings)

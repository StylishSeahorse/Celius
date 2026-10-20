import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import router as v1_router
from app.auth.bootstrap import ensure_bootstrap_user
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.errors import install_error_handlers
from app.security import SecurityMiddleware
from app.services.model_gateway import get_model_gateway
from app.static import mount_web_app

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with get_sessionmaker()() as db:
        await ensure_bootstrap_user(db, settings)
    yield
    await get_model_gateway().aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Celius API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.app_env == "production" else "/api/docs",
        openapi_url=None if settings.app_env == "production" else "/api/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.include_router(v1_router)
    mount_web_app(app, settings.static_root)  # last: it catches every other path
    return app


app = create_app()

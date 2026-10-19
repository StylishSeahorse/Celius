import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.auth.passwords import hash_password
from app.config import Settings, get_settings
from app.db import get_db
from app.db.base import Base
from app.main import create_app
from app.models import User
from app.services.model_gateway import OpenAICompatibleGateway, get_model_gateway

PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        model_base_url="http://model.test:8080/v1",
        model_max_retries=1,
    )


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def user(sessionmaker) -> User:
    async with sessionmaker() as db:
        u = User(username="owner", password_hash=hash_password(PASSWORD), is_admin=True)
        db.add(u)
        await db.commit()
        return u


@pytest.fixture
def model_handler():
    """Override per test: a callable(httpx.Request) -> httpx.Response for the fake model server."""
    return lambda request: httpx.Response(200, json={"status": "ok", "data": []})


@pytest.fixture
def gateway(settings, model_handler) -> OpenAICompatibleGateway:
    return OpenAICompatibleGateway.from_settings(
        settings, transport=httpx.MockTransport(model_handler)
    )


@pytest.fixture
async def client(settings, sessionmaker, gateway) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _db():
        async with sessionmaker() as db:
            yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_model_gateway] = lambda: gateway
    # No lifespan: the bootstrap step is tested directly.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    await gateway.aclose()


@pytest.fixture
async def auth_headers(client, user) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}

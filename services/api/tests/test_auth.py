from sqlalchemy import func, select

from app.auth.bootstrap import ensure_bootstrap_user
from app.config import Settings
from app.models import AuditEvent, User
from tests.conftest import PASSWORD


async def test_login_sets_cookie_and_me_works(client, user):
    response = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "owner"


async def test_bearer_token_auth(client, auth_headers):
    me = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert me.status_code == 200


async def test_wrong_password_rejected_and_audited(client, user, sessionmaker):
    response = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": "wrong"}
    )
    assert response.status_code == 401
    unknown = await client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "wrong"}
    )
    assert unknown.status_code == 401
    assert unknown.json() == response.json()  # no username enumeration

    async with sessionmaker() as db:
        failures = await db.scalar(
            select(func.count()).where(AuditEvent.action == "auth.login_failed")
        )
    assert failures == 2


async def test_unauthenticated_requests_rejected(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.get("/api/v1/models")).status_code == 401
    bad = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


async def test_logout_revokes_session(client, auth_headers):
    assert (await client.post("/api/v1/auth/logout", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 401


async def test_inactive_user_cannot_use_existing_session(client, auth_headers, sessionmaker):
    async with sessionmaker() as db:
        u = await db.scalar(select(User))
        u.is_active = False
        await db.commit()
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 401


async def test_bootstrap_user_created_once(sessionmaker):
    settings = Settings(
        app_env="test", bootstrap_username="admin", bootstrap_password="a-long-password-123"
    )
    async with sessionmaker() as db:
        assert await ensure_bootstrap_user(db, settings) is not None
    async with sessionmaker() as db:
        assert await ensure_bootstrap_user(db, settings) is None
        assert await db.scalar(select(func.count()).select_from(User)) == 1


def test_production_requires_strong_secret():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(app_env="production")
    Settings(app_env="production", app_secret_key="x" * 40)

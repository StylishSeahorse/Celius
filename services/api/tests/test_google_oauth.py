"""Gmail through "Sign in with Google" (ADR C-14): consent, callback, tokens, XOAUTH2."""

import base64
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

import app.services.integrations.email as email_module
import app.services.integrations.google as google
from app.auth.passwords import hash_password
from app.models import IntegrationAccount, User
from app.services.integrations import accounts
from tests.integration_fakes import FakeMailServer, message

CLIENT_ID = "celius-test.apps.googleusercontent.com"
ADDRESS = "robin@gmail.com"


def id_token(**claims) -> str:
    body = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "email": ADDRESS,
        "email_verified": True,
        **claims,
    }

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(body)}.signature"


class FakeGoogle:
    """Google's token and revocation endpoints."""

    def __init__(self):
        self.verifiers: dict[str, str] = {}  # code -> PKCE challenge it was issued for
        self.requests: list[dict] = []
        self.revoked: list[str] = []
        self.claims: dict = {}
        self.scope = "openid email https://mail.google.com/"
        self.refresh_token: str | None = "refresh-1"
        self.refresh_fails = False
        self.issued = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append({"url": str(request.url), **form})
        if str(request.url) == google.REVOKE_URL:
            self.revoked.append(form["token"])
            return httpx.Response(200)
        assert form["client_id"] == CLIENT_ID and form["client_secret"] == "shh"
        if form["grant_type"] == "authorization_code":
            challenge = self.verifiers.get(form["code"])
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if challenge != base64.urlsafe_b64encode(digest).rstrip(b"=").decode():
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.issued += 1
            answer = {
                "access_token": f"access-{self.issued}",
                "expires_in": 3599,
                "scope": self.scope,
                "id_token": id_token(**self.claims),
            }
            if self.refresh_token:
                answer["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=answer)
        if form["grant_type"] == "refresh_token":
            if self.refresh_fails:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.issued += 1
            return httpx.Response(
                200, json={"access_token": f"access-{self.issued}", "expires_in": 3599}
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


@pytest.fixture
def googly(settings, monkeypatch) -> FakeGoogle:
    settings.google_oauth_client_id = CLIENT_ID
    settings.google_oauth_client_secret = SecretStr("shh")
    fake = FakeGoogle()
    monkeypatch.setattr(google, "TRANSPORT", httpx.MockTransport(fake.handler))
    google._ACCESS.clear()
    return fake


@pytest.fixture
def mail(monkeypatch) -> FakeMailServer:
    server = FakeMailServer()
    monkeypatch.setattr(email_module, "IMAP_FACTORY", server.imap)
    monkeypatch.setattr(email_module, "SMTP_FACTORY", server.smtp)
    return server


async def consent(client, headers, googly, code="code-1") -> dict:
    """Start, and pretend the user consented: the query Google would send back."""
    started = await client.post("/api/v1/integrations/google/start", json={}, headers=headers)
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["url"]).query)
    googly.verifiers[code] = query["code_challenge"][0]
    return {"code": code, "state": query["state"][0]}


async def callback(client, params) -> dict:
    response = await client.get(
        "/api/v1/integrations/google/callback", params=params, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/#/today?")
    return {k: v[0] for k, v in parse_qs(location.split("?", 1)[1]).items()}


def xoauth(token: str) -> str:
    return google.xoauth2(ADDRESS, token)


async def test_not_configured(client, auth_headers):
    status = (await client.get("/api/v1/integrations/google", headers=auth_headers)).json()
    assert status["configured"] is False
    assert status["redirect_uri"].endswith("/api/v1/integrations/google/callback")
    started = await client.post("/api/v1/integrations/google/start", json={}, headers=auth_headers)
    assert started.status_code == 409


async def test_consent_url_asks_for_offline_gmail_with_pkce(client, auth_headers, googly):
    started = await client.post(
        "/api/v1/integrations/google/start", json={"login_hint": ADDRESS}, headers=auth_headers
    )
    url = urlsplit(started.json()["url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == google.AUTH_URL
    assert query["client_id"] == CLIENT_ID
    assert query["scope"].split() == ["openid", "email", "https://mail.google.com/"]
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["login_hint"] == ADDRESS
    assert query["redirect_uri"] == "http://localhost:8000/api/v1/integrations/google/callback"
    cookie = started.headers["set-cookie"]
    assert google.NONCE_COOKIE in cookie and "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower() and google.CALLBACK_PATH in cookie
    assert "shh" not in started.text  # the client secret never leaves the server


async def test_connect_read_draft_and_send_with_xoauth2(
    client, auth_headers, googly, mail, sessionmaker, settings
):
    back = await callback(client, await consent(client, auth_headers, googly))
    assert back == {"google": "connected", "message": ADDRESS}

    [acct] = (await client.get("/api/v1/integrations/accounts", headers=auth_headers)).json()
    assert acct["provider"] == "gmail" and acct["kind"] == "email"
    assert acct["name"] == f"Gmail ({ADDRESS})" and acct["has_password"] is True
    assert acct["config"]["imap_host"] == "imap.gmail.com"
    assert acct["config"]["drafts_folder"] == "[Gmail]/Drafts"
    assert "refresh" not in json.dumps(acct) and "access-" not in json.dumps(acct)
    async with sessionmaker() as db:
        row = await db.get(IntegrationAccount, uuid.UUID(acct["id"]))
        assert "refresh-1" not in row.credentials  # sealed
        assert accounts.credentials(settings, row)["refresh_token"] == "refresh-1"

    mail.oauth_tokens.add(xoauth("access-1"))
    mail.deliver(message("Invoice", "billing@example.com", "Due Friday"))
    listed = await client.get(
        f"/api/v1/email/messages?account_id={acct['id']}", headers=auth_headers
    )
    assert listed.status_code == 200, listed.text
    assert [m["subject"] for m in listed.json()] == ["Invoice"]
    assert mail.logins[-1] == ("XOAUTH2", xoauth("access-1"))

    sent = await client.post(
        "/api/v1/email/send",
        json={
            "account_id": acct["id"],
            "to": ["sam@example.com"],
            "subject": "Hi",
            "body": "Hello",
        },
        headers=auth_headers,
    )
    assert sent.status_code == 200, sent.text
    assert [m["From"] for m in mail.sent] == [ADDRESS]
    check = await client.post(
        f"/api/v1/integrations/accounts/{acct['id']}/check", headers=auth_headers
    )
    assert check.json() == {"ok": True, "error": None}


async def test_tokens_refresh_and_a_revoked_grant_asks_to_sign_in_again(
    client, auth_headers, googly, mail, sessionmaker, settings
):
    await callback(client, await consent(client, auth_headers, googly))
    [acct] = (await client.get("/api/v1/integrations/accounts", headers=auth_headers)).json()
    async with sessionmaker() as db:  # the stored access token has expired
        row = await db.get(IntegrationAccount, uuid.UUID(acct["id"]))
        stored = accounts.credentials(settings, row)
        accounts.seal_credentials(settings, row, {**stored, "expires_at": time.time() - 10})
        await db.commit()
    google._ACCESS.clear()
    mail.oauth_tokens.add(xoauth("access-2"))
    check = await client.post(
        f"/api/v1/integrations/accounts/{acct['id']}/check", headers=auth_headers
    )
    assert check.json()["ok"] is True
    assert googly.requests[-1]["grant_type"] == "refresh_token"
    assert googly.requests[-1]["refresh_token"] == "refresh-1"
    # Cached: no second refresh while the token is valid.
    before = len(googly.requests)
    await client.post(f"/api/v1/integrations/accounts/{acct['id']}/check", headers=auth_headers)
    assert len(googly.requests) == before

    google._ACCESS.clear()
    googly.refresh_fails = True
    check = await client.post(
        f"/api/v1/integrations/accounts/{acct['id']}/check", headers=auth_headers
    )
    assert check.json()["ok"] is False
    assert "sign in with Google again" in check.json()["error"]


async def test_reconnecting_updates_the_same_account(
    client, auth_headers, googly, settings, sessionmaker
):
    await callback(client, await consent(client, auth_headers, googly))
    googly.refresh_token = None  # Google may leave it out on a later consent
    back = await callback(client, await consent(client, auth_headers, googly, code="code-2"))
    assert back["google"] == "connected"
    rows = (await client.get("/api/v1/integrations/accounts", headers=auth_headers)).json()
    assert len(rows) == 1
    async with sessionmaker() as db:
        row = await db.get(IntegrationAccount, uuid.UUID(rows[0]["id"]))
        stored = accounts.credentials(settings, row)
    assert stored["refresh_token"] == "refresh-1" and stored["access_token"] == "access-2"


async def test_callback_refuses_what_it_should(client, auth_headers, googly):
    params = await consent(client, auth_headers, googly)
    # A different browser (no nonce cookie).
    client.cookies.clear()
    back = await callback(client, params)
    assert back["google"] == "error" and "same browser" in back["message"]
    # A forged or altered state.
    params = await consent(client, auth_headers, googly)
    back = await callback(client, {**params, "state": params["state"][:-4] + "AAAA"})
    assert back["google"] == "error" and "isn't valid" in back["message"]
    # The user said no.
    assert (await callback(client, {"error": "access_denied"}))["google"] == "cancelled"
    # Gmail access unticked on the consent screen.
    googly.scope = "openid email"
    back = await callback(client, await consent(client, auth_headers, googly, code="c2"))
    assert "tick the Gmail box" in back["message"]
    # An ID token meant for another app, or an unverified address.
    googly.scope = "openid email https://mail.google.com/"
    googly.claims = {"aud": "someone-else"}
    back = await callback(client, await consent(client, auth_headers, googly, code="c3"))
    assert "wasn't meant for this server" in back["message"]
    googly.claims = {"email_verified": False}
    back = await callback(client, await consent(client, auth_headers, googly, code="c4"))
    assert "didn't confirm" in back["message"]
    assert (await client.get("/api/v1/integrations/accounts", headers=auth_headers)).json() == []


async def test_expired_state(client, auth_headers, googly, monkeypatch):
    params = await consent(client, auth_headers, googly)
    real = time.time
    monkeypatch.setattr(google.time, "time", lambda: real() + google.STATE_SECONDS + 1)
    back = await callback(client, params)
    assert "took too long" in back["message"]


async def test_state_belongs_to_the_user_who_started(client, auth_headers, googly, sessionmaker):
    params = await consent(client, auth_headers, googly)
    async with sessionmaker() as db:
        db.add(User(username="other", password_hash=hash_password("other-password-123")))
        await db.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "other", "password": "other-password-123"}
    )
    other = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # Finishing the first user's consent connects the mailbox to the first user only.
    await callback(client, params)
    assert (await client.get("/api/v1/integrations/accounts", headers=other)).json() == []
    assert (
        len((await client.get("/api/v1/integrations/accounts", headers=auth_headers)).json()) == 1
    )


async def test_servers_are_fixed_and_removing_revokes(client, auth_headers, googly, sessionmaker):
    await callback(client, await consent(client, auth_headers, googly))
    [acct] = (await client.get("/api/v1/integrations/accounts", headers=auth_headers)).json()
    for body in ({"config": {"imap_host": "evil.example.com"}}, {"password": "x"}):
        changed = await client.patch(
            f"/api/v1/integrations/accounts/{acct['id']}", json=body, headers=auth_headers
        )
        assert changed.status_code == 422
    renamed = await client.patch(
        f"/api/v1/integrations/accounts/{acct['id']}",
        json={"name": "Personal"},
        headers=auth_headers,
    )
    assert renamed.json()["name"] == "Personal"
    # A Gmail account can't be created by hand with other servers.
    made = await client.post(
        "/api/v1/integrations/accounts",
        json={"kind": "email", "provider": "gmail", "name": "x", "config": {}},
        headers=auth_headers,
    )
    assert made.status_code == 422
    removed = await client.delete(
        f"/api/v1/integrations/accounts/{acct['id']}", headers=auth_headers
    )
    assert removed.status_code == 204
    assert googly.revoked == ["refresh-1"]
    async with sessionmaker() as db:
        assert (await db.scalars(select(IntegrationAccount))).all() == []

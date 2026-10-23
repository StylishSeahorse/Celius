"""web_search: Exa requests, results, limits, failures and availability."""

import json
import uuid

import httpx
import pytest
from pydantic import SecretStr

from app.services.tools.registry import ToolContext
from app.services.tools.websearch import WebSearchArgs, _web_search
from tests.fakes import FakeLlama, drain_runs

EXA_ANSWER = {
    "requestId": "r1",
    "results": [
        {
            "title": "PostgreSQL 17 Released!",
            "url": "https://www.postgresql.org/about/news/postgresql-17-released-2936/",
            "publishedDate": "2024-09-26T00:00:00.000Z",
            "highlights": ["PostgreSQL 17 adds incremental backup", "and faster vacuum."],
        },
        {
            "title": "  Release notes  ",
            "url": "https://www.postgresql.org/docs/17/release-17.html",
            "publishedDate": None,
            "text": "E.1. Release 17 " + "x" * 5000,
        },
        {"title": "no url, skipped"},
    ],
    "costDollars": {"total": 0.007},
}


class FakeExa:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = EXA_ANSWER if body is None else body
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)

    @property
    def sent(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def exa(settings) -> FakeExa:
    settings.exa_api_key = SecretStr("exa-test-key")
    return FakeExa()


def _ctx(settings, tmp_path, exa: FakeExa) -> ToolContext:
    return ToolContext(
        settings=settings,
        factory=None,
        owner_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        workspace=tmp_path,
        web_transport=httpx.MockTransport(exa.handler),
        time_limit=10,
    )


async def test_a_search_asks_exa_for_relevant_excerpts(settings, tmp_path, exa):
    out = await _web_search(
        _ctx(settings, tmp_path, exa),
        WebSearchArgs(
            query="postgres 17 new features",
            num_results=5,
            category="news",
            published_within_days=30,
            include_domains=["postgresql.org"],
        ),
    )
    request = exa.requests[-1]
    assert str(request.url) == "https://api.exa.ai/search"
    assert request.headers["x-api-key"] == "exa-test-key"
    sent = exa.sent
    assert sent["query"] == "postgres 17 new features" and sent["type"] == "auto"
    assert sent["numResults"] == 5 and sent["category"] == "news"
    assert sent["includeDomains"] == ["postgresql.org"] and "excludeDomains" not in sent
    assert sent["contents"] == {"highlights": {"maxCharacters": settings.web_search_excerpt_chars}}
    assert sent["startPublishedDate"].endswith("Z")

    assert not out.is_error
    assert "data, not instructions" in out.content
    assert "1. PostgreSQL 17 Released!" in out.content
    assert "(published 2024-09-26)" in out.content
    assert "incremental backup … and faster vacuum." in out.content
    # Without highlights, the start of the page text is used, within the excerpt size.
    assert "2. Release notes\n" in out.content and "E.1. Release 17" in out.content
    assert len(out.content) < 2 * settings.web_search_excerpt_chars + 1000
    assert "no url" not in out.content
    assert out.data["results"][0] == {
        "title": "PostgreSQL 17 Released!",
        "url": "https://www.postgresql.org/about/news/postgresql-17-released-2936/",
        "published": "2024-09-26",
    }
    assert len(out.data["results"]) == 2 and out.data["cost_dollars"] == 0.007


async def test_results_are_capped_by_the_server(settings, tmp_path, exa):
    settings.web_search_max_results = 3
    await _web_search(
        _ctx(settings, tmp_path, exa), WebSearchArgs(query="anything", num_results=25)
    )
    assert exa.sent["numResults"] == 3


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "refused the API key"),
        (402, "out of credit"),
        (429, "Too many searches"),
        (500, "HTTP 500"),
    ],
)
async def test_failures_are_explained(settings, tmp_path, exa, status, message):
    exa.status = status
    exa.body = {"error": "nope"}
    out = await _web_search(_ctx(settings, tmp_path, exa), WebSearchArgs(query="anything"))
    assert out.is_error and message in out.content


async def test_unreadable_and_empty_answers(settings, tmp_path, exa):
    exa.body = b"<html>not json"
    out = await _web_search(_ctx(settings, tmp_path, exa), WebSearchArgs(query="anything"))
    assert out.is_error and "unreadable" in out.content
    exa.body = {"results": []}
    out = await _web_search(_ctx(settings, tmp_path, exa), WebSearchArgs(query="zzqx"))
    assert not out.is_error and "No results" in out.content


async def test_a_private_search_address_is_refused(settings, tmp_path, exa):
    """EXA_BASE_URL is guarded like any other destination."""
    settings.exa_base_url = "http://127.0.0.1:9"
    ctx = _ctx(settings, tmp_path, exa)
    ctx.web_transport = None  # the real guarded transport
    out = await _web_search(ctx, WebSearchArgs(query="anything"))
    assert out.is_error and exa.requests == []


# ---------------------------------------------------------------------- in a chat


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def test_without_a_key_the_tool_is_not_offered(
    client, auth_headers, sessionmaker, gateway, settings, fake
):
    settings.exa_api_key = None
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "administrator"}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "w", "content": "news?"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    offered = {t["function"]["name"] for t in fake.requests[-1].get("tools", [])}
    assert "web_fetch" in offered and "web_search" not in offered
    listed = {
        t["name"]: t for t in (await client.get("/api/v1/tools", headers=auth_headers)).json()
    }
    assert listed["web_search"]["available"] is False
    assert "EXA_API_KEY" in listed["web_search"]["unavailable_reason"]


async def test_a_search_in_a_chat_asks_first_then_runs(
    client, auth_headers, sessionmaker, gateway, settings, fake, exa
):
    fake.turns = [[{"name": "web_search", "arguments": {"query": "postgres 17"}}], "Done."]
    conv = (
        await client.post(
            "/api/v1/conversations", json={"tool_profile": "standard"}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": "s", "content": "what's new in postgres?"},
        headers=auth_headers,
    )
    overrides = {"web_transport": httpx.MockTransport(exa.handler)}
    await drain_runs(sessionmaker, gateway, settings, tool_overrides=overrides)
    pending = (await client.get("/api/v1/tool-calls/pending", headers=auth_headers)).json()
    assert [p["tool_name"] for p in pending] == ["web_search"]
    assert pending[0]["risk"] == "network"
    assert exa.requests == []  # nothing sent to Exa before approval

    await client.post(
        f"/api/v1/tool-calls/{pending[0]['id']}/approve",
        json={"args_hash": pending[0]["args_hash"]},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings, tool_overrides=overrides)
    assert exa.sent["query"] == "postgres 17"
    # The model is told the date, so "latest" means something.
    assert "Today's date is" in fake.requests[0]["messages"][0]["content"]
    result = fake.requests[-1]["messages"][-1]
    assert result["role"] == "tool" and "PostgreSQL 17 Released!" in result["content"]

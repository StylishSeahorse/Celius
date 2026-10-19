import httpx
import pytest


async def test_live(client):
    response = await client.get("/api/v1/health/live")
    assert response.json() == {"status": "ok"}


async def test_ready_reports_model_but_does_not_gate(client):
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["database"]["ok"] is True
    assert "model_server" in body["checks"]


@pytest.fixture
def model_handler():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    return handler


async def test_ready_when_model_unreachable(client):
    body = (await client.get("/api/v1/health/ready")).json()
    assert body["status"] == "ready"
    assert body["checks"]["model_server"] == {
        "ok": False,
        "provider": "llama_cpp",
        "latency_ms": None,
        "detail": "unreachable",
    }


async def test_web_app_served_with_spa_fallback(tmp_path, settings):
    from app.main import create_app

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "app-123.js").write_text("console.log(1)")
    settings.static_root = str(tmp_path)
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/")).text == "<html>app</html>"
        assert (await client.get("/some/client/route")).text == "<html>app</html>"
        asset = await client.get("/assets/app-123.js")
        assert "immutable" in asset.headers["cache-control"]
        assert (await client.get("/api/v1/nope")).status_code == 404
        assert (await client.get("/api/v1/health/live")).json() == {"status": "ok"}

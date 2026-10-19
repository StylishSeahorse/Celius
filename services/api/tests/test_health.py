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

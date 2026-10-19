import httpx
import pytest


@pytest.fixture
def model_handler():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "no tools"}}]})
        return httpx.Response(404)

    return handler


async def test_list_models(client, auth_headers):
    body = (await client.get("/api/v1/models", headers=auth_headers)).json()
    assert body["default_model_id"] == "local-model"
    assert body["health"]["ok"] is True
    assert [m["id"] for m in body["models"]] == ["local-model"]
    assert "raw" not in body["models"][0]


async def test_verify_tools_reports_unsupported(client, auth_headers):
    response = await client.post("/api/v1/models/local-model/verify-tools", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["tool_calling"] is False
    assert response.json()["source"] == "verified"

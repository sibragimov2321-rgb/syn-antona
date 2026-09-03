import json

import httpx
import pytest

from app.ai.models import AIResult
from app.ai.service import AIUnavailable, OpenAICompatibleProvider
from app.core.config import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/"])
async def test_private_hermes_uses_authenticated_request_without_redirects(monkeypatch, suffix):
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": '{"status":"OK"}'}}]},
            )

    monkeypatch.setattr("app.ai.service.httpx.AsyncClient", Client)
    settings = Settings(
        ai_api_key="test-key", ai_model="hermes-agent",
        ai_base_url="http://hermes.railway.internal:8642/v1" + suffix,
    )
    result = await OpenAICompatibleProvider(settings).complete_json("connectivity only", AIResult)
    assert result == {"status": "OK"}
    assert captured["url"] == "http://hermes.railway.internal:8642/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer test-key"}
    assert captured["options"]["follow_redirects"] is False
    assert captured["body"]["model"] == "hermes-agent"
    assert captured["body"]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("base,model,key", [
    ("http://example.com/v1", "hermes-agent", "test-key"),
    ("http://127.0.0.1:8642/v1", "hermes-agent", "test-key"),
    ("http://other.railway.internal:8642/v1", "hermes-agent", "test-key"),
    ("http://hermes.railway.internal.evil.com:8642/v1", "hermes-agent", "test-key"),
    ("http://hermes.railway.internal:80/v1", "hermes-agent", "test-key"),
    ("http://hermes.railway.internal:8642/v1?x=1", "hermes-agent", "test-key"),
    ("http://hermes.railway.internal:8642/v1#x", "hermes-agent", "test-key"),
    ("http://user:password@hermes.railway.internal:8642/v1", "hermes-agent", "test-key"),
    ("http://hermes.railway.internal:8642/v1", "other-model", "test-key"),
    ("http://hermes.railway.internal:8642/v1", "hermes-agent", None),
])
async def test_unapproved_plain_http_or_missing_key_blocked_before_request(
    monkeypatch, base, model, key,
):
    def forbidden_client(**kwargs):
        pytest.fail("HTTP client must not be created")

    monkeypatch.setattr("app.ai.service.httpx.AsyncClient", forbidden_client)
    settings = Settings(ai_api_key=key, ai_base_url=base, ai_model=model)
    with pytest.raises(AIUnavailable):
        await OpenAICompatibleProvider(settings).complete_json("connectivity only", AIResult)


@pytest.mark.asyncio
async def test_private_hermes_redirect_is_not_followed(monkeypatch):
    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://outside.example/v1"})

    monkeypatch.setattr(
        "app.ai.service.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(
        ai_api_key="test-key", ai_model="hermes-agent",
        ai_base_url="http://hermes.railway.internal:8642/v1",
    )
    with pytest.raises(AIUnavailable, match="AI HTTP 307"):
        await OpenAICompatibleProvider(settings).complete_json("connectivity only", AIResult)
    assert requests == ["http://hermes.railway.internal:8642/v1/chat/completions"]
    assert "test-key" not in json.dumps(requests)

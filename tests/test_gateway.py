import json

import httpx
import pytest

from common.config import get_settings
from services.gateway.app import app as gateway_app
from services.gateway.signature import is_valid, sign
from tests.fixtures.events import delivery_id, pr_event

SECRET = "test-secret"


@pytest.fixture
def forwarded(monkeypatch):
    """Points the gateway at a fake webhook service and records what it receives."""
    monkeypatch.setattr(get_settings(), "github_webhook_secret", SECRET)
    monkeypatch.setattr(get_settings(), "internal_token", "tok")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": "queued"})

    gateway_app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://webhook")
    return seen


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw") as c:
        yield c


def _headers(body: bytes, secret: str = SECRET) -> dict:
    return {
        "x-github-event": "pull_request",
        "x-github-delivery": delivery_id(),
        "x-hub-signature-256": sign(secret, body),
        "content-type": "application/json",
    }


def test_signature_helpers():
    body = b'{"a":1}'
    assert is_valid("s", body, sign("s", body))
    assert not is_valid("s", body, sign("other", body))
    assert not is_valid("s", body + b" ", sign("s", body))  # any byte change breaks it
    assert not is_valid("s", body, None)
    assert not is_valid("", body, sign("", body))  # no secret configured -> never valid


async def test_valid_signature_is_forwarded_with_internal_token(client, forwarded):
    body = json.dumps(pr_event()).encode()
    resp = await client.post("/webhook/github", content=body, headers=_headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"result": "queued"}
    (req,) = forwarded
    assert req.url.path == "/events"
    assert req.content == body  # forwarded byte-for-byte
    assert req.headers["x-internal-token"] == "tok"
    assert req.headers["x-github-event"] == "pull_request"


async def test_bad_signature_is_rejected(client, forwarded):
    body = json.dumps(pr_event()).encode()
    resp = await client.post("/webhook/github", content=body, headers=_headers(body, secret="wrong"))
    assert resp.status_code == 401
    assert forwarded == []


async def test_missing_github_headers_is_400(client, forwarded):
    resp = await client.post("/webhook/github", content=b"{}")
    assert resp.status_code == 400
    assert forwarded == []


async def test_webhook_service_down_is_502(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_webhook_secret", SECRET)

    def boom(request):
        raise httpx.ConnectError("refused")

    gateway_app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://webhook")
    body = b"{}"
    resp = await client.post("/webhook/github", content=body, headers=_headers(body))
    assert resp.status_code == 502

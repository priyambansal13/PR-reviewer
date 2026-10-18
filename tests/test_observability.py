import httpx
from prometheus_client import REGISTRY

from common import tracing
from common.config import get_settings
from services.gateway.app import app as gateway_app


def _value(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_metrics_endpoint_is_internal_only():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw") as c:
        assert (await c.get("/metrics")).status_code == 200  # Prometheus on the compose network
        assert (await c.get("/metrics", headers={"cf-connecting-ip": "1.2.3.4"})).status_code == 404  # via tunnel
        assert (await c.get("/metrics", headers={"x-forwarded-for": "1.2.3.4"})).status_code == 404  # via a LB


async def test_rejected_webhooks_are_counted(monkeypatch):
    monkeypatch.setattr(get_settings(), "github_webhook_secret", "s")
    before = _value("prr_webhooks_total", event="push", result="bad_signature")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw") as c:
        r = await c.post(
            "/webhook/github",
            content=b"{}",
            headers={"x-github-event": "push", "x-github-delivery": "d", "x-hub-signature-256": "sha256=bad"},
        )
    assert r.status_code == 401
    assert _value("prr_webhooks_total", event="push", result="bad_signature") == before + 1


def test_tracing_is_a_no_op_without_keys(monkeypatch):
    monkeypatch.setattr(get_settings(), "langfuse_public_key", "")
    with tracing.trace("x", seed="s", session_id="p", tags=[], metadata={}) as tr:
        assert tr.callbacks == [] and tr.trace_id is None
        tr.output({"ok": True})  # safe to call

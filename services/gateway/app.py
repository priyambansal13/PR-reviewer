"""Gateway — the only public service. Verifies GitHub's HMAC signature and forwards verified events internally.

Run: uv run uvicorn services.gateway.app:app --port 8000
"""

import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_fastapi_instrumentator import Instrumentator

from common.config import get_settings
from common.logging import setup_logging
from common.metrics import WEBHOOKS
from services.gateway.signature import is_valid

log = setup_logging("gateway")

MAX_BODY_BYTES = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB
FORWARDED_HEADERS = ("x-github-event", "x-github-delivery", "x-github-hook-installation-target-id")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.github_webhook_secret:
        log.warning("GITHUB_WEBHOOK_SECRET is empty — every webhook will be rejected")
    app.state.http = httpx.AsyncClient(base_url=settings.webhook_service_url, timeout=10)
    app.state.internal = httpx.AsyncClient(timeout=30)  # admin API calls to the other internal services
    yield
    await app.state.http.aclose()
    await app.state.internal.aclose()


app = FastAPI(title="PR Reviewer · Gateway", lifespan=lifespan)
Instrumentator(excluded_handlers=["/metrics", "/healthz"]).instrument(app).expose(app, include_in_schema=False)


@app.middleware("http")
async def metrics_are_internal(request: Request, call_next):
    # The gateway is public; /metrics is for Prometheus on the internal network only. Requests that came
    # through the tunnel (Cloudflare) or a load balancer carry these headers.
    if request.url.path == "/metrics" and (
        "cf-connecting-ip" in request.headers or "x-forwarded-for" in request.headers
    ):
        return Response(status_code=404)
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook/github")
async def github_webhook(request: Request) -> Response:
    settings = get_settings()

    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY_BYTES:
        WEBHOOKS.labels("unknown", "too_large").inc()
        raise HTTPException(413, "payload too large")
    body = await request.body()

    event = request.headers.get("x-github-event")
    delivery = request.headers.get("x-github-delivery")
    if not event or not delivery:
        WEBHOOKS.labels(event or "unknown", "missing_headers").inc()
        raise HTTPException(400, "missing X-GitHub-Event / X-GitHub-Delivery")

    if not is_valid(settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("rejected delivery %s (%s): bad signature from %s", delivery, event, request.client)
        WEBHOOKS.labels(event, "bad_signature").inc()
        raise HTTPException(401, "invalid signature")

    headers = {h: request.headers[h] for h in FORWARDED_HEADERS if h in request.headers}
    headers["content-type"] = "application/json"
    if settings.internal_token:
        headers["x-internal-token"] = settings.internal_token

    try:
        upstream = await request.app.state.http.post("/events", content=body, headers=headers)
    except httpx.HTTPError as exc:
        log.error("webhook service unreachable for delivery %s: %s", delivery, exc)
        WEBHOOKS.labels(event, "upstream_error").inc()
        # 502 makes GitHub mark the delivery failed, so it can be redelivered from the App settings page
        raise HTTPException(502, "webhook service unavailable") from exc

    log.info("delivery %s (%s) -> %s", delivery, event, upstream.status_code)
    WEBHOOKS.labels(event, "forwarded" if upstream.status_code < 400 else f"upstream_{upstream.status_code}").inc()
    return Response(upstream.content, upstream.status_code, media_type="application/json")


# --- admin API: /api/<service>/<path> -> that internal service ------------------------------------------------------

INTERNAL_SERVICES = {
    "orchestrator": lambda s: s.orchestrator_service_url,
    "reviewer": lambda s: s.reviewer_service_url,
    "learner": lambda s: s.learner_service_url,
}


@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def admin_api(service: str, path: str, request: Request) -> Response:
    """Authenticated pass-through to the internal services' APIs (e.g. GET /api/orchestrator/reviews)."""
    settings = get_settings()
    if not settings.admin_api_token:
        raise HTTPException(503, "admin API disabled (ADMIN_API_TOKEN not set)")
    auth = request.headers.get("authorization", "")
    if not secrets.compare_digest(auth, f"Bearer {settings.admin_api_token}"):
        raise HTTPException(401, "missing or invalid bearer token")
    if service not in INTERNAL_SERVICES:
        raise HTTPException(404, f"unknown service {service!r}")

    url = f"{INTERNAL_SERVICES[service](settings).rstrip('/')}/{path}"
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if settings.internal_token:
        headers["x-internal-token"] = settings.internal_token
    try:
        upstream = await request.app.state.internal.request(
            request.method, url, params=request.query_params, content=await request.body(), headers=headers
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{service} service unavailable") from exc
    return Response(upstream.content, upstream.status_code, media_type=upstream.headers.get("content-type"))

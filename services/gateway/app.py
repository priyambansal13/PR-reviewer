"""Gateway — the only public service. Verifies GitHub's HMAC signature and forwards verified events internally.

Run: uv run uvicorn services.gateway.app:app --port 8000
"""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from common.config import get_settings
from common.logging import setup_logging
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
    yield
    await app.state.http.aclose()


app = FastAPI(title="PR Reviewer · Gateway", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook/github")
async def github_webhook(request: Request) -> Response:
    settings = get_settings()

    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY_BYTES:
        raise HTTPException(413, "payload too large")
    body = await request.body()

    event = request.headers.get("x-github-event")
    delivery = request.headers.get("x-github-delivery")
    if not event or not delivery:
        raise HTTPException(400, "missing X-GitHub-Event / X-GitHub-Delivery")

    if not is_valid(settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("rejected delivery %s (%s): bad signature from %s", delivery, event, request.client)
        raise HTTPException(401, "invalid signature")

    headers = {h: request.headers[h] for h in FORWARDED_HEADERS if h in request.headers}
    headers["content-type"] = "application/json"
    if settings.internal_token:
        headers["x-internal-token"] = settings.internal_token

    try:
        upstream = await request.app.state.http.post("/events", content=body, headers=headers)
    except httpx.HTTPError as exc:
        log.error("webhook service unreachable for delivery %s: %s", delivery, exc)
        # 502 makes GitHub mark the delivery failed, so it can be redelivered from the App settings page
        raise HTTPException(502, "webhook service unavailable") from exc

    log.info("delivery %s (%s) -> %s", delivery, event, upstream.status_code)
    return Response(upstream.content, upstream.status_code, media_type="application/json")

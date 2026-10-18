"""Webhook service — internal. Parses events, de-duplicates, stores metadata, enqueues Celery tasks.

Run: uv run uvicorn services.webhook.app:app --port 8001
"""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.celery_app import celery_app
from common.config import get_settings
from common.db.models import ReviewRun, WebhookDelivery
from common.db.session import get_engine
from common.logging import setup_logging
from common.metrics import EVENTS
from services.webhook.handlers import InvalidPayload, process_event

log = setup_logging("webhook")

app = FastAPI(title="PR Reviewer · Webhook")
Instrumentator(excluded_handlers=["/metrics", "/healthz"]).instrument(app).expose(app, include_in_schema=False)


class Enqueuer(Protocol):
    def send(self, task_name: str, kwargs: dict[str, Any]) -> None: ...


class CeleryEnqueuer:
    def send(self, task_name: str, kwargs: dict[str, Any]) -> None:
        # send_task by *name*: this service never imports worker code
        celery_app.send_task(task_name, kwargs=kwargs)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        yield session


def get_enqueuer() -> Enqueuer:
    return CeleryEnqueuer()


def check_internal_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().internal_token
    if expected and not secrets.compare_digest(x_internal_token or "", expected):
        raise HTTPException(403, "internal endpoint")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/events", dependencies=[Depends(check_internal_token)])
async def events(
    request: Request,
    x_github_event: Annotated[str, Header()],
    x_github_delivery: Annotated[str, Header()],
    session: Annotated[AsyncSession, Depends(get_session)],
    enqueuer: Annotated[Enqueuer, Depends(get_enqueuer)],
) -> dict:
    try:
        payload = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(400, "body is not JSON") from exc

    try:
        outcome = await process_event(session, x_github_event, x_github_delivery, payload)
    except InvalidPayload as exc:
        await session.rollback()
        log.warning("delivery %s: invalid %s payload: %s", x_github_delivery, x_github_event, exc)
        raise HTTPException(422, "unexpected payload shape") from exc
    await session.commit()

    for task_name, kwargs in outcome.tasks:
        try:
            await asyncio.to_thread(enqueuer.send, task_name, kwargs)
        except Exception as exc:
            # Redis is down. Undo the claim so a GitHub redelivery is processed instead of seen as a duplicate.
            log.exception("delivery %s: enqueue %s failed", x_github_delivery, task_name)
            await _forget(session, x_github_delivery, outcome.run_id)
            raise HTTPException(503, "queue unavailable, redeliver later") from exc

    EVENTS.labels(x_github_event, payload.get("action") or "-", outcome.result).inc()
    return outcome.as_json()


async def _forget(session: AsyncSession, delivery_id: str, run_id) -> None:
    await session.execute(delete(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id))
    if run_id is not None:
        await session.execute(delete(ReviewRun).where(ReviewRun.id == run_id))
    await session.commit()

"""Webhook service — internal. Parses events, de-duplicates, stores metadata, enqueues Celery tasks.

Run: uv run uvicorn services.webhook.app:app --port 8001
"""

import asyncio
import json
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from common.api import Enqueuer, check_internal_token, get_enqueuer, get_session, make_app
from common.db.models import ReviewRun, WebhookDelivery
from common.logging import setup_logging
from common.metrics import EVENTS
from services.webhook.handlers import InvalidPayload, process_event

log = setup_logging("webhook")

app = make_app("Webhook")  # /events checks the internal token itself (below)

__all__ = ["app", "get_enqueuer", "get_session"]  # tests override these dependencies


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

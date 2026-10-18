"""Reviewer service API — posting state of a run, and re-posting saved findings (no LLM calls).

Run: uv run uvicorn services.reviewer.app:app --port 8003
"""

import asyncio
import uuid

from fastapi import HTTPException

from common.api import Queue, Session, internal_router, make_app
from common.celery_app import TASK_POST_REVIEW
from services.reviewer.runs import NotPostable, posting_status, reset_for_repost

app = make_app("Reviewer")
api = internal_router()


@api.get("/reviews/{run_id}/posting")
async def posting(run_id: uuid.UUID, session: Session) -> dict:
    if (status := await posting_status(session, run_id)) is None:
        raise HTTPException(404, "run not found")
    return status


@api.post("/reviews/{run_id}/post", status_code=202)
async def post(run_id: uuid.UUID, session: Session, queue: Queue) -> dict:
    """Post the run's saved findings to the PR. Safe to repeat: an already-posted review is recognised."""
    try:
        await reset_for_repost(session, run_id)
    except LookupError:
        raise HTTPException(404, "run not found") from None
    except NotPostable as exc:
        raise HTTPException(409, str(exc)) from None
    await session.commit()
    await asyncio.to_thread(queue.send, TASK_POST_REVIEW, {"run_id": str(run_id)})
    return {"run_id": str(run_id), "status": "posting"}


app.include_router(api)

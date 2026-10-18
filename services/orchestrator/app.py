"""Orchestrator service API — review runs: list, inspect, re-run. The work itself happens in the Celery worker.

Run: uv run uvicorn services.orchestrator.app:app --port 8002
"""

import asyncio
import uuid

from fastapi import HTTPException, Query

from common.api import Queue, Session, internal_router, make_app
from common.celery_app import TASK_REVIEW_PR
from services.orchestrator.runs import Busy, get_run, list_runs, reset_for_rereview

app = make_app("Orchestrator")
api = internal_router()


@api.get("/reviews")
async def reviews(
    session: Session,
    repo: str | None = Query(None, description="owner/name"),
    pr: int | None = Query(None, description="PR number (with repo)"),
    limit: int = Query(20, le=100),
) -> list[dict]:
    return await list_runs(session, repo, pr, limit)


@api.get("/reviews/{run_id}")
async def review(run_id: uuid.UUID, session: Session) -> dict:
    if (run := await get_run(session, run_id)) is None:
        raise HTTPException(404, "run not found")
    return run


@api.post("/reviews/{run_id}/rerun", status_code=202)
async def rerun(run_id: uuid.UUID, session: Session, queue: Queue) -> dict:
    """Review the run's commit again from scratch (new LLM calls, new check run, new GitHub review)."""
    try:
        await reset_for_rereview(session, run_id)
    except LookupError:
        raise HTTPException(404, "run not found") from None
    except Busy as exc:
        raise HTTPException(409, f"run is {exc} — wait for it to finish") from None
    await session.commit()
    await asyncio.to_thread(queue.send, TASK_REVIEW_PR, {"run_id": str(run_id)})
    return {"run_id": str(run_id), "status": "queued"}


app.include_router(api)

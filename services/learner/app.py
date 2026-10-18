"""Learner service API — what it knows about a repo, and learning from a PR on demand.

Run: uv run uvicorn services.learner.app:app --port 8004
"""

import asyncio

from fastapi import HTTPException

from common.api import Queue, Session, internal_router, make_app
from common.celery_app import TASK_LEARN_FROM_PR
from services.learner.knowledge import find_pr_id, repo_knowledge

app = make_app("Learner")
api = internal_router()


@api.get("/repos/{owner}/{name}/knowledge")
async def knowledge(owner: str, name: str, session: Session) -> dict:
    if (k := await repo_knowledge(session, f"{owner}/{name}")) is None:
        raise HTTPException(404, "repository not found")
    return k


@api.post("/repos/{owner}/{name}/pulls/{number}/learn", status_code=202)
async def learn(owner: str, name: str, number: int, session: Session, queue: Queue) -> dict:
    """Run the Learner on this PR now (re-learns even if already learned). Unmerged PRs: feedback only."""
    if (pr_id := await find_pr_id(session, f"{owner}/{name}", number)) is None:
        raise HTTPException(404, "pull request not found")
    await asyncio.to_thread(queue.send, TASK_LEARN_FROM_PR, {"pr_id": str(pr_id), "force": True})
    return {"pr_id": str(pr_id), "status": "queued"}


app.include_router(api)

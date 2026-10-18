"""Orchestrator worker. Phase 1 stub: confirms the run arrived. Phase 2 fetches the diff, Phase 3 runs LangGraph.

Run: uv run celery -A services.orchestrator.worker worker -Q review -n orchestrator@%h --loglevel=INFO
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from common.celery_app import TASK_REVIEW_PR, celery_app
from common.db.models import PullRequest, ReviewRun
from common.db.session import dispose_engine, session_scope
from common.logging import setup_logging

log = setup_logging("orchestrator")


async def _review(run_id: str) -> None:
    try:
        async with session_scope() as session:
            run = await session.scalar(
                select(ReviewRun)
                .where(ReviewRun.id == uuid.UUID(run_id))
                .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
            )
            if run is None:
                log.warning("run %s not found", run_id)
                return
            pr = run.pull_request
            log.info(
                "received run %s for %s#%d @ %s (status=%s) — review pipeline arrives in Phase 2/3",
                run_id,
                pr.repository.full_name,
                pr.number,
                run.head_sha[:7],
                run.status,
            )
    finally:
        await dispose_engine()


@celery_app.task(name=TASK_REVIEW_PR)
def review_pr(run_id: str) -> None:
    asyncio.run(_review(run_id))

"""Posting state of a run — used by the reviewer API and scripts/runs.py."""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.models import Finding, ReviewRun, RunStatus

POSTABLE = (RunStatus.saved, RunStatus.posted, RunStatus.skipped, RunStatus.failed)


class NotPostable(Exception):
    pass


async def posting_status(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None:
    run = await session.get(ReviewRun, run_id)
    if run is None:
        return None
    posted = await session.scalar(
        select(func.count()).where(Finding.run_id == run_id, Finding.github_comment_id.is_not(None))
    )
    return {
        "id": str(run.id),
        "status": run.status.value,
        "github_review_id": run.github_review_id,
        "inline_comments_linked": posted,
        "error": run.error,
    }


async def reset_for_repost(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Marks saved findings for posting again. No LLM calls; the hidden marker prevents double posts."""
    run = await session.get(ReviewRun, run_id)
    if run is None:
        raise LookupError(run_id)
    if run.status not in POSTABLE or not run.finished_at:
        raise NotPostable(f"run is {run.status.value}" + ("" if run.finished_at else ", never reviewed"))
    await session.execute(update(Finding).where(Finding.run_id == run_id).values(posted=False, github_comment_id=None))
    await session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id)
        .values(status=RunStatus.saved, github_review_id=None, error=None)
    )

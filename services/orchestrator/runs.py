"""Reading and resetting review runs — used by the orchestrator API and by scripts/runs.py."""

import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from common import tracing
from common.db.models import Finding, PullRequest, Repository, ReviewRun, RunStatus

IN_PROGRESS = (RunStatus.queued, RunStatus.fetching, RunStatus.reviewing, RunStatus.posting)
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class Busy(Exception):
    """The run is being processed right now; resetting it would race the worker."""


async def list_runs(
    session: AsyncSession, repo: str | None = None, number: int | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    findings = select(func.count()).where(Finding.run_id == ReviewRun.id).scalar_subquery()
    q = (
        select(ReviewRun, findings)
        .join(PullRequest)
        .join(Repository)
        .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
        .order_by(ReviewRun.created_at.desc())
        .limit(min(limit, 100))
    )
    if repo:
        q = q.where(Repository.full_name == repo)
    if number is not None:
        q = q.where(PullRequest.number == number)
    return [_summary(run, count) for run, count in (await session.execute(q)).all()]


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None:
    run = await session.scalar(
        select(ReviewRun)
        .where(ReviewRun.id == run_id)
        .options(
            selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository),
            selectinload(ReviewRun.findings),
        )
    )
    if run is None:
        return None
    findings = sorted(run.findings, key=lambda f: (SEVERITY_ORDER[f.severity.value], f.file, f.line or 0))
    return {
        **_summary(run, len(findings)),
        "summary": run.summary,
        "findings": [
            {
                "severity": f.severity.value,
                "file": f.file,
                "line": f.line,
                "title": f.title,
                "category": f.category,
                "owasp_id": f.owasp_id,
                "agents": f.agents,
                "explanation": f.body,
                "suggestion": f.suggestion,
                "confidence": f.confidence,
                "posted": f.posted,
                "thumbs_up": f.thumbs_up,
                "thumbs_down": f.thumbs_down,
            }
            for f in findings
        ],
    }


async def reset_for_rereview(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Clears a run's results so the orchestrator reviews it from scratch (new check run, new review)."""
    status = await session.scalar(select(ReviewRun.status).where(ReviewRun.id == run_id))
    if status is None:
        raise LookupError(run_id)
    if status in IN_PROGRESS:
        raise Busy(status.value)
    await session.execute(delete(Finding).where(Finding.run_id == run_id))
    await session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id)
        .values(
            status=RunStatus.queued,
            error=None,
            summary=None,
            verdict=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            started_at=None,
            finished_at=None,
            github_check_run_id=None,
            github_review_id=None,
            langfuse_trace_id=None,
        )
    )


def _summary(run: ReviewRun, findings: int) -> dict[str, Any]:
    pr = run.pull_request
    return {
        "id": str(run.id),
        "repo": pr.repository.full_name,
        "pr": pr.number,
        "pr_title": pr.title,
        "head_sha": run.head_sha,
        "status": run.status.value,
        "verdict": run.verdict,
        "findings": findings,
        "tokens": run.tokens_in + run.tokens_out,
        "cost_usd": float(run.cost_usd),
        "error": run.error,
        "github_review_id": run.github_review_id,
        "trace_url": _trace_url(run.langfuse_trace_id),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@lru_cache(maxsize=1024)
def _trace_url(trace_id: str | None) -> str | None:
    lf = tracing.langfuse() if trace_id else None
    if lf is None:
        return None
    try:
        return lf.get_trace_url(trace_id=trace_id)
    except Exception:
        return None

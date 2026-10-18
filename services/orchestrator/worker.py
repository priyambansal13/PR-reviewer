"""Orchestrator worker: fetches the PR diff (Phase 2); runs the LangGraph review (Phase 3).

Run: uv run celery -A services.orchestrator.worker worker -Q review -n orchestrator@%h --loglevel=INFO
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from common.celery_app import TASK_REVIEW_PR, celery_app
from common.config import get_settings
from common.db.models import PullRequest, ReviewRun, RunStatus
from common.db.session import dispose_engine, session_scope
from common.github import GitHubAppAuth, GitHubClient, GitHubRetryable
from common.logging import setup_logging
from services.orchestrator.fetch import Superseded, fetch_diff

log = setup_logging("orchestrator")

# A redelivered task (worker crash with acks_late) may find its run already in progress; anything else is done.
RUNNABLE = (RunStatus.queued, RunStatus.fetching, RunStatus.reviewing)


async def _set(run_id: uuid.UUID, **values: Any) -> None:
    async with session_scope() as session:
        await session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(**values))


async def review(run_id: str) -> str:
    settings = get_settings()
    rid = uuid.UUID(run_id)
    async with session_scope() as session:
        run = await session.scalar(
            select(ReviewRun)
            .where(ReviewRun.id == rid)
            .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
        )
        if run is None:
            log.warning("run %s not found", run_id)
            return "missing"
        if run.status not in RUNNABLE:
            log.info("run %s already %s — nothing to do", run_id, run.status)
            return run.status.value
        pr, repo = run.pull_request, run.pull_request.repository
        head_sha, base_sha, installation_id = run.head_sha, run.base_sha, repo.installation_id
        label = f"{repo.full_name}#{pr.number}@{head_sha[:7]}"

    if installation_id is None:
        await _set(rid, status=RunStatus.failed, error="repository has no GitHub App installation")
        return "failed"

    await _set(rid, status=RunStatus.fetching, started_at=datetime.now(UTC), error=None)

    redis = Redis.from_url(settings.redis_url)
    async with httpx.AsyncClient(base_url=settings.github_api_url, timeout=30) as http:
        gh = GitHubClient(http, GitHubAppAuth.from_settings(http, redis), installation_id)
        try:
            fetched = await fetch_diff(gh, repo.full_name, pr.number, base_sha, head_sha, settings.max_diff_lines)
        except Superseded as exc:
            log.info("%s: skipped, %s", label, exc)
            await _set(rid, status=RunStatus.skipped, error=str(exc), finished_at=datetime.now(UTC))
            return "skipped"
        finally:
            await redis.aclose()

    sel = fetched.selection
    log.info(
        "%s: %d files in diff (%s), %d selected / %d changed lines, %d skipped",
        label, len(fetched.files), fetched.source, len(sel.files), sel.changed_lines, len(sel.skipped),
    )  # fmt: skip
    for f in sel.files:
        log.info("  review  %-60s +%d -%d  (%d commentable lines)",
                 f.path, f.additions, f.deletions, len(f.commentable_lines()))  # fmt: skip
    for path, reason in sel.skipped:
        log.info("  skip    %-60s %s", path, reason)

    # Phase 3 replaces this with the LangGraph review. Until then the run stops here.
    await _set(
        rid,
        status=RunStatus.skipped,
        files_reviewed=len(sel.files),
        summary=f"diff fetched via {fetched.source}: {len(sel.files)} files, {sel.changed_lines} changed lines",
        error="review engine not built yet (Phase 3)",
        finished_at=datetime.now(UTC),
    )
    return "fetched"


async def _review_task(run_id: str) -> str:
    try:
        return await review(run_id)
    finally:
        await dispose_engine()


@celery_app.task(
    name=TASK_REVIEW_PR,
    bind=True,
    autoretry_for=(GitHubRetryable,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def review_pr(self, run_id: str) -> str:
    try:
        return asyncio.run(_review_task(run_id))
    except GitHubRetryable:
        if self.request.retries >= self.max_retries:
            asyncio.run(_fail(run_id, "GitHub kept failing (rate limit / outage)"))
        raise
    except Exception as exc:
        asyncio.run(_fail(run_id, f"{type(exc).__name__}: {exc}"))
        raise


async def _fail(run_id: str, error: str) -> None:
    try:
        await _set(uuid.UUID(run_id), status=RunStatus.failed, error=error[:2000], finished_at=datetime.now(UTC))
    finally:
        await dispose_engine()

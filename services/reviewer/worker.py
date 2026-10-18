"""Reviewer worker: posts a saved review run to the PR as one GitHub review (inline comments + summary).

Run: uv run celery -A services.reviewer.worker worker -Q post -n reviewer@%h --loglevel=INFO
"""

import logging
import uuid
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from common import aio, worker_metrics  # noqa: F401  (registers the metrics server)
from common.celery_app import TASK_POST_REVIEW, celery_app
from common.config import get_settings
from common.db.models import Finding, PullRequest, ReviewRun, RunStatus
from common.db.session import session_scope
from common.diff import FileDiff, parse_diff, parse_patch
from common.github import DiffUnavailable, GitHubAppAuth, GitHubClient, GitHubError, GitHubRetryable
from common.logging import setup_logging
from common.metrics import INLINE_COMMENTS, POSTED
from services.reviewer.compose import MARKER, build_review

log = setup_logging("reviewer")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _set(run_id: uuid.UUID, **values: Any) -> None:
    async with session_scope() as session:
        await session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(**values))


async def post(run_id: str) -> str:
    settings = get_settings()
    rid = uuid.UUID(run_id)
    async with session_scope() as session:
        run = await session.scalar(
            select(ReviewRun)
            .where(ReviewRun.id == rid)
            .options(
                selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository),
                selectinload(ReviewRun.findings),
            )
        )
        if run is None:
            return "missing"
        if run.status == RunStatus.posted or run.github_review_id:
            return "already posted"
        if run.status not in (RunStatus.saved, RunStatus.posting):
            log.info("run %s is %s — not posting", run_id, run.status)
            return run.status.value
        pr, repo = run.pull_request, run.pull_request.repository
        findings: list[Finding] = list(run.findings)
        head_sha, base_sha = run.head_sha, run.base_sha
        summary, verdict = run.summary or "", run.verdict or "comment"
        tokens, cost = run.tokens_in + run.tokens_out, float(run.cost_usd)
        # Identifies this *result* of the run: `repost` keeps it (never double-posts), `rereview` saves new
        # findings with a new finished_at, so its review is posted fresh.
        review_key = f"{run_id}:{int(run.finished_at.timestamp()) if run.finished_at else 0}"
        label = f"{repo.full_name}#{pr.number}@{head_sha[:7]}"

    await _set(rid, status=RunStatus.posting)
    redis = Redis.from_url(settings.redis_url)
    try:
        async with httpx.AsyncClient(base_url=settings.github_api_url, timeout=30) as http:
            gh = GitHubClient(http, GitHubAppAuth.from_settings(http, redis), repo.installation_id)

            pull = await gh.get_pull(repo.full_name, pr.number)
            if pull["head"]["sha"] != head_sha or pull["state"] != "open":
                why = "PR closed" if pull["state"] != "open" else f"PR head moved to {pull['head']['sha'][:7]}"
                log.info("%s: not posting, %s", label, why)
                await _set(rid, status=RunStatus.skipped, error=f"not posted: {why}")
                POSTED.labels("-", "skipped").inc()
                return "skipped"

            files = await _files(gh, repo.full_name, pr.number, base_sha, head_sha)
            footer = f"<sub>{settings.openai_model} · {tokens:,} tokens · ${cost:.4f} · run `{run_id[:8]}`</sub>"
            payload = build_review(review_key, findings, files, summary, verdict, settings.max_inline_comments, footer)

            # Idempotency: a previous attempt may have posted and then crashed before saving the id.
            # The payload is deterministic, so we can still link its comments to our findings.
            marker = MARKER.format(review_key)
            reviews = await gh.list_reviews(repo.full_name, pr.number)
            if existing := next((r for r in reviews if marker in (r.get("body") or "")), None):
                log.info("%s: review %s already on GitHub — recording it", label, existing["id"])
                await _record(rid, gh, repo.full_name, pr.number, existing["id"], _inline(payload))
                POSTED.labels("-", "already_posted").inc()
                return "already posted"

            review, payload = await _create_with_fallbacks(
                gh,
                repo.full_name,
                pr.number,
                head_sha,
                payload,
                label,
                rebuild_without_inline=lambda: build_review(review_key, findings, files, summary, verdict, 0, footer),
            )

            await _dismiss_stale(gh, repo.full_name, pr.number, reviews, review, head_sha)
            await _record(rid, gh, repo.full_name, pr.number, review["id"], _inline(payload))
            POSTED.labels(payload.event, "posted").inc()
            INLINE_COMMENTS.inc(len(payload.comments))
            log.info(
                "%s: posted review %s (%s, %d inline comments)",
                label,
                review["id"],
                payload.event,
                len(payload.comments),
            )
            return "posted"
    finally:
        await redis.aclose()


def _inline(payload) -> list[tuple[Any, str, int]]:
    return [(fid, c["path"], c["line"]) for fid, c in zip(payload.inline_ids, payload.comments, strict=True)]


async def _create_with_fallbacks(gh, repo, number, head_sha, payload, label, rebuild_without_inline):
    """GitHub answers 422 for different reasons; each has its own fallback, so a review always gets posted."""
    for _ in range(3):
        try:
            review = await gh.create_review(repo, number, head_sha, payload.body, payload.event, payload.comments)
            return review, payload
        except GitHubError as exc:
            if exc.status != 422:
                raise
            msg = str(exc).lower()
            if "own pull request" in msg and payload.event != "COMMENT":
                # Nobody (bots included) may request changes on a PR they opened.
                log.info("%s: PR authored by the bot — posting as COMMENT", label)
                payload.event = "COMMENT"
            elif payload.comments:
                # An inline position GitHub can't resolve. Better a body-only review than none.
                log.warning("%s: inline comments rejected (%s) — posting summary only", label, exc)
                event = payload.event
                payload = rebuild_without_inline()
                payload.event = event
            else:
                raise
    raise GitHubError("could not post review after fallbacks")


async def _files(gh: GitHubClient, repo: str, number: int, base: str, head: str) -> dict[str, FileDiff]:
    try:
        parsed = parse_diff(await gh.get_compare_diff(repo, base, head))
    except DiffUnavailable:
        parsed = [
            parse_patch(f["filename"], f.get("patch"), f.get("status", "modified"), f.get("previous_filename"))
            for f in await gh.list_pull_files(repo, number)
        ]
    return {f.path: f for f in parsed}


async def _dismiss_stale(gh: GitHubClient, repo: str, number: int, reviews: list, new: dict, head_sha: str) -> None:
    """Our older "changes requested" reviews would keep blocking the PR after the author fixed things."""
    me = (new.get("user") or {}).get("id")
    for r in reviews:
        if r["id"] != new["id"] and (r.get("user") or {}).get("id") == me and r.get("state") == "CHANGES_REQUESTED":
            try:
                await gh.dismiss_review(repo, number, r["id"], f"Superseded by the AI review of {head_sha[:7]}.")
            except GitHubError as exc:
                log.info("could not dismiss old review %s: %s", r["id"], exc)


async def _record(
    rid: uuid.UUID, gh: GitHubClient, repo: str, number: int, review_id: int, inline: list[tuple[Any, str, int]]
) -> None:
    """Saves the review id and links each finding to its GitHub comment (the Learner reads 👍/👎 on them)."""
    comment_ids: dict[Any, int] = {}
    if inline:
        try:
            posted = await gh.list_review_comments(repo, number, review_id)
        except GitHubError as exc:
            log.warning("could not list review comments: %s", exc)
            posted = []
        by_position: dict[tuple[str, int], list[int]] = {}
        for c in sorted(posted, key=lambda c: c["id"]):
            by_position.setdefault((c["path"], c.get("line") or c.get("original_line")), []).append(c["id"])
        for fid, path, line in inline:
            if ids := by_position.get((path, line)):
                comment_ids[fid] = ids.pop(0)
    async with session_scope() as session:
        for fid, cid in comment_ids.items():
            await session.execute(update(Finding).where(Finding.id == fid).values(posted=True, github_comment_id=cid))
        await session.execute(
            update(ReviewRun)
            .where(ReviewRun.id == rid)
            .values(status=RunStatus.posted, github_review_id=review_id, error=None)
        )


@celery_app.task(
    name=TASK_POST_REVIEW,
    bind=True,
    autoretry_for=(GitHubRetryable,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def post_review(self, run_id: str) -> str:
    try:
        return aio.run(post(run_id))
    except GitHubRetryable:
        if self.request.retries >= self.max_retries:
            aio.run(_set(uuid.UUID(run_id), status=RunStatus.failed, error="posting: GitHub kept failing"))
        raise
    except Exception as exc:
        POSTED.labels("-", "failed").inc()
        aio.run(_set(uuid.UUID(run_id), status=RunStatus.failed, error=f"posting: {type(exc).__name__}: {exc}"[:2000]))
        raise

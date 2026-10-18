"""Orchestrator worker: fetch the diff, run the LangGraph review, save findings, report on the PR's checks.

Run: uv run celery -A services.orchestrator.worker worker -Q review -n orchestrator@%h --loglevel=INFO
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from common import aio
from common.celery_app import TASK_POST_REVIEW, TASK_REVIEW_PR, celery_app
from common.config import Settings, get_settings
from common.db.models import Finding as FindingRow
from common.db.models import PullRequest, ReviewRun, RunStatus
from common.db.session import session_scope
from common.github import GitHubAppAuth, GitHubClient, GitHubError, GitHubRetryable
from common.logging import setup_logging
from services.orchestrator import report
from services.orchestrator.fetch import Superseded, fetch_diff
from services.orchestrator.graph.cost import cost_usd
from services.orchestrator.graph.graph import build_review_graph
from services.orchestrator.graph.schemas import ReviewContext

log = setup_logging("orchestrator")
CHECK_NAME = "AI Review"

# A redelivered task (worker crash with acks_late) may find its run already in progress; anything else is done.
RUNNABLE = (RunStatus.queued, RunStatus.fetching, RunStatus.reviewing)


def make_llm(settings: Settings) -> ChatOpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=0.1,
        timeout=90,
        max_retries=2,
    )


async def _set(run_id: uuid.UUID, **values: Any) -> None:
    async with session_scope() as session:
        await session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(**values))


async def _tokens_used_today() -> int:
    async with session_scope() as session:
        since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        total = await session.scalar(
            select(func.coalesce(func.sum(ReviewRun.tokens_in + ReviewRun.tokens_out), 0)).where(
                ReviewRun.created_at >= since
            )
        )
        return int(total or 0)


class Checks:
    """The PR's "AI Review" check. Best effort: without the Checks permission the review still runs."""

    def __init__(self, gh: GitHubClient, repo: str, head_sha: str, existing_id: int | None):
        self.gh, self.repo, self.head_sha, self.id = gh, repo, head_sha, existing_id

    async def start(self) -> int | None:
        if self.id is None:
            try:
                self.id = await self.gh.create_check_run(
                    self.repo,
                    self.head_sha,
                    CHECK_NAME,
                    "Reviewing…",
                    "Four AI reviewers (static analysis, security, architecture, style) are looking at this commit.",
                )
            except GitHubError as exc:
                if exc.status == 403:
                    log.warning("no Checks permission on %s — add 'Checks: Read and write' to the App", self.repo)
                else:
                    log.warning("could not create check run: %s", exc)
        return self.id

    async def finish(self, conclusion: str, title: str, summary: str, text: str | None = None) -> None:
        if self.id is None:
            return
        try:
            await self.gh.complete_check_run(self.repo, self.id, conclusion, title, summary, text)
        except GitHubError as exc:
            log.warning("could not complete check run: %s", exc)


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
        check_id = run.github_check_run_id
        label = f"{repo.full_name}#{pr.number}@{head_sha[:7]}"

    if installation_id is None:
        await _set(rid, status=RunStatus.failed, error="repository has no GitHub App installation")
        return "failed"

    used = await _tokens_used_today()
    if used >= settings.daily_token_budget:
        log.warning("%s: daily token budget used (%d/%d) — skipping", label, used, settings.daily_token_budget)
        await _set(rid, status=RunStatus.skipped, error="daily token budget reached", finished_at=datetime.now(UTC))
        return "skipped"

    await _set(rid, status=RunStatus.fetching, started_at=datetime.now(UTC), error=None)

    redis = Redis.from_url(settings.redis_url)
    try:
        async with httpx.AsyncClient(base_url=settings.github_api_url, timeout=30) as http:
            gh = GitHubClient(http, GitHubAppAuth.from_settings(http, redis), installation_id)
            checks = Checks(gh, repo.full_name, head_sha, check_id)
            if (new_id := await checks.start()) and new_id != check_id:
                await _set(rid, github_check_run_id=new_id)

            try:
                try:
                    fetched = await fetch_diff(
                        gh, repo.full_name, pr.number, base_sha, head_sha, settings.max_diff_lines
                    )
                except Superseded as exc:
                    log.info("%s: skipped, %s", label, exc)
                    await checks.finish("skipped", "Superseded", f"A newer commit was pushed ({exc}).")
                    await _set(rid, status=RunStatus.skipped, error=str(exc), finished_at=datetime.now(UTC))
                    return "skipped"

                sel = fetched.selection
                log.info(
                    "%s: %d files in diff (%s), %d selected / %d changed lines, %d skipped",
                    label,
                    len(fetched.files),
                    fetched.source,
                    len(sel.files),
                    sel.changed_lines,
                    len(sel.skipped),
                )
                if not sel.files:
                    await checks.finish(
                        "success", "Nothing to review", report.run_footer([], settings.openai_model, sel.skipped)
                    )
                    await _set(
                        rid, status=RunStatus.skipped, error="no reviewable files", finished_at=datetime.now(UTC)
                    )
                    return "skipped"

                await _set(rid, status=RunStatus.reviewing, files_reviewed=len(sel.files))
                ctx = ReviewContext(repo=repo.full_name, number=pr.number, title=pr.title, files=sel.files)
                # Phase 5: ctx.conventions / ctx.frequent_issues come from the Learner's repo_patterns.
                graph = build_review_graph(make_llm(settings))
                state = await graph.ainvoke({"ctx": ctx}, config={"run_name": f"review {label}"})

                findings, runs = state["findings"], state["agent_runs"]
                tin, tout = sum(r.tokens_in for r in runs), sum(r.tokens_out for r in runs)
                log.info(
                    "%s: %d findings after merge %s, verdict %s, %d+%d tokens",
                    label,
                    len(findings),
                    state["merge_stats"],
                    state["verdict"],
                    tin,
                    tout,
                )
                if all(r.error for r in runs if r.agent != "summary"):
                    raise RuntimeError("every review agent failed: " + "; ".join(f"{r.agent}: {r.error}" for r in runs))

                await _save(rid, findings, state, tin, tout, settings.openai_model)

                await checks.finish(
                    report.check_conclusion(findings),
                    report.headline(findings),
                    f"**{report.VERDICT[state['verdict']]}**\n\n{state['summary']}\n\n"
                    f"{report.findings_table(findings)}\n\n"
                    + report.run_footer(runs, settings.openai_model, sel.skipped),
                    report.details(findings),
                )
                celery_app.send_task(TASK_POST_REVIEW, kwargs={"run_id": run_id})  # reviewer posts it to the PR
                return "saved"
            except GitHubRetryable:
                raise  # Celery retries the task; the retry reuses this check run
            except Exception as exc:
                await checks.finish("neutral", "Review failed", f"The automated review crashed: `{type(exc).__name__}`")
                raise
    finally:
        await redis.aclose()


async def _save(rid: uuid.UUID, findings: list, state: dict, tin: int, tout: int, model: str) -> None:
    async with session_scope() as session:
        await session.execute(delete(FindingRow).where(FindingRow.run_id == rid))  # re-reviews start clean
        session.add_all(
            FindingRow(
                run_id=rid,
                agents=f.agents,
                severity=f.severity,
                category=f.category,
                owasp_id=f.owasp_id,
                file=f.file,
                line=f.line,
                title=f.title,
                body=f.explanation,
                suggestion=f.suggestion,
                confidence=f.confidence,
                fingerprint=f.fingerprint,
            )
            for f in findings
        )
        await session.execute(
            update(ReviewRun)
            .where(ReviewRun.id == rid)
            .values(
                status=RunStatus.saved,
                summary=state["summary"],
                verdict=state["verdict"],
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=cost_usd(model, tin, tout),
                finished_at=datetime.now(UTC),
                error=None,
            )
        )


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
        return aio.run(review(run_id))
    except GitHubRetryable:
        if self.request.retries >= self.max_retries:
            aio.run(_fail(run_id, "GitHub kept failing (rate limit / outage)"))
        raise
    except Exception as exc:
        aio.run(_fail(run_id, f"{type(exc).__name__}: {exc}"))
        raise


async def _fail(run_id: str, error: str) -> None:
    await _set(uuid.UUID(run_id), status=RunStatus.failed, error=error[:2000], finished_at=datetime.now(UTC))


logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per OpenAI/GitHub request is too noisy

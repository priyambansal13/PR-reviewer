"""Inspect and re-run reviews.

uv run python scripts/runs.py list                         # recent review runs
uv run python scripts/runs.py show owner/repo#12           # latest run of a PR (or pass a run id)
uv run python scripts/runs.py rereview owner/repo#12       # reset the latest run of a PR and queue it again
"""

import asyncio
import re
import sys
import uuid
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.celery_app import TASK_LEARN_FROM_PR, TASK_POST_REVIEW, TASK_REVIEW_PR, celery_app  # noqa: E402
from common.db.models import Finding, PullRequest, RepoPattern, Repository, ReviewRun, RunStatus  # noqa: E402
from common.db.session import dispose_engine, session_scope  # noqa: E402
from common.patterns import rank, score  # noqa: E402

ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


async def _find_run(session, ref: str) -> ReviewRun:
    q = select(ReviewRun).options(
        selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository), selectinload(ReviewRun.findings)
    )
    if m := re.fullmatch(r"(.+/.+)#(\d+)", ref):
        q = (
            q.join(PullRequest)
            .join(Repository)
            .where(Repository.full_name == m[1], PullRequest.number == int(m[2]))
            .order_by(ReviewRun.created_at.desc())
            .limit(1)
        )
    else:
        q = q.where(ReviewRun.id == uuid.UUID(ref))
    run = await session.scalar(q)
    if run is None:
        sys.exit(f"no run found for {ref}")
    return run


async def list_runs() -> None:
    async with session_scope() as s:
        runs = (
            await s.scalars(
                select(ReviewRun)
                .options(
                    selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository),
                    selectinload(ReviewRun.findings),
                )
                .order_by(ReviewRun.created_at.desc())
                .limit(15)
            )
        ).all()
    for r in runs:
        pr = r.pull_request
        print(
            f"{r.created_at:%m-%d %H:%M}  {pr.repository.full_name}#{pr.number:<4} {r.head_sha[:7]}  "
            f"{r.status.value:<9} files={r.files_reviewed:<3} findings={len(r.findings):<3} "
            f"tokens={r.tokens_in + r.tokens_out:<6} ${float(r.cost_usd):.4f}  {r.verdict or ''}  {r.error or ''}"
        )


async def show(ref: str) -> None:
    async with session_scope() as s:
        r = await _find_run(s, ref)
    pr = r.pull_request
    print(f"{pr.repository.full_name}#{pr.number} @ {r.head_sha[:7]} — {pr.title}")
    print(f"status={r.status.value} verdict={r.verdict} tokens={r.tokens_in}+{r.tokens_out} ${float(r.cost_usd):.4f}")
    if r.error:
        print(f"error: {r.error}")
    if r.summary:
        print(f"\n{r.summary}\n")
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    for f in sorted(r.findings, key=lambda f: (order[f.severity.value], f.file, f.line or 0)):
        where = f"{f.file}:{f.line}" if f.line else f.file
        owasp = f" [{f.owasp_id}]" if f.owasp_id else ""
        print(f"{ICON[f.severity.value]} {f.severity.value:<8} {where:<34} {f.title}{owasp}  ({', '.join(f.agents)})")


async def rereview(ref: str) -> None:
    async with session_scope() as s:
        r = await _find_run(s, ref)
        await s.execute(delete(Finding).where(Finding.run_id == r.id))
        await s.execute(
            update(ReviewRun)
            .where(ReviewRun.id == r.id)
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
                github_check_run_id=None,  # a fresh check run on the PR
                github_review_id=None,
            )
        )
        run_id = str(r.id)
    celery_app.send_task(TASK_REVIEW_PR, kwargs={"run_id": run_id})
    print(f"queued run {run_id} again — follow with: ./setup.sh logs orchestrator-worker")


async def repost(ref: str) -> None:
    async with session_scope() as s:
        r = await _find_run(s, ref)
        await s.execute(update(Finding).where(Finding.run_id == r.id).values(posted=False, github_comment_id=None))
        await s.execute(
            update(ReviewRun)
            .where(ReviewRun.id == r.id)
            .values(status=RunStatus.saved, github_review_id=None, error=None)
        )
        run_id = str(r.id)
    celery_app.send_task(TASK_POST_REVIEW, kwargs={"run_id": run_id})
    print(f"posting run {run_id} again — follow with: ./setup.sh logs reviewer-worker")


async def learn(ref: str) -> None:
    async with session_scope() as s:
        r = await _find_run(s, ref)
        pr_id = str(r.pr_id)
    celery_app.send_task(TASK_LEARN_FROM_PR, kwargs={"pr_id": pr_id, "force": True})
    print(f"learner queued for PR {pr_id} — follow with: ./setup.sh logs learner-worker")


async def knowledge(repo: str) -> None:
    async with session_scope() as s:
        patterns = (await s.scalars(select(RepoPattern).join(Repository).where(Repository.full_name == repo))).all()
    for p in sorted(patterns, key=lambda p: (p.kind.value, -score(p))):
        print(f"{p.kind.value:<15} freq={p.frequency:<3} weight={p.weight:.2f} score={score(p):5.2f}  {p.pattern}")
    k = rank(list(patterns))
    print(
        f"\nUsed in prompts: {len(k.conventions)} conventions, {len(k.frequent_issues)} recurring issues;"
        f" suppressed: {k.suppressed_categories or 'none'}"
    )


async def main(argv: list[str]) -> None:
    try:
        match argv:
            case ["list"]:
                await list_runs()
            case ["show", ref]:
                await show(ref)
            case ["rereview", ref]:
                await rereview(ref)
            case ["repost", ref]:
                await repost(ref)
            case ["learn", ref]:
                await learn(ref)
            case ["knowledge", repo]:
                await knowledge(repo)
            case _:
                print(__doc__)
                sys.exit(1)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

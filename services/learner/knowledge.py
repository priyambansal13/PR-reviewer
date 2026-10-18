"""What the Learner knows — used by the learner API and scripts/runs.py."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.models import PullRequest, RepoPattern, Repository
from common.patterns import rank, score


async def repo_knowledge(session: AsyncSession, repo: str) -> dict[str, Any] | None:
    repo_row = await session.scalar(select(Repository).where(Repository.full_name == repo))
    if repo_row is None:
        return None
    patterns = list((await session.scalars(select(RepoPattern).where(RepoPattern.repo_id == repo_row.id))).all())
    used = rank(patterns)
    return {
        "repo": repo,
        "used_in_prompts": {
            "conventions": used.conventions,
            "recurring_issues": used.frequent_issues,
            "suppressed_categories": used.suppressed_categories,
        },
        "patterns": [
            {
                "kind": p.kind.value,
                "key": p.key,
                "pattern": p.pattern,
                "frequency": p.frequency,
                "weight": round(p.weight, 3),
                "score": round(score(p), 3),
                "last_seen": p.last_seen.isoformat(),
            }
            for p in sorted(patterns, key=lambda p: (p.kind.value, -score(p)))
        ],
    }


async def find_pr_id(session: AsyncSession, repo: str, number: int) -> uuid.UUID | None:
    return await session.scalar(
        select(PullRequest.id).join(Repository).where(Repository.full_name == repo, PullRequest.number == number)
    )

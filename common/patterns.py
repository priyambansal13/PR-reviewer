"""What the Learner knows about a repo, ranked for use in review prompts.

Score = frequency × weight × age decay. Weight comes from human feedback on the bot's comments (👍 raise it,
👎 lower it). Decay halves a pattern's score every 90 days, so habits the team dropped fade out.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.models import PatternKind, RepoPattern

HALF_LIFE_DAYS = 90
MAX_CONVENTIONS = 8
MAX_FREQUENT = 5
FREQUENT_MIN_PRS = 2  # an issue must show up in at least this many PRs to be "recurring"
CONVENTION_MIN_PRS = 2  # a convention must be seen in this many merged PRs before it steers reviews
FREQUENT_MIN_WEIGHT = 0.5
SUPPRESS_BELOW_WEIGHT = 0.4  # (1+👍)/(1+👍+👎) < 0.4 means at least two more 👎 than 👍


@dataclass
class RepoKnowledge:
    conventions: list[str] = field(default_factory=list)
    frequent_issues: list[str] = field(default_factory=list)
    suppressed_categories: list[str] = field(default_factory=list)


def feedback_weight(ups: int, downs: int) -> float:
    return (1 + ups) / (1 + ups + downs)


def score(p: RepoPattern, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    age_days = max(0.0, (now - p.last_seen).total_seconds() / 86_400)
    return p.frequency * p.weight * 0.5 ** (age_days / HALF_LIFE_DAYS)


def rank(patterns: list[RepoPattern], now: datetime | None = None) -> RepoKnowledge:
    conventions = sorted(
        (p for p in patterns if p.kind == PatternKind.convention and p.frequency >= CONVENTION_MIN_PRS),
        key=lambda p: -score(p, now),
    )
    issues = [p for p in patterns if p.kind == PatternKind.frequent_issue]
    frequent = sorted(
        (p for p in issues if p.frequency >= FREQUENT_MIN_PRS and p.weight >= FREQUENT_MIN_WEIGHT),
        key=lambda p: -score(p, now),
    )
    return RepoKnowledge(
        conventions=[p.pattern for p in conventions[:MAX_CONVENTIONS]],
        frequent_issues=[p.pattern for p in frequent[:MAX_FREQUENT]],
        suppressed_categories=sorted(p.key for p in issues if p.weight < SUPPRESS_BELOW_WEIGHT),
    )


async def load_knowledge(session: AsyncSession, repo_id: int) -> RepoKnowledge:
    patterns = (await session.scalars(select(RepoPattern).where(RepoPattern.repo_id == repo_id))).all()
    return rank(list(patterns))

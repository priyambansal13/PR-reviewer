import random
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from common.db.models import (
    Finding,
    Installation,
    PatternKind,
    PullRequest,
    RepoPattern,
    Repository,
    ReviewRun,
    RunStatus,
    Severity,
)
from common.db.session import session_scope
from common.github import GitHubAppAuth, GitHubClient
from common.patterns import feedback_weight, load_knowledge, rank, score
from services.learner.learn import ClaimCheck, Convention, ConventionsReport, VerificationReport, learn_from_pr
from services.orchestrator.graph.merge import merge_findings
from services.orchestrator.graph.prompts import user_prompt
from tests.test_github import keypair  # noqa: F401  (fixture)
from tests.test_review_graph import FILES, af

API = "https://api.github.com"
NOW = datetime(2026, 10, 18, tzinfo=UTC)


def pat(kind, key, freq=1, weight=1.0, age_days=0):
    return RepoPattern(
        kind=kind,
        key=key,
        pattern=f"pattern {key}",
        frequency=freq,
        weight=weight,
        last_seen=NOW - timedelta(days=age_days),
    )


# --- ranking -----------------------------------------------------------------------------------------------------


def test_feedback_weight():
    assert feedback_weight(0, 0) == 1.0
    assert feedback_weight(0, 1) == 0.5
    assert feedback_weight(0, 2) < 0.4  # suppressed after two 👎 with no 👍
    assert feedback_weight(3, 1) == 0.8


def test_decay_halves_every_90_days():
    assert score(pat(PatternKind.convention, "a", freq=4), NOW) == 4
    assert score(pat(PatternKind.convention, "a", freq=4, age_days=90), NOW) == pytest.approx(2)


def test_rank_thresholds_and_suppression():
    k = rank(
        [
            pat(PatternKind.convention, "old", freq=5, age_days=400),
            pat(PatternKind.convention, "fresh", freq=2),
            pat(PatternKind.convention, "seen-once", freq=1),  # not trusted yet
            pat(PatternKind.frequent_issue, "sql_injection", freq=3),
            pat(PatternKind.frequent_issue, "one_off", freq=1),  # needs >= 2 PRs
            pat(PatternKind.frequent_issue, "naming", freq=4, weight=0.25),  # team keeps 👎-ing it
        ],
        NOW,
    )
    assert k.conventions == ["pattern fresh", "pattern old"]  # 5 × 0.5^(400/90) ≈ 0.23 < 2
    assert k.frequent_issues == ["pattern sql_injection"]
    assert k.suppressed_categories == ["naming"]


def test_suppressed_categories_are_dropped_unless_severe():
    raw = [
        ("style", af(3, "Rename variable", severity="low", category="naming")),
        ("security", af(5, "SQL injection", severity="critical", category="naming")),  # severe: kept anyway
        ("static", af(4, "Connection leak", severity="medium", category="resource_leak")),
    ]
    merged, stats = merge_findings(raw, FILES, {"naming"})
    assert {f.title for f in merged} == {"SQL injection", "Connection leak"}
    assert stats["suppressed"] == 1


def test_prompt_carries_learned_knowledge():
    p = user_prompt("o/r", 1, "t", "DIFF", ["Use `with` for DB connections"], ["sql injection (e.g. ...)"], ["naming"])
    assert "learned from its merged PRs" in p and "Use `with` for DB connections" in p
    assert "keep recurring" in p and "unhelpful (👎): naming" in p


# --- learning cycle (real Postgres, mocked GitHub + LLM) ----------------------------------------------------------


class FakeLLM:
    """Proposes `conventions`; the fact-checker rejects any whose text is in `false_claims`."""

    def __init__(self, conventions, false_claims=()):
        self.conventions, self.false_claims, self.prompts = conventions, set(false_claims), []

    def with_structured_output(self, schema, include_raw=False):
        llm = self

        class Bound:
            async def ainvoke(self, messages, config=None):
                llm.prompts.append(messages[1].content)
                raw = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
                if schema is VerificationReport:
                    lines = [ln for ln in messages[1].content.splitlines() if ln[:1].isdigit() and ". " in ln]
                    parsed = VerificationReport(
                        checks=[
                            ClaimCheck(
                                index=int(ln.split(".")[0]),
                                holds=ln.split(". ", 1)[1] not in llm.false_claims,
                                reason="checked",
                            )
                            for ln in lines
                        ]
                    )
                else:
                    parsed = ConventionsReport(conventions=llm.conventions)
                return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return Bound()


DIFF = (
    "diff --git a/app/svc.py b/app/svc.py\n--- a/app/svc.py\n+++ b/app/svc.py\n"
    "@@ -1 +1,3 @@\n import x\n+def get(conn) -> int:\n+    return 1\n"
)


@pytest.fixture
async def merged_pr(engine):
    inst, repo_id = random.randint(10**8, 10**9), random.randint(10**8, 10**9)
    repo = f"octo/learn-{repo_id}"
    async with session_scope() as s:
        s.add(Installation(id=inst, account_login="octo"))
        s.add(Repository(id=repo_id, installation_id=inst, full_name=repo))
        await s.flush()
        prs = [PullRequest(repo_id=repo_id, number=n, title=f"PR {n}", merged=n == 2) for n in (1, 2)]
        s.add_all(prs)
        await s.flush()
        for pr in prs:  # the same two categories show up in both PRs -> "recurring"
            # one transaction = one now(); give PR 2's findings a later time so "most recent example" is defined
            created = datetime.now(UTC) - timedelta(hours=3 - pr.number)
            run = ReviewRun(pr_id=pr.id, head_sha=f"{pr.number}" * 40, status=RunStatus.posted)
            run.findings = [
                Finding(
                    agents=["security"],
                    severity=Severity.high,
                    category="sql_injection",
                    file="a.py",
                    line=1,
                    title=f"SQL injection #{pr.number}",
                    body="b",
                    fingerprint="x",
                    github_comment_id=1000 + pr.number,
                    created_at=created,
                ),
                Finding(
                    agents=["style"],
                    severity=Severity.low,
                    category="naming",
                    file="a.py",
                    line=2,
                    title="Rename x",
                    body="b",
                    fingerprint="y",
                    github_comment_id=2000 + pr.number,
                    created_at=created,
                ),
            ]
            s.add(run)
        await s.flush()
        pr2_id = prs[1].id
    yield SimpleNamespace(repo=repo, repo_id=repo_id, inst=inst, pr_id=pr2_id)
    async with session_scope() as s:
        await s.execute(delete(Repository).where(Repository.id == repo_id))
        await s.execute(delete(Installation).where(Installation.id == inst))


def _reactions(*contents, bot=False):
    return [{"content": c, "user": {"type": "Bot" if bot else "User"}} for c in contents]


@respx.mock
async def test_learn_from_merged_pr(keypair, merged_pr):  # noqa: F811
    r = merged_pr
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    respx.post(f"{API}/app/installations/{r.inst}/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t", "expires_at": expires})
    )
    respx.get(f"{API}/repos/{r.repo}/pulls/comments/1002/reactions").mock(
        return_value=httpx.Response(200, json=_reactions("+1", "+1", "-1", bot=False) + _reactions("-1", bot=True))
    )
    respx.get(f"{API}/repos/{r.repo}/pulls/comments/2002/reactions").mock(
        return_value=httpx.Response(200, json=_reactions("-1", "-1"))
    )
    respx.get(f"{API}/repos/{r.repo}/pulls/2").mock(
        return_value=httpx.Response(200, json={"base": {"sha": "b" * 40}, "head": {"sha": "h" * 40}})
    )
    respx.get(f"{API}/repos/{r.repo}/compare/{'b' * 40}...{'h' * 40}").mock(return_value=httpx.Response(200, text=DIFF))

    llm = FakeLLM(
        [
            Convention(
                key="Type Hints On Public Functions",
                text="Public functions have return type hints",
                evidence=["app/svc.py:2", "b/app/svc.py:3"],
            ),
            # invented evidence: line 1 is a context line, line 9 doesn't exist -> discarded
            Convention(
                key="docstrings", text="All functions have docstrings", evidence=["app/svc.py:1", "app/svc.py:9"]
            ),
        ]
    )
    async with httpx.AsyncClient(base_url=API) as http:
        gh = GitHubClient(http, GitHubAppAuth("1", keypair[0], http), r.inst)
        async with session_scope() as s:
            result = await learn_from_pr(s, r.pr_id, gh, llm)
        async with session_scope() as s:
            again = await learn_from_pr(s, r.pr_id, gh, llm)

    assert (result.thumbs_up, result.thumbs_down) == (2, 3)  # the bot's own 👎 is ignored
    assert result.conventions == 1 and result.frequent_issue_categories == 2
    assert again.skipped and again.skipped.startswith("already learned")
    assert "SQL injection #2" in llm.prompts[0]  # flagged code is listed so it isn't learned as a convention

    async with session_scope() as s:
        k = await load_knowledge(s, r.repo_id)
        patterns = {p.key: p for p in (await s.scalars(select(RepoPattern).where(RepoPattern.repo_id == r.repo_id)))}
    assert patterns["type-hints-on-public-functions"].pattern == "Public functions have return type hints"
    assert patterns["sql_injection"].frequency == 2  # appeared in both PRs
    assert patterns["sql_injection"].weight == pytest.approx(feedback_weight(2, 1))
    assert patterns["naming"].weight == pytest.approx(feedback_weight(0, 2))
    assert k.conventions == []  # seen in one merged PR only: stored, but not trusted yet
    assert patterns["type-hints-on-public-functions"].frequency == 1
    assert k.frequent_issues == ['sql injection (e.g. "SQL injection #2")']
    assert k.suppressed_categories == ["naming"]

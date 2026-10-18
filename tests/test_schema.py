import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.db.models import (
    Finding,
    Installation,
    PullRequest,
    Repository,
    ReviewRun,
    RunStatus,
    Severity,
)


async def _make_pr(db) -> PullRequest:
    inst = Installation(id=1, account_login="octo")
    repo = Repository(id=100, installation=inst, full_name="octo/demo")
    pr = PullRequest(repository=repo, number=7, title="Add login", author="dev")
    db.add_all([inst, repo, pr])
    await db.flush()
    return pr


async def test_review_run_with_findings_round_trip(db):
    pr = await _make_pr(db)
    run = ReviewRun(pull_request=pr, head_sha="a" * 40)
    run.findings.append(
        Finding(
            agents=["security", "static"],
            severity=Severity.high,
            category="injection",
            owasp_id="A03",
            file="app/db.py",
            line=42,
            title="SQL built with f-string",
            body="User input reaches the query unescaped.",
            fingerprint="f1",
        )
    )
    db.add(run)
    await db.flush()

    loaded = (await db.execute(select(ReviewRun).where(ReviewRun.id == run.id))).scalar_one()
    await db.refresh(loaded, ["findings"])
    assert loaded.status is RunStatus.queued  # server default
    assert loaded.findings[0].agents == ["security", "static"]
    assert loaded.findings[0].severity is Severity.high


async def test_same_head_sha_is_rejected(db):
    """UNIQUE(pr_id, head_sha) is the de-duplication guarantee the webhook service relies on."""
    pr = await _make_pr(db)
    db.add(ReviewRun(pull_request=pr, head_sha="b" * 40))
    await db.flush()
    db.add(ReviewRun(pull_request=pr, head_sha="b" * 40))
    with pytest.raises(IntegrityError):
        await db.flush()

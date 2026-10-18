import json
import random
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, select

from common.config import get_settings
from common.db.models import Finding, Installation, PullRequest, Repository, ReviewRun, RunStatus, Severity
from common.db.session import session_scope
from common.diff import parse_diff
from common.github import client as client_mod
from services.reviewer.compose import MARKER, build_review
from services.reviewer.worker import post

DIFF = """\
diff --git a/app/users.py b/app/users.py
new file mode 100644
--- /dev/null
+++ b/app/users.py
@@ -0,0 +1,5 @@
+import sqlite3
+
+def find_user(name):
+    conn = sqlite3.connect("u.db")
+    return conn.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchone()
"""
FILES = {f.path: f for f in parse_diff(DIFF)}


def fnd(line, severity="high", suggestion=None, title="SQL injection", owasp="A03", file="app/users.py"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agents=["security"],
        severity=severity,
        category="sql_injection",
        owasp_id=owasp,
        file=file,
        line=line,
        title=title,
        body="User input reaches the query.",
        suggestion=suggestion,
        confidence=0.9,
    )


# --- composing ---------------------------------------------------------------------------------------------------


def test_inline_comments_body_and_marker():
    p = build_review(
        "run-1",
        [fnd(5), fnd(None, "low", title="No tests", owasp=None)],
        FILES,
        "Adds lookup.",
        "request_changes",
        max_inline=25,
    )
    assert p.event == "REQUEST_CHANGES"
    (c,) = p.comments
    assert (c["path"], c["line"], c["side"]) == ("app/users.py", 5, "RIGHT")
    assert c["body"].startswith("**🟠 High** · SQL injection · `A03 Injection`")
    assert "⛔ Changes requested" in p.body and "No tests" in p.body  # file-level finding goes to the body
    assert p.body.endswith(MARKER.format("run-1"))


def test_bot_never_approves():
    assert build_review("r", [], FILES, "Fine.", "approve", 25).event == "COMMENT"
    assert build_review("r", [fnd(5, "low")], FILES, "s", "comment", 25).event == "COMMENT"


def test_single_line_suggestion_keeps_indentation():
    fix = 'return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()'
    body = build_review("r", [fnd(5, suggestion=fix)], FILES, "s", "comment", 25).comments[0]["body"]
    assert f"```suggestion\n    {fix}\n```" in body  # 4-space indent copied from the original line


def test_multi_line_suggestion_is_plain_code_not_one_click():
    fix = "with sqlite3.connect('u.db') as conn:\n    return conn.execute(q, (name,)).fetchone()"
    body = build_review("r", [fnd(4, suggestion=fix)], FILES, "s", "comment", 25).comments[0]["body"]
    assert "```suggestion" not in body and "Suggested fix:\n```python" in body


def test_suggestion_equal_to_original_is_dropped():
    same = """return conn.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchone()"""
    body = build_review("r", [fnd(5, suggestion=same)], FILES, "s", "comment", 25).comments[0]["body"]
    assert "```" not in body


def test_inline_cap_and_non_commentable_lines_overflow_to_body():
    findings = [
        fnd(5, "critical"),
        fnd(4, "medium", title="Leak"),
        fnd(3, "low", title="Docstring"),
        fnd(42, "high", title="Line not in diff"),
    ]
    p = build_review("r", findings, FILES, "s", "request_changes", max_inline=2)
    assert [c["line"] for c in p.comments] == [5, 4]  # most severe first
    assert "2 more findings" in p.body and "Docstring" in p.body and "Line not in diff" in p.body


# --- posting (real Postgres, mocked GitHub) ------------------------------------------------------------------------

API = "https://api.github.com"


@pytest.fixture
def github_app(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "app.pem"
    pem.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    s = get_settings()
    monkeypatch.setattr(s, "github_app_id", "1")
    monkeypatch.setattr(s, "github_private_key_path", pem)
    monkeypatch.setattr(s, "redis_url", "redis://localhost:6380/15")
    monkeypatch.setattr(s, "github_api_url", API)

    async def instant(_):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", instant)


@pytest.fixture
async def saved_run(engine):
    """A committed repo/PR/run with two findings; deleted afterwards."""
    inst_id, repo_id = random.randint(10**8, 10**9), random.randint(10**8, 10**9)
    repo = f"octo/test-{repo_id}"
    async with session_scope() as s:
        s.add(Installation(id=inst_id, account_login="octo"))
        s.add(Repository(id=repo_id, installation_id=inst_id, full_name=repo))
        await s.flush()
        pr = PullRequest(repo_id=repo_id, number=7, title="Add lookup", author="dev")
        s.add(pr)
        await s.flush()
        run = ReviewRun(
            pr_id=pr.id,
            head_sha="h" * 40,
            base_sha="b" * 40,
            status=RunStatus.saved,
            summary="Adds a lookup.",
            verdict="request_changes",
            tokens_in=100,
            tokens_out=20,
        )
        run.findings = [
            Finding(
                agents=["security"],
                severity=Severity.critical,
                category="sql_injection",
                owasp_id="A03",
                file="app/users.py",
                line=5,
                title="SQL injection",
                body="b",
                fingerprint="f1",
            ),
            Finding(
                agents=["static"],
                severity=Severity.medium,
                category="resource_leak",
                file="app/users.py",
                line=4,
                title="Connection never closed",
                body="b",
                fingerprint="f2",
            ),
        ]
        s.add(run)
        await s.flush()
        run_id = str(run.id)
    yield SimpleNamespace(run_id=run_id, repo=repo, inst=inst_id)
    async with session_scope() as s:
        await s.execute(delete(Repository).where(Repository.id == repo_id))
        await s.execute(delete(Installation).where(Installation.id == inst_id))


def mock_github(mock, repo, inst, *, head="h" * 40, state="open", reviews=None, create=None):
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    mock.post(f"{API}/app/installations/{inst}/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t", "expires_at": expires})
    )
    mock.get(f"{API}/repos/{repo}/pulls/7").mock(
        return_value=httpx.Response(200, json={"head": {"sha": head}, "state": state})
    )
    mock.get(f"{API}/repos/{repo}/pulls/7/reviews").mock(return_value=httpx.Response(200, json=reviews or []))
    mock.get(f"{API}/repos/{repo}/compare/{'b' * 40}...{'h' * 40}").mock(return_value=httpx.Response(200, text=DIFF))
    created = mock.post(f"{API}/repos/{repo}/pulls/7/reviews").mock(
        side_effect=create or [httpx.Response(200, json={"id": 900, "user": {"id": 42}})]
    )
    mock.get(f"{API}/repos/{repo}/pulls/7/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 2, "path": "app/users.py", "line": 4, "pull_request_review_id": 900},
                {"id": 1, "path": "app/users.py", "line": 5, "pull_request_review_id": 900},
                {"id": 3, "path": "app/users.py", "line": 5, "pull_request_review_id": 111},
            ],
        )
    )
    dismiss = mock.put(url__regex=rf"{API}/repos/{repo}/pulls/7/reviews/\d+/dismissals").mock(
        return_value=httpx.Response(200, json={})
    )
    return created, dismiss


async def _run_and_findings(run_id):
    async with session_scope() as s:
        run = await s.get(ReviewRun, uuid.UUID(run_id))
        findings = (await s.scalars(select(Finding).where(Finding.run_id == run.id))).all()
        return run, {f.line: f for f in findings}


@respx.mock
async def test_posts_one_review_and_links_comments(github_app, saved_run):
    created, dismiss = mock_github(
        respx,
        saved_run.repo,
        saved_run.inst,
        reviews=[{"id": 800, "user": {"id": 42}, "state": "CHANGES_REQUESTED", "body": "old review"}],
    )
    assert await post(saved_run.run_id) == "posted"

    sent = json.loads(created.calls[0].request.content)
    assert sent["commit_id"] == "h" * 40 and sent["event"] == "REQUEST_CHANGES"
    assert [c["line"] for c in sent["comments"]] == [5, 4]  # critical first
    run, findings = await _run_and_findings(saved_run.run_id)
    assert (run.status, run.github_review_id) == (RunStatus.posted, 900)
    assert (findings[5].github_comment_id, findings[4].github_comment_id) == (1, 2)  # matched by path+line
    assert dismiss.call_count == 1  # our previous CHANGES_REQUESTED review no longer blocks the PR

    assert await post(saved_run.run_id) == "already posted"
    assert created.call_count == 1
    assert MARKER.format(f"{saved_run.run_id}:0") in sent["body"]


@respx.mock
async def test_marker_on_github_prevents_double_post(github_app, saved_run):
    created, _ = mock_github(
        respx,
        saved_run.repo,
        saved_run.inst,
        reviews=[
            {"id": 900, "user": {"id": 42}, "state": "COMMENTED", "body": "x " + MARKER.format(f"{saved_run.run_id}:0")}
        ],
    )
    assert await post(saved_run.run_id) == "already posted"
    assert created.call_count == 0
    run, findings = await _run_and_findings(saved_run.run_id)
    assert run.github_review_id == 900
    assert findings[5].github_comment_id == 1  # comments linked even on the recovery path


@respx.mock
async def test_rejected_inline_position_falls_back_to_body_only(github_app, saved_run):
    created, _ = mock_github(
        respx,
        saved_run.repo,
        saved_run.inst,
        create=[
            httpx.Response(422, json={"message": "Line could not be resolved"}),
            httpx.Response(200, json={"id": 900, "user": {"id": 42}}),
        ],
    )
    assert await post(saved_run.run_id) == "posted"
    retry = json.loads(created.calls[1].request.content)
    assert retry["comments"] == [] and "SQL injection" in retry["body"]


@respx.mock
async def test_superseded_or_closed_pr_is_not_posted(github_app, saved_run):
    created, _ = mock_github(respx, saved_run.repo, saved_run.inst, head="n" * 40)
    assert await post(saved_run.run_id) == "skipped"
    assert created.call_count == 0
    run, _ = await _run_and_findings(saved_run.run_id)
    assert run.status == RunStatus.skipped and "head moved" in run.error


@respx.mock
async def test_own_pr_falls_back_to_comment_and_keeps_inline(github_app, saved_run):
    created, _ = mock_github(
        respx,
        saved_run.repo,
        saved_run.inst,
        create=[
            httpx.Response(422, json={"errors": ["Review Can not request changes on your own pull request"]}),
            httpx.Response(200, json={"id": 900, "user": {"id": 42}}),
        ],
    )
    assert await post(saved_run.run_id) == "posted"
    retry = json.loads(created.calls[1].request.content)
    assert retry["event"] == "COMMENT" and len(retry["comments"]) == 2

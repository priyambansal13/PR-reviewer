import random
from datetime import UTC, datetime

import httpx
import pytest

from common.api import get_enqueuer, get_session
from common.celery_app import TASK_LEARN_FROM_PR, TASK_POST_REVIEW, TASK_REVIEW_PR
from common.config import get_settings
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
from services.gateway.app import app as gateway_app
from services.learner.app import app as learner_app
from services.orchestrator.app import app as orchestrator_app
from services.reviewer.app import app as reviewer_app

APPS = (orchestrator_app, reviewer_app, learner_app)
TOKEN = "internal-test-token"


class FakeQueue:
    def __init__(self):
        self.sent = []

    def send(self, task, kwargs):
        self.sent.append((task, kwargs))


@pytest.fixture
def queue(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_token", TOKEN)
    q = FakeQueue()

    async def session():
        yield db

    for app in APPS:
        app.dependency_overrides[get_session] = session
        app.dependency_overrides[get_enqueuer] = lambda: q
    yield q
    for app in APPS:
        app.dependency_overrides.clear()


@pytest.fixture
async def data(db):
    inst, repo_id = random.randint(10**8, 10**9), random.randint(10**8, 10**9)
    repo = f"octo/api-{repo_id}"
    db.add(Installation(id=inst, account_login="octo"))
    db.add(Repository(id=repo_id, installation_id=inst, full_name=repo))
    await db.flush()
    pr = PullRequest(repo_id=repo_id, number=9, title="Add login")
    db.add(pr)
    await db.flush()
    done = ReviewRun(
        pr_id=pr.id,
        head_sha="a" * 40,
        status=RunStatus.posted,
        verdict="request_changes",
        finished_at=datetime.now(UTC),
        tokens_in=100,
        tokens_out=20,
        github_review_id=55,
    )
    done.findings = [
        Finding(
            agents=["security"],
            severity=Severity.low,
            category="naming",
            file="a.py",
            line=3,
            title="Rename",
            body="b",
            fingerprint="f2",
        ),
        Finding(
            agents=["security"],
            severity=Severity.critical,
            category="sql_injection",
            owasp_id="A03",
            file="a.py",
            line=5,
            title="SQL injection",
            body="b",
            fingerprint="f1",
        ),
    ]
    busy = ReviewRun(pr_id=pr.id, head_sha="b" * 40, status=RunStatus.reviewing)
    db.add_all([done, busy])
    db.add(
        RepoPattern(
            repo_id=repo_id, kind=PatternKind.frequent_issue, key="sql_injection", pattern="sql injection", frequency=3
        )
    )
    await db.flush()
    return {"repo": repo, "done": str(done.id), "busy": str(busy.id), "pr_id": str(pr.id)}


def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://svc", headers={"x-internal-token": TOKEN}
    )


async def test_internal_token_required_but_probes_open(queue):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=orchestrator_app), base_url="http://svc") as c:
        assert (await c.get("/reviews")).status_code == 403
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/metrics")).status_code == 200


async def test_orchestrator_lists_and_shows_runs(queue, data):
    async with client(orchestrator_app) as c:
        runs = (await c.get("/reviews", params={"repo": data["repo"]})).json()
        assert {r["status"] for r in runs} == {"posted", "reviewing"}
        done = next(r for r in runs if r["id"] == data["done"])
        assert (done["pr"], done["findings"], done["tokens"]) == (9, 2, 120)

        detail = (await c.get(f"/reviews/{data['done']}")).json()
        assert [f["severity"] for f in detail["findings"]] == ["critical", "low"]  # most severe first
        assert detail["findings"][0]["owasp_id"] == "A03"
        assert (await c.get(f"/reviews/{'0' * 8}-0000-0000-0000-{'0' * 12}")).status_code == 404


async def test_orchestrator_rerun(queue, data, db):
    async with client(orchestrator_app) as c:
        r = await c.post(f"/reviews/{data['done']}/rerun")
        assert r.status_code == 202
        assert queue.sent == [(TASK_REVIEW_PR, {"run_id": data["done"]})]
        detail = (await c.get(f"/reviews/{data['done']}")).json()
        assert (detail["status"], detail["findings"], detail["github_review_id"]) == ("queued", [], None)

        busy = await c.post(f"/reviews/{data['busy']}/rerun")  # a worker is on it right now
        assert busy.status_code == 409 and "reviewing" in busy.json()["detail"]
        assert len(queue.sent) == 1


async def test_reviewer_post_and_status(queue, data):
    async with client(reviewer_app) as c:
        status = (await c.get(f"/reviews/{data['done']}/posting")).json()
        assert (status["status"], status["github_review_id"]) == ("posted", 55)

        assert (await c.post(f"/reviews/{data['done']}/post")).status_code == 202
        assert queue.sent == [(TASK_POST_REVIEW, {"run_id": data["done"]})]
        assert (await c.get(f"/reviews/{data['done']}/posting")).json()["status"] == "saved"

        never_reviewed = await c.post(f"/reviews/{data['busy']}/post")
        assert never_reviewed.status_code == 409


async def test_learner_knowledge_and_learn(queue, data):
    owner, name = data["repo"].split("/")
    async with client(learner_app) as c:
        k = (await c.get(f"/repos/{owner}/{name}/knowledge")).json()
        assert k["used_in_prompts"]["recurring_issues"] == ["sql injection"]
        assert k["patterns"][0]["frequency"] == 3

        assert (await c.post(f"/repos/{owner}/{name}/pulls/9/learn")).status_code == 202
        assert queue.sent == [(TASK_LEARN_FROM_PR, {"pr_id": data["pr_id"], "force": True})]
        assert (await c.post(f"/repos/{owner}/{name}/pulls/999/learn")).status_code == 404
        assert (await c.get("/repos/nobody/nothing/knowledge")).status_code == 404


# --- gateway admin proxy -----------------------------------------------------------------------------------------


@pytest.fixture
def upstream(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "admin_api_token", "admin-secret")
    monkeypatch.setattr(s, "internal_token", TOKEN)
    monkeypatch.setattr(s, "orchestrator_service_url", "http://orchestrator:8002")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    gateway_app.state.internal = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


async def _gw(path, method="GET", token="admin-secret"):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw") as c:
        return await c.request(method, path, headers=headers)


async def test_gateway_proxies_admin_api_with_internal_token(upstream):
    r = await _gw("/api/orchestrator/reviews?repo=o/r&limit=5")
    assert r.status_code == 200
    (req,) = upstream
    assert str(req.url) == "http://orchestrator:8002/reviews?repo=o%2Fr&limit=5"
    assert req.headers["x-internal-token"] == TOKEN


async def test_gateway_admin_api_auth(upstream, monkeypatch):
    assert (await _gw("/api/orchestrator/reviews", token=None)).status_code == 401
    assert (await _gw("/api/orchestrator/reviews", token="wrong")).status_code == 401
    assert (await _gw("/api/database/drop", token="admin-secret")).status_code == 404
    monkeypatch.setattr(get_settings(), "admin_api_token", "")
    assert (await _gw("/api/orchestrator/reviews")).status_code == 503  # disabled without a token
    assert upstream == []

import json

import httpx
import pytest
from sqlalchemy import select

from common.celery_app import TASK_LEARN_FROM_PR, TASK_REVIEW_PR
from common.config import get_settings
from common.db.models import PullRequest, Repository, ReviewRun, WebhookDelivery
from services.gateway.app import app as gateway_app
from services.gateway.signature import sign
from services.webhook.app import app as webhook_app
from services.webhook.app import get_enqueuer, get_session
from tests.fixtures.events import delivery_id, installation_event, pr_event


class FakeEnqueuer:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    def send(self, task_name, kwargs):
        if self.fail:
            raise ConnectionError("redis down")
        self.sent.append((task_name, kwargs))


@pytest.fixture
def queue(db):
    q = FakeEnqueuer()

    async def _session():
        yield db

    webhook_app.dependency_overrides[get_session] = _session
    webhook_app.dependency_overrides[get_enqueuer] = lambda: q
    yield q
    webhook_app.dependency_overrides.clear()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webhook_app), base_url="http://wh") as c:
        yield c


async def send(client, payload: dict, event: str = "pull_request", delivery: str | None = None):
    return await client.post(
        "/events",
        content=json.dumps(payload),
        headers={
            "x-github-event": event,
            "x-github-delivery": delivery or delivery_id(),
            "x-internal-token": get_settings().internal_token,
        },
    )


async def test_opened_pr_is_stored_and_queued(client, queue, db):
    resp = await send(client, pr_event("opened"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "queued"

    run = await db.scalar(select(ReviewRun).where(ReviewRun.id == body["run_id"]))
    assert run.head_sha == "a" * 40
    pr = await db.get(PullRequest, run.pr_id)
    assert (pr.number, pr.title, pr.author) == (7, "Add login endpoint", "dev")
    repo = await db.get(Repository, 100)
    assert (repo.full_name, repo.installation_id) == ("octo/demo", 555)
    assert queue.sent == [(TASK_REVIEW_PR, {"run_id": body["run_id"]})]


async def test_same_delivery_twice_is_duplicate(client, queue):
    d = delivery_id()
    assert (await send(client, pr_event(), delivery=d)).json()["result"] == "queued"
    assert (await send(client, pr_event(), delivery=d)).json()["result"] == "duplicate_delivery"
    assert len(queue.sent) == 1


async def test_same_head_sha_from_new_delivery_is_duplicate(client, queue):
    assert (await send(client, pr_event("opened"))).json()["result"] == "queued"
    resp = await send(client, pr_event("reopened"))
    assert resp.json()["result"] == "duplicate_sha"
    assert len(queue.sent) == 1


async def test_new_commit_queues_a_new_run(client, queue):
    await send(client, pr_event("opened"))
    resp = await send(client, pr_event("synchronize", head={"ref": "feature/login", "sha": "c" * 40}))
    assert resp.json()["result"] == "queued"
    assert len(queue.sent) == 2


async def test_draft_is_ignored_until_ready(client, queue):
    resp = await send(client, pr_event("opened", draft=True))
    assert resp.json() == {"result": "ignored", "reason": "draft PR"}
    resp = await send(client, pr_event("ready_for_review", draft=False))
    assert resp.json()["result"] == "queued"


async def test_merged_pr_queues_learner(client, queue, db):
    await send(client, pr_event("opened"))
    resp = await send(client, pr_event("closed", state="closed", merged=True, merged_at="2026-10-18T10:00:00Z"))
    assert resp.json()["result"] == "learn_queued"
    assert queue.sent[-1][0] == TASK_LEARN_FROM_PR
    pr = await db.scalar(select(PullRequest).where(PullRequest.repo_id == 100, PullRequest.number == 7))
    await db.refresh(pr)
    assert pr.merged and pr.state == "closed"


async def test_closed_without_merge_is_ignored(client, queue):
    resp = await send(client, pr_event("closed", state="closed", merged=False))
    assert resp.json()["result"] == "ignored"
    assert queue.sent == []


async def test_disabled_repo_is_ignored(client, queue, db):
    await send(client, installation_event("created"), event="installation")
    await send(client, installation_event("deleted"), event="installation")
    repo = await db.get(Repository, 100)
    await db.refresh(repo)
    assert repo.enabled is False
    resp = await send(client, pr_event("opened"))
    assert resp.json() == {"result": "ignored", "reason": "repository disabled"}


async def test_installation_syncs_repositories(client, queue, db):
    resp = await send(client, installation_event("created"), event="installation")
    assert resp.json() == {"result": "synced", "reason": "2 added, 0 removed"}
    assert (await db.get(Repository, 101)).full_name == "octo/api"


async def test_ping_and_unknown_events(client, queue):
    assert (await send(client, {"zen": "hi"}, event="ping")).json()["result"] == "pong"
    assert (await send(client, {"action": "created"}, event="issues")).json()["result"] == "ignored"


async def test_bad_payload_is_422_and_not_recorded(client, queue, db):
    d = delivery_id()
    resp = await send(client, {"action": "opened", "pull_request": {}}, delivery=d)
    assert resp.status_code == 422
    assert await db.get(WebhookDelivery, d) is None


async def test_queue_down_returns_503_and_allows_redelivery(client, queue, db):
    queue.fail = True
    d = delivery_id()
    resp = await send(client, pr_event(), delivery=d)
    assert resp.status_code == 503
    assert await db.get(WebhookDelivery, d) is None
    runs = await db.scalars(select(ReviewRun).join(PullRequest).where(PullRequest.repo_id == 100))
    assert runs.all() == []

    queue.fail = False  # Redis is back; GitHub redelivers the same delivery id
    assert (await send(client, pr_event(), delivery=d)).json()["result"] == "queued"


async def test_internal_token_required(client, queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_token", "secret-tok")
    resp = await client.post(
        "/events", content="{}", headers={"x-github-event": "ping", "x-github-delivery": delivery_id()}
    )
    assert resp.status_code == 403


async def test_gateway_to_webhook_end_to_end(queue, monkeypatch):
    """Signed request -> gateway -> webhook (in-process) -> DB + queue."""
    monkeypatch.setattr(get_settings(), "github_webhook_secret", "s3cret")
    gateway_app.state.http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app), base_url="http://webhook"
    )
    body = json.dumps(pr_event()).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gw") as gw:
        resp = await gw.post(
            "/webhook/github",
            content=body,
            headers={
                "x-github-event": "pull_request",
                "x-github-delivery": delivery_id(),
                "x-hub-signature-256": sign("s3cret", body),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["result"] == "queued"
    assert queue.sent[0][0] == TASK_REVIEW_PR

"""Turns a verified GitHub event into database rows and a list of Celery tasks to send.

Everything here runs inside one DB transaction. Tasks are *returned*, not sent: the caller sends them after
commit, so a worker can never pick up a run id that isn't in the database yet.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.celery_app import TASK_LEARN_FROM_PR, TASK_REVIEW_PR
from common.db.models import Installation, PullRequest, Repository, ReviewRun, WebhookDelivery
from common.github_events import REVIEW_ACTIONS, PullRequestEvent

log = logging.getLogger("webhook")


class InvalidPayload(Exception):
    pass


@dataclass
class Outcome:
    result: str  # queued | learn_queued | duplicate_delivery | duplicate_sha | ignored | pong | synced
    reason: str = ""
    run_id: uuid.UUID | None = None
    tasks: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"result": self.result}
        if self.reason:
            body["reason"] = self.reason
        if self.run_id:
            body["run_id"] = str(self.run_id)
        return body


async def process_event(session: AsyncSession, event: str, delivery_id: str, payload: dict) -> Outcome:
    repo_id = (payload.get("repository") or {}).get("id")
    claimed = await session.scalar(
        insert(WebhookDelivery)
        .values(
            delivery_id=delivery_id,
            event=event,
            action=payload.get("action"),
            repo_id=repo_id,
            result="processing",
        )
        .on_conflict_do_nothing()
        .returning(WebhookDelivery.delivery_id)
    )
    if claimed is None:
        # GitHub redelivered (or retried) a delivery we already handled
        return Outcome("duplicate_delivery")

    match event:
        case "ping":
            outcome = Outcome("pong")
        case "pull_request":
            outcome = await _pull_request(session, payload)
        case "installation" | "installation_repositories":
            outcome = await _installation(session, event, payload)
        case _:
            outcome = Outcome("ignored", f"event {event} not handled")

    await session.execute(
        update(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id).values(result=outcome.result)
    )
    return outcome


async def _pull_request(session: AsyncSession, payload: dict) -> Outcome:
    try:
        ev = PullRequestEvent.model_validate(payload)
    except ValidationError as exc:
        raise InvalidPayload(str(exc)) from exc

    pr, repo = ev.pull_request, ev.repository
    if ev.installation:
        await _upsert_installation(session, ev.installation.id, repo.owner.login)
    enabled = await _upsert_repository(
        session, repo.id, repo.full_name, ev.installation.id if ev.installation else None
    )

    pr_id = await session.scalar(
        insert(PullRequest)
        .values(
            repo_id=repo.id,
            number=pr.number,
            title=pr.title,
            author=pr.user.login,
            base_ref=pr.base.ref,
            head_ref=pr.head.ref,
            state=pr.state,
            draft=pr.draft,
            merged=bool(pr.merged),
            merged_at=pr.merged_at,
        )
        .on_conflict_do_update(
            index_elements=[PullRequest.repo_id, PullRequest.number],
            set_={
                "title": pr.title,
                "base_ref": pr.base.ref,
                "head_ref": pr.head.ref,
                "state": pr.state,
                "draft": pr.draft,
                "merged": bool(pr.merged),
                "merged_at": pr.merged_at,
                "updated_at": func.now(),
            },
        )
        .returning(PullRequest.id)
    )

    if not enabled:
        return Outcome("ignored", "repository disabled")

    if ev.action in REVIEW_ACTIONS:
        if pr.draft:
            return Outcome("ignored", "draft PR")
        run_id = await session.scalar(
            insert(ReviewRun)
            .values(pr_id=pr_id, head_sha=pr.head.sha, base_sha=pr.base.sha)
            .on_conflict_do_nothing(index_elements=[ReviewRun.pr_id, ReviewRun.head_sha])
            .returning(ReviewRun.id)
        )
        if run_id is None:
            return Outcome("duplicate_sha", f"{pr.head.sha[:7]} already reviewed")
        log.info("%s#%d @ %s: review queued (%s)", repo.full_name, pr.number, pr.head.sha[:7], ev.action)
        return Outcome("queued", run_id=run_id, tasks=[(TASK_REVIEW_PR, {"run_id": str(run_id)})])

    if ev.action == "closed" and pr.merged:
        log.info("%s#%d merged: learner queued", repo.full_name, pr.number)
        return Outcome("learn_queued", tasks=[(TASK_LEARN_FROM_PR, {"pr_id": str(pr_id)})])

    return Outcome("ignored", f"action {ev.action}")


async def _installation(session: AsyncSession, event: str, payload: dict) -> Outcome:
    """Keeps installations/repositories in sync when the App is installed, removed, or given more repos."""
    inst = payload.get("installation") or {}
    inst_id, action = inst.get("id"), payload.get("action")
    if not inst_id:
        raise InvalidPayload("installation.id missing")
    await _upsert_installation(session, inst_id, (inst.get("account") or {}).get("login", ""))

    if event == "installation":
        repos = payload.get("repositories") or []
        added = repos if action in ("created", "new_permissions_accepted") else []
        removed = repos if action == "deleted" else []
    else:
        added = payload.get("repositories_added") or []
        removed = payload.get("repositories_removed") or []

    for r in added:
        await _upsert_repository(session, r["id"], r["full_name"], inst_id, enable=True)
    if removed:
        await session.execute(
            update(Repository)
            .where(Repository.id.in_([r["id"] for r in removed]))
            .values(enabled=False, installation_id=None)
        )
    return Outcome("synced", f"{len(added)} added, {len(removed)} removed")


async def _upsert_installation(session: AsyncSession, inst_id: int, login: str) -> None:
    stmt = insert(Installation).values(id=inst_id, account_login=login)
    set_: dict[str, Any] = {"updated_at": func.now()}
    if login:
        set_["account_login"] = login
    await session.execute(stmt.on_conflict_do_update(index_elements=[Installation.id], set_=set_))


async def _upsert_repository(
    session: AsyncSession, repo_id: int, full_name: str, inst_id: int | None, enable: bool = False
) -> bool:
    """Returns whether reviews are enabled for the repo."""
    set_: dict[str, Any] = {"full_name": full_name, "updated_at": func.now()}
    if inst_id is not None:
        set_["installation_id"] = inst_id
    if enable:
        set_["enabled"] = True
    return await session.scalar(
        insert(Repository)
        .values(id=repo_id, full_name=full_name, installation_id=inst_id)
        .on_conflict_do_update(index_elements=[Repository.id], set_=set_)
        .returning(Repository.enabled)
    )

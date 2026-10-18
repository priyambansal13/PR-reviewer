"""Learner worker: learns from merged PRs (conventions, recurring issues, 👍/👎 feedback).

Run: uv run celery -A services.learner.worker worker -Q learn -n learner@%h --loglevel=INFO
"""

import logging
import uuid

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from common import aio
from common.celery_app import TASK_LEARN_FROM_PR, celery_app
from common.config import get_settings
from common.db.models import PullRequest
from common.db.session import session_scope
from common.github import GitHubAppAuth, GitHubClient, GitHubRetryable
from common.llm import make_llm
from common.logging import setup_logging
from services.learner.learn import learn_from_pr as learn

log = setup_logging("learner")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_learner(pr_id: str, force: bool = False) -> dict:
    settings = get_settings()
    pid = uuid.UUID(pr_id)
    async with session_scope() as session:
        pr = await session.scalar(
            select(PullRequest).where(PullRequest.id == pid).options(selectinload(PullRequest.repository))
        )
        if pr is None or pr.repository.installation_id is None:
            return {"skipped": "PR or installation not found"}
        installation_id, label = pr.repository.installation_id, f"{pr.repository.full_name}#{pr.number}"

    redis = Redis.from_url(settings.redis_url)
    try:
        async with httpx.AsyncClient(base_url=settings.github_api_url, timeout=30) as http:
            gh = GitHubClient(http, GitHubAppAuth.from_settings(http, redis), installation_id)
            async with session_scope() as session:
                result = await learn(session, pid, gh, make_llm(settings, temperature=0), force=force)
    finally:
        await redis.aclose()
    log.info("%s: %s", label, result)
    return result.__dict__


@celery_app.task(
    name=TASK_LEARN_FROM_PR,
    autoretry_for=(GitHubRetryable,),
    retry_backoff=60,
    max_retries=5,
)
def learn_from_pr(pr_id: str, force: bool = False) -> dict:
    return aio.run(run_learner(pr_id, force))

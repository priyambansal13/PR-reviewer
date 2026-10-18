"""Learner worker. Phase 1 stub: logs merged PRs. Phase 5 mines them into repo_patterns.

Run: uv run celery -A services.learner.worker worker -Q learn -n learner@%h --loglevel=INFO
"""

from common.celery_app import TASK_LEARN_FROM_PR, celery_app
from common.logging import setup_logging

log = setup_logging("learner")


@celery_app.task(name=TASK_LEARN_FROM_PR)
def learn_from_pr(pr_id: str) -> None:
    log.info("merged PR %s received — learning arrives in Phase 5", pr_id)

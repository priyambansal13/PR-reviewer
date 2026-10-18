"""One Celery app shared by all workers. Each service consumes its own queue so they scale separately."""

from celery import Celery

from common.config import get_settings

QUEUE_REVIEW = "review"  # orchestrator: fetch diff + LangGraph
QUEUE_POST = "post"  # reviewer: post comments to GitHub
QUEUE_LEARN = "learn"  # learner: mine merged PRs

TASK_REVIEW_PR = "orchestrator.review_pr"
TASK_POST_REVIEW = "reviewer.post_review"
TASK_LEARN_FROM_PR = "learner.learn_from_pr"

settings = get_settings()

celery_app = Celery("pr_reviewer", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=86_400,
    task_acks_late=True,  # a crashed worker's task goes back on the queue
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # LLM tasks are long; don't hoard them
    task_routes={
        TASK_REVIEW_PR: {"queue": QUEUE_REVIEW},
        TASK_POST_REVIEW: {"queue": QUEUE_POST},
        TASK_LEARN_FROM_PR: {"queue": QUEUE_LEARN},
    },
    broker_connection_retry_on_startup=True,
)

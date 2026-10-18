from common.celery_app import (
    QUEUE_LEARN,
    QUEUE_POST,
    QUEUE_REVIEW,
    TASK_LEARN_FROM_PR,
    TASK_POST_REVIEW,
    TASK_REVIEW_PR,
    celery_app,
)


def test_each_task_has_its_own_queue():
    routes = celery_app.conf.task_routes
    assert routes[TASK_REVIEW_PR]["queue"] == QUEUE_REVIEW
    assert routes[TASK_POST_REVIEW]["queue"] == QUEUE_POST
    assert routes[TASK_LEARN_FROM_PR]["queue"] == QUEUE_LEARN


def test_redis_broker_is_reachable():
    with celery_app.connection_for_write() as conn:
        conn.ensure_connection(max_retries=2)


def test_aio_run_reuses_one_loop_per_process():
    import asyncio

    from common import aio

    async def current():
        return asyncio.get_running_loop()

    assert aio.run(current()) is aio.run(current())

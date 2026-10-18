"""Serves a Celery worker container's metrics on :METRICS_PORT (default 9100).

docker-compose empties PROMETHEUS_MULTIPROC_DIR before starting celery, then each forked child writes its own
mmap files there; this server (in the parent process) sums them on every scrape.
"""

import os

from celery.signals import worker_init, worker_process_shutdown


@worker_init.connect
def _start_metrics_server(**_) -> None:
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    from prometheus_client import CollectorRegistry, multiprocess, start_http_server

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")), registry=registry)


@worker_process_shutdown.connect
def _mark_dead(pid: int | None = None, **_) -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid or os.getpid())

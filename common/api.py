"""Building blocks shared by the internal FastAPI services (webhook, orchestrator, reviewer, learner)."""

import secrets
from collections.abc import AsyncIterator
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.celery_app import celery_app
from common.config import get_settings
from common.db.session import get_engine


class Enqueuer(Protocol):
    def send(self, task_name: str, kwargs: dict[str, Any]) -> None: ...


class CeleryEnqueuer:
    def send(self, task_name: str, kwargs: dict[str, Any]) -> None:
        # send_task by *name*: an API never imports worker code
        celery_app.send_task(task_name, kwargs=kwargs)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as session:
        yield session


def get_enqueuer() -> Enqueuer:
    return CeleryEnqueuer()


def check_internal_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    """Internal services only accept calls that came through the gateway (which adds this header)."""
    expected = get_settings().internal_token
    if expected and not secrets.compare_digest(x_internal_token or "", expected):
        raise HTTPException(403, "internal endpoint")


Session = Annotated[AsyncSession, Depends(get_session)]
Queue = Annotated[Enqueuer, Depends(get_enqueuer)]


def make_app(title: str) -> FastAPI:
    """A FastAPI app with open /healthz (probes) and /metrics (Prometheus)."""
    app = FastAPI(title=f"PR Reviewer · {title}")
    Instrumentator(excluded_handlers=["/metrics", "/healthz"]).instrument(app).expose(app, include_in_schema=False)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return app


def internal_router() -> APIRouter:
    """Business routes go on this router: only callable with the gateway's internal token."""
    return APIRouter(dependencies=[Depends(check_internal_token)])

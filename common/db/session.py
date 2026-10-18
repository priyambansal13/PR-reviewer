"""Async engine + session helpers.

asyncpg connections are bound to the event loop that created them, so the engine is cached per loop.
Celery workers run every task on one long-lived loop per process (common/aio.py), so they share one engine;
one-off scripts using asyncio.run() get their own and call dispose_engine() at the end.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from common.config import get_settings

_engines: dict[int, AsyncEngine] = {}


def get_engine() -> AsyncEngine:
    loop_id = id(asyncio.get_running_loop())
    engine = _engines.get(loop_id)
    if engine is None:
        engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _engines[loop_id] = engine
    return engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on error."""
    maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    engine = _engines.pop(id(asyncio.get_running_loop()), None)
    if engine is not None:
        await engine.dispose()

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from common.config import get_settings


@pytest.fixture(scope="session")
async def engine():
    eng = create_async_engine(get_settings().database_url)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("select 1"))
    except OSError:
        pytest.skip("Postgres not reachable — run ./setup.sh start")
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine):
    """A session inside a transaction that is rolled back after the test, so tests leave no rows behind."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        yield session
        await session.close()
        await trans.rollback()

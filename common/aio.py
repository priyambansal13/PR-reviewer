"""Running async code from Celery's synchronous tasks.

`asyncio.run()` per task creates and closes a new event loop every time. Libraries that pool connections
process-wide (the OpenAI SDK's httpx client, SQLAlchemy's asyncpg pool) then hold connections bound to a dead
loop, and the next task fails with "Event loop is closed". Instead, each worker process keeps one loop for
its whole life and runs every task on it (prefork children run one task at a time, so this is safe).
"""

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_pid: int | None = None


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    global _loop, _pid
    if _loop is None or _loop.is_closed() or _pid != os.getpid():  # new process after fork -> new loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _pid = os.getpid()
    return _loop.run_until_complete(coro)

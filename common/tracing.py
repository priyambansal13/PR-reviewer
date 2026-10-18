"""Langfuse tracing for LLM calls. Off (a no-op) unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.

One trace per review: a root span with the PR as the Langfuse *session*, and the LangChain callback handler
records every agent's LLM call (prompt, output, tokens, latency, cost) as children of it.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from common.config import get_settings

log = logging.getLogger("tracing")

_client: Any = None
_pid: int | None = None


@dataclass
class Trace:
    callbacks: list = field(default_factory=list)  # pass as config={"callbacks": ...}
    trace_id: str | None = None
    url: str | None = None
    span: Any = None

    def output(self, value: Any) -> None:
        if self.span is not None:
            self.span.update(output=value)


def langfuse() -> Any:
    """The Langfuse client for this process (created after fork, so its export thread lives in the child)."""
    global _client, _pid
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    if _client is None or _pid != os.getpid():
        from langfuse import Langfuse

        _client = Langfuse(public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, base_url=s.langfuse_host)
        _pid = os.getpid()
    return _client


@contextmanager
def trace(name: str, *, seed: str, session_id: str, tags: list[str], metadata: dict[str, str]) -> Iterator[Trace]:
    lf = langfuse()
    if lf is None:
        yield Trace()
        return
    from langfuse import propagate_attributes
    from langfuse.langchain import CallbackHandler

    trace_id = lf.create_trace_id(seed=seed)  # deterministic: the run knows its trace id up front
    try:
        with lf.start_as_current_observation(
            trace_context={"trace_id": trace_id}, name=name, as_type="chain", metadata=metadata
        ) as span:
            with propagate_attributes(session_id=session_id, tags=tags, trace_name=name, metadata=metadata):
                yield Trace([CallbackHandler()], trace_id, lf.get_trace_url(trace_id=trace_id), span)
    finally:
        try:
            lf.flush()
        except Exception:  # tracing must never break a review
            log.warning("langfuse flush failed", exc_info=True)

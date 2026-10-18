"""Agent nodes. Each one sends the line-numbered diff to the model with its own system prompt and gets back a
validated `AgentReport`. Large diffs are split into chunks reviewed concurrently."""

import asyncio
import logging
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from services.orchestrator.graph import prompts
from services.orchestrator.graph.schemas import AgentReport, AgentRun, ReviewContext

log = logging.getLogger("orchestrator.agents")

CHUNK_CHARS = 40_000  # about 10k tokens of diff per call


def chunk_files(files: list, limit: int = CHUNK_CHARS) -> list[str]:
    """Groups rendered files into chunks under `limit` characters. A file is never split."""
    chunks: list[list[str]] = [[]]
    size = 0
    for f in files:
        text = f.render()
        if chunks[-1] and size + len(text) > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(text)
        size += len(text)
    return ["\n\n".join(c) for c in chunks if c]


def make_agent_node(agent: str, llm: Any):
    """`llm` is a LangChain chat model (or a test double with the same `with_structured_output`)."""
    structured = llm.with_structured_output(AgentReport, include_raw=True)
    system = SystemMessage(prompts.system_prompt(agent))

    async def node(state: dict, config: RunnableConfig) -> dict:
        ctx: ReviewContext = state["ctx"]
        started = time.monotonic()
        run = AgentRun(agent=agent, seconds=0, started_at=time.time())
        found: list = []
        try:
            calls = [
                structured.ainvoke(
                    [
                        system,
                        HumanMessage(
                            prompts.user_prompt(
                                ctx.repo, ctx.number, ctx.title, chunk, ctx.conventions, ctx.frequent_issues
                            )
                        ),
                    ],
                    config={**config, "run_name": f"{agent}-agent", "tags": [f"agent:{agent}"]},
                )
                for chunk in chunk_files(ctx.files)
            ]
            for result in await asyncio.gather(*calls):
                usage = getattr(result["raw"], "usage_metadata", None) or {}
                run.tokens_in += usage.get("input_tokens", 0)
                run.tokens_out += usage.get("output_tokens", 0)
                if result.get("parsing_error") or result.get("parsed") is None:
                    raise ValueError(f"unparseable model output: {result.get('parsing_error')}")
                report: AgentReport = result["parsed"]
                found.extend((agent, f) for f in report.findings[: prompts.MAX_FINDINGS_PER_AGENT])
        except Exception as exc:
            # One agent failing must not sink the whole review: record it and carry on with the others.
            log.exception("%s agent failed", agent)
            run.error = f"{type(exc).__name__}: {exc}"[:500]
            found = []
        run.seconds = round(time.monotonic() - started, 2)
        run.findings = len(found)
        log.info(
            "%s agent: %d findings in %.1fs (%d in / %d out tokens)",
            agent,
            run.findings,
            run.seconds,
            run.tokens_in,
            run.tokens_out,
        )
        return {"raw_findings": found, "agent_runs": [run]}

    node.__name__ = f"{agent}_agent"
    return node

"""The review graph.

    START ─┬─> static ───────┐
           ├─> security ─────┤
           ├─> architecture ─┼─> merge ─> summarize ─> END
           └─> style ────────┘

The four agent nodes have no edges between them, so LangGraph runs them in the same superstep, concurrently.
`merge` has an edge from each of them, so it runs once, after all four finish. They write to the same state keys
at the same time, and the `operator.add` reducers concatenate their lists instead of overwriting each other.
"""

import operator
import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from services.orchestrator.graph import prompts
from services.orchestrator.graph.agents import make_agent_node
from services.orchestrator.graph.merge import merge_findings
from services.orchestrator.graph.schemas import (
    SEVERITY_RANK,
    AgentFinding,
    AgentRun,
    Finding,
    ReviewContext,
    ReviewSummary,
)


class ReviewState(TypedDict, total=False):
    ctx: ReviewContext
    raw_findings: Annotated[list[tuple[str, AgentFinding]], operator.add]  # written by 4 agents in parallel
    agent_runs: Annotated[list[AgentRun], operator.add]
    findings: list[Finding]
    merge_stats: dict
    summary: str
    verdict: str


def build_review_graph(llm: Any):
    g = StateGraph(ReviewState)
    for agent in prompts.AGENTS:
        g.add_node(agent, make_agent_node(agent, llm))
        g.add_edge(START, agent)
        g.add_edge(agent, "merge")
    g.add_node("merge", _merge_node)
    g.add_node("summarize", _make_summary_node(llm))
    g.add_edge("merge", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


def _merge_node(state: ReviewState) -> dict:
    findings, stats = merge_findings(state.get("raw_findings", []), state["ctx"].files)
    return {"findings": findings, "merge_stats": stats}


def _make_summary_node(llm: Any):
    structured = llm.with_structured_output(ReviewSummary, include_raw=True)

    async def summarize(state: ReviewState, config: RunnableConfig) -> dict:
        ctx, findings = state["ctx"], state.get("findings", [])
        verdict = rule_verdict(findings)
        started = time.monotonic()
        run = AgentRun(agent="summary", seconds=0, started_at=time.time())
        files = "\n".join(f"- {f.path} (+{f.additions} -{f.deletions})" for f in ctx.files)
        listed = "\n".join(f"- [{f.severity}] {f.file}:{f.line or '-'} {f.title}" for f in findings[:40]) or "(none)"
        failed = [r.agent for r in state.get("agent_runs", []) if r.error]
        try:
            result = await structured.ainvoke(
                [
                    SystemMessage(prompts.SUMMARY_SYSTEM),
                    HumanMessage(f"PR #{ctx.number}: {ctx.title}\n\nFiles:\n{files}\n\nFindings:\n{listed}"),
                ],
                config={**config, "run_name": "summary"},
            )
            usage = getattr(result["raw"], "usage_metadata", None) or {}
            run.tokens_in, run.tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            summary = result["parsed"].summary if result.get("parsed") else ""
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"[:500]
            summary = ""
        if not summary:
            summary = f"Automated review found {len(findings)} issue(s) across {len(ctx.files)} file(s)."
        if failed:
            summary += f"\n\n_Note: the {', '.join(failed)} reviewer(s) failed on this run._"
        run.seconds = round(time.monotonic() - started, 2)
        # The verdict follows a fixed rule rather than the model, so it's predictable.
        return {"summary": summary, "verdict": verdict, "agent_runs": [run]}

    return summarize


def rule_verdict(findings: list[Finding]) -> str:
    if any(SEVERITY_RANK[f.severity] <= SEVERITY_RANK["high"] for f in findings):
        return "request_changes"
    if any(SEVERITY_RANK[f.severity] <= SEVERITY_RANK["low"] for f in findings):
        return "comment"
    return "approve"

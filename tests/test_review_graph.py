import asyncio
import time

import pytest
from langchain_core.messages import AIMessage

from common.diff import parse_diff
from services.orchestrator.graph.agents import chunk_files
from services.orchestrator.graph.graph import build_review_graph, rule_verdict
from services.orchestrator.graph.merge import fingerprint, merge_findings
from services.orchestrator.graph.prompts import REMITS
from services.orchestrator.graph.schemas import AgentFinding, AgentReport, Finding, ReviewContext, ReviewSummary

DIFF = """\
diff --git a/app/users.py b/app/users.py
new file mode 100644
--- /dev/null
+++ b/app/users.py
@@ -0,0 +1,6 @@
+import sqlite3
+
+def find_user(name):
+    conn = sqlite3.connect("u.db")
+    return conn.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchone()
+
"""
FILES = parse_diff(DIFF)


def af(line, title, severity="high", category="sql_injection", file="app/users.py", confidence=0.9, owasp=None):
    return AgentFinding(
        file=file,
        line=line,
        severity=severity,
        category=category,
        owasp_id=owasp,
        title=title,
        explanation="because",
        suggestion=None,
        confidence=confidence,
    )


class FakeLLM:
    """Stands in for ChatOpenAI: answers by agent (recognised from its system prompt), tracks concurrency."""

    def __init__(self, by_agent: dict, delay: float = 0.0, fail: tuple = ()):
        self.by_agent, self.delay, self.fail = by_agent, delay, fail
        self.active = self.max_active = 0

    def with_structured_output(self, schema, include_raw=False):
        llm = self

        class Bound:
            async def ainvoke(self, messages, config=None):
                usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
                raw = AIMessage(content="", usage_metadata=usage)
                if schema is ReviewSummary:
                    return {"raw": raw, "parsed": ReviewSummary(summary="Adds a user lookup."), "parsing_error": None}
                agent = next(a for a, remit in REMITS.items() if remit in messages[0].content)
                llm.active += 1
                llm.max_active = max(llm.max_active, llm.active)
                try:
                    await asyncio.sleep(llm.delay)
                    if agent in llm.fail:
                        raise TimeoutError("model timed out")
                    report = AgentReport(findings=llm.by_agent.get(agent, []))
                    return {"raw": raw, "parsed": report, "parsing_error": None}
                finally:
                    llm.active -= 1

        return Bound()


# --- merge ------------------------------------------------------------------------------------------------------


def test_merge_dedupes_across_agents_and_keeps_higher_severity():
    raw = [
        ("static", af(5, "Query built with f-string", severity="medium", category="unsafe_query")),
        ("security", af(5, "SQL injection via f-string query", severity="critical", owasp="A03")),
    ]
    merged, stats = merge_findings(raw, FILES)
    assert len(merged) == 1
    f = merged[0]
    assert f.agents == ["security", "static"]
    assert (f.severity, f.owasp_id, f.title) == ("critical", "A03", "SQL injection via f-string query")
    assert stats["duplicates"] == 1


def test_merge_keeps_distinct_problems_on_nearby_lines():
    raw = [
        ("static", af(4, "Connection is never closed", category="resource_leak")),
        ("security", af(5, "SQL injection", category="sql_injection")),
    ]
    merged, _ = merge_findings(raw, FILES)
    assert len(merged) == 2


def test_merge_validates_lines_files_and_confidence():
    raw = [
        ("static", af(99, "Line not in diff")),  # -> file level
        ("static", af(4, "Made-up file", file="nope.py")),  # -> dropped
        ("style", af(3, "Maybe rename", severity="low", category="naming", confidence=0.3)),  # -> dropped
        ("style", af(3, "Missing docstring", severity="low", category="docstring", file="users.py")),  # suffix match
    ]
    merged, stats = merge_findings(raw, FILES)
    assert stats == {"received": 4, "low_confidence": 1, "unknown_file": 1, "moved_to_file_level": 1, "duplicates": 0}
    assert {(f.title, f.line) for f in merged} == {("Line not in diff", None), ("Missing docstring", 3)}
    assert merged[0].severity == "high"  # sorted by severity


def test_fingerprint_ignores_word_order_and_filler():
    assert fingerprint("sql_injection", "SQL injection in the query") == fingerprint(
        "sql_injection", "query SQL injection"
    )
    assert fingerprint("sql_injection", "x") != fingerprint("naming", "x")


def test_rule_verdict():
    def f(sev):
        return Finding([], "a", 1, sev, "c", None, "t", "e", None, 1.0)

    assert rule_verdict([]) == "approve"
    assert rule_verdict([f("info")]) == "approve"
    assert rule_verdict([f("low"), f("medium")]) == "comment"
    assert rule_verdict([f("medium"), f("high")]) == "request_changes"


def test_chunking_never_splits_a_file():
    many = parse_diff(DIFF.replace("app/users.py", "a.py") + DIFF.replace("app/users.py", "b.py"))
    assert len(chunk_files(many, limit=10_000)) == 1
    assert len(chunk_files(many, limit=10)) == 2


# --- graph ------------------------------------------------------------------------------------------------------


async def test_four_agents_run_in_parallel_and_merge():
    llm = FakeLLM(
        {
            "static": [af(4, "Connection is never closed", severity="medium", category="resource_leak")],
            "security": [af(5, "SQL injection via f-string", severity="critical", owasp="A03")],
            "architecture": [af(None, "Data access mixed into handler", severity="low", category="layering")],
            "style": [af(3, "Missing docstring", severity="info", category="docstring")],
        },
        delay=0.3,
    )
    graph = build_review_graph(llm)
    started = time.monotonic()
    state = await graph.ainvoke({"ctx": ReviewContext("octo/demo", 7, "Add lookup", FILES)})
    elapsed = time.monotonic() - started

    assert llm.max_active == 4  # all four agents were in flight at the same time
    assert elapsed < 0.9  # sequential would take at least 1.2s
    assert [f.severity for f in state["findings"]] == ["critical", "medium", "low", "info"]
    assert state["verdict"] == "request_changes"
    assert state["summary"] == "Adds a user lookup."
    runs = {r.agent: r for r in state["agent_runs"]}
    assert set(runs) == {"static", "security", "architecture", "style", "summary"}
    assert runs["security"].tokens_in == 100


async def test_one_failing_agent_does_not_sink_the_review():
    llm = FakeLLM({"security": [af(5, "SQL injection", owasp="A03")]}, fail=("style",))
    state = await build_review_graph(llm).ainvoke({"ctx": ReviewContext("octo/demo", 7, "t", FILES)})
    runs = {r.agent: r for r in state["agent_runs"]}
    assert runs["style"].error.startswith("TimeoutError")
    assert len(state["findings"]) == 1
    assert "style reviewer(s) failed" in state["summary"]


@pytest.mark.parametrize("agent", ["static", "security", "architecture", "style"])
def test_prompts_separate_remits(agent):
    from services.orchestrator.graph.prompts import system_prompt

    p = system_prompt(agent)
    assert "untrusted" in p and "NOT yours" in p


def test_only_security_sets_owasp_and_severity_is_capped_outside_remit():
    raw = [
        ("static", af(5, "SQL injection in find_user", owasp="A01")),
        ("security", af(5, "SQL injection vulnerability", severity="critical", owasp="A03")),
        ("style", af(3, "Confusing function name", severity="high", category="naming")),
        ("architecture", af(None, "Data access in handler", severity="critical", category="layering")),
    ]
    merged, _ = merge_findings(raw, FILES)
    by_title = {f.title: f for f in merged}
    assert by_title["SQL injection vulnerability"].owasp_id == "A03"
    assert by_title["Confusing function name"].severity == "low"
    assert by_title["Data access in handler"].severity == "medium"


def test_same_line_findings_merge_with_plural_folding():
    raw = [
        ("security", af(5, "Hard-coded secret key", category="hard_coded_secret", owasp="A02")),
        ("style", af(5, "Avoid hard-coded secrets", severity="low", category="magic_number")),
    ]
    merged, _ = merge_findings(raw, FILES)
    assert len(merged) == 1 and merged[0].agents == ["security", "style"]

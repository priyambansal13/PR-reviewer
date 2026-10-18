"""Markdown for humans: the check-run output now, the PR review summary in Phase 4."""

from collections import Counter

from services.orchestrator.graph.cost import cost_usd
from services.orchestrator.graph.schemas import SEVERITY_RANK, AgentRun, Finding

ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
VERDICT = {"approve": "✅ Looks good", "comment": "💬 Some suggestions", "request_changes": "⛔ Changes requested"}


def headline(findings: list[Finding]) -> str:
    if not findings:
        return "No issues found"
    counts = Counter(f.severity for f in findings)
    parts = [f"{counts[s]} {s}" for s in sorted(counts, key=SEVERITY_RANK.get)]
    return f"{len(findings)} issue{'s' if len(findings) != 1 else ''} · " + ", ".join(parts)


def check_conclusion(findings: list[Finding]) -> str:
    if any(f.severity == "critical" for f in findings):
        return "failure"
    return "neutral" if findings else "success"


def findings_table(findings: list[Finding], limit: int = 50) -> str:
    if not findings:
        return "_No issues found._"
    rows = ["| | Where | Issue | Found by |", "|---|---|---|---|"]
    for f in findings[:limit]:
        where = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
        owasp = f" ({f.owasp_id})" if f.owasp_id else ""
        rows.append(f"| {ICON[f.severity]} {f.severity} | {where} | {_cell(f.title)}{owasp} | {', '.join(f.agents)} |")
    if len(findings) > limit:
        rows.append(f"\n_…and {len(findings) - limit} more._")
    return "\n".join(rows)


def details(findings: list[Finding]) -> str:
    out = []
    for f in findings:
        where = f"{f.file}:{f.line}" if f.line else f.file
        owasp = f" · {f.owasp_id}" if f.owasp_id else ""
        meta = f"`{where}` · {f.category}{owasp} · {', '.join(f.agents)}"
        out.append(f"### {ICON[f.severity]} {f.title}\n{meta}\n\n{f.explanation}")
        if f.suggestion:
            out.append(f"```\n{f.suggestion}\n```")
    return "\n\n".join(out)


def run_footer(runs: list[AgentRun], model: str, skipped: list[tuple[str, str]]) -> str:
    lines = []
    if skipped:
        lines.append("<details><summary>Files not reviewed</summary>\n")
        lines += [f"- `{p}`: {why}" for p, why in skipped]
        lines.append("</details>\n")
    tin, tout = sum(r.tokens_in for r in runs), sum(r.tokens_out for r in runs)
    per_agent = " · ".join(f"{r.agent} {r.seconds:.1f}s{' ⚠️ failed' if r.error else ''}" for r in runs)
    lines.append(
        f"<sub>{model} · {tin:,} in / {tout:,} out tokens · ${cost_usd(model, tin, tout):.4f} · {per_agent}</sub>"
    )
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")

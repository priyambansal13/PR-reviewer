"""Turns saved findings into one GitHub review: inline comments + a summary body. Pure functions, no I/O."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol

from common.diff import FileDiff

MARKER = "<!-- pr-reviewer:run={} -->"  # hidden in the review body; lets a retry see it already posted

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
LABEL = {"critical": "🔴 Critical", "high": "🟠 High", "medium": "🟡 Medium", "low": "🔵 Low", "info": "⚪ Info"}
HEADLINE = {"approve": "✅ Looks good", "comment": "💬 Some suggestions", "request_changes": "⛔ Changes requested"}
OWASP = {
    "A01": "Broken Access Control",
    "A02": "Cryptographic Failures",
    "A03": "Injection",
    "A04": "Insecure Design",
    "A05": "Security Misconfiguration",
    "A06": "Vulnerable Components",
    "A07": "Auth Failures",
    "A08": "Integrity Failures",
    "A09": "Logging & Monitoring Failures",
    "A10": "SSRF",
}
LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".sql": "sql",
    ".sh": "bash",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".php": "php",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".tf": "hcl",
}


class FindingLike(Protocol):
    id: Any
    agents: list[str]
    severity: Any  # enum or str
    category: str
    owasp_id: str | None
    file: str
    line: int | None
    title: str
    body: str
    suggestion: str | None
    confidence: float


@dataclass
class ReviewPayload:
    body: str
    event: str  # COMMENT | REQUEST_CHANGES (the bot never APPROVEs)
    comments: list[dict[str, Any]]
    inline_ids: list[Any] = field(default_factory=list)  # finding ids posted as inline comments, same order


def sev(f: FindingLike) -> str:
    return getattr(f.severity, "value", f.severity)


def build_review(
    run_id: str,
    findings: list[FindingLike],
    files: dict[str, FileDiff],
    summary: str,
    verdict: str,
    max_inline: int,
    footer: str = "",
    request_changes: bool = True,
) -> ReviewPayload:
    ordered = sorted(findings, key=lambda f: (SEVERITY_RANK[sev(f)], f.file, f.line or 0))
    inline, rest = [], []
    for f in ordered:
        fd = files.get(f.file)
        ok = f.line is not None and fd is not None and f.line in fd.commentable_lines()
        (inline if ok and len(inline) < max_inline else rest).append(f)

    comments = [_comment(f, files[f.file]) for f in inline]
    body = _body(run_id, ordered, rest, summary, verdict, len(inline), footer)
    event = "REQUEST_CHANGES" if verdict == "request_changes" and request_changes else "COMMENT"
    return ReviewPayload(body, event, comments, [f.id for f in inline])


def _comment(f: FindingLike, fd: FileDiff) -> dict[str, Any]:
    owasp = f" · `{f.owasp_id} {OWASP.get(f.owasp_id, '')}`".rstrip() if f.owasp_id else ""
    parts = [f"**{LABEL[sev(f)]}** · {f.title}{owasp}", "", f.body]
    if fix := _suggestion_block(f, fd):
        parts += ["", fix]
    parts += ["", f"<sub>{', '.join(f.agents)} · confidence {f.confidence:.0%}</sub>"]
    return {"path": f.file, "line": f.line, "side": "RIGHT", "body": "\n".join(parts)}


def _suggestion_block(f: FindingLike, fd: FileDiff) -> str | None:
    if not f.suggestion or not f.suggestion.strip():
        return None
    lines = f.suggestion.strip("\n").splitlines()
    original = fd.line_content(f.line) if f.line else None
    if len(lines) == 1 and original is not None:
        # GitHub replaces exactly the commented line with the block's contents: one line for one line is safe.
        fixed = lines[0]
        indent = original[: len(original) - len(original.lstrip())]
        if not fixed.startswith((" ", "\t")):
            fixed = indent + fixed  # models often drop the indentation
        if fixed.strip() == original.strip():
            return None
        return f"```suggestion\n{fixed}\n```"
    # Multi-line: we can't be sure which lines it should replace, so show it as code instead of a one-click change.
    lang = LANG.get(PurePosixPath(f.file).suffix, "")
    return f"Suggested fix:\n```{lang}\n" + "\n".join(lines) + "\n```"


def _body(run_id, ordered, rest, summary, verdict, n_inline, footer) -> str:
    counts = Counter(sev(f) for f in ordered)
    tally = " · ".join(f"{LABEL[s]} {counts[s]}" for s in sorted(counts, key=SEVERITY_RANK.get)) or "no issues"
    out = [f"## 🤖 AI Review — {HEADLINE.get(verdict, verdict)}", "", summary.strip(), "", f"**Findings:** {tally}"]
    if n_inline:
        out.append(f"\n{n_inline} are posted as inline comments below.")
    if rest:
        out += [
            "",
            "<details><summary>"
            f"{len(rest)} more finding{'s' if len(rest) != 1 else ''} (file-level or over the inline limit)"
            "</summary>",
            "",
            "| | Where | Issue |",
            "|---|---|---|",
        ]
        for f in rest:
            where = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
            title = f.title.replace("|", "\\|")
            owasp = f" ({f.owasp_id})" if f.owasp_id else ""
            out.append(f"| {LABEL[sev(f)]} | {where} | {title}{owasp} |")
        out += ["", "</details>"]
    if footer:
        out += ["", footer]
    out += ["", MARKER.format(run_id)]
    return "\n".join(out)

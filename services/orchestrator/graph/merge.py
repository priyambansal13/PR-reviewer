"""Merging the four agents' findings: validate, de-duplicate, rank."""

import hashlib
import re

from common.diff import FileDiff
from services.orchestrator.graph.schemas import SEVERITY_RANK, AgentFinding, Finding

MIN_CONFIDENCE = 0.5
NEAR_LINES = 2  # two findings within this many lines of each other may be the same problem
# Agents drift outside their remit; cap the severity they can claim there.
SEVERITY_CAP = {"style": "low", "architecture": "medium"}
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "of", "in", "on", "to", "is", "be", "for", "and", "or", "with", "this", "that", "use"}


def merge_findings(
    raw: list[tuple[str, AgentFinding]], files: list[FileDiff], suppressed: set[str] | frozenset = frozenset()
) -> tuple[list[Finding], dict]:
    """Returns the merged findings plus counters explaining what was dropped (useful for tuning).

    `suppressed`: categories the team keeps 👎-ing (from the Learner). Dropped unless high or critical.
    """
    by_path = {f.path: f for f in files}
    stats = {"received": len(raw), "low_confidence": 0, "unknown_file": 0, "moved_to_file_level": 0, "duplicates": 0}
    if suppressed:
        stats["suppressed"] = 0
    merged: list[Finding] = []

    for agent, af in raw:
        if af.confidence < MIN_CONFIDENCE:
            stats["low_confidence"] += 1
            continue
        if _slug(af.category) in suppressed and SEVERITY_RANK[af.severity] > SEVERITY_RANK["high"]:
            stats["suppressed"] += 1
            continue
        path = _resolve_path(af.file, by_path)
        if path is None:
            stats["unknown_file"] += 1  # the model invented or mangled a path
            continue
        line = af.line
        if line is not None and line not in by_path[path].commentable_lines():
            # GitHub would reject an inline comment here; keep the point but put it in the summary instead
            stats["moved_to_file_level"] += 1
            line = None

        severity = af.severity
        if (cap := SEVERITY_CAP.get(agent)) and SEVERITY_RANK[severity] < SEVERITY_RANK[cap]:
            severity = cap
        candidate = Finding(
            agents=[agent],
            file=path,
            line=line,
            severity=severity,
            category=_slug(af.category),
            owasp_id=(af.owasp_id or None) if agent == "security" else None,  # only the OWASP specialist labels
            title=af.title.strip()[:120],
            explanation=af.explanation.strip(),
            suggestion=(af.suggestion or "").strip("\n") or None,
            confidence=round(af.confidence, 2),
        )
        twin = next((m for m in merged if _same_problem(m, candidate)), None)
        if twin is None:
            merged.append(candidate)
        else:
            stats["duplicates"] += 1
            _absorb(twin, candidate)

    for f in merged:
        f.fingerprint = fingerprint(f.category, f.title)
    merged.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.file, f.line if f.line is not None else -1))
    return merged, stats


def fingerprint(category: str, title: str) -> str:
    """Stable id for "the same kind of problem" across PRs (the Learner counts these)."""
    words = sorted(_words(title))
    return hashlib.sha1(f"{category}|{' '.join(words)}".encode()).hexdigest()[:16]


def _same_problem(a: Finding, b: Finding) -> bool:
    if a.file != b.file:
        return False
    if a.line is None or b.line is None:
        return a.line is None and b.line is None and _similarity(a.title, b.title) >= 0.6
    if a.line == b.line:
        return a.category == b.category or _similarity(a.title, b.title) >= 0.25
    if abs(a.line - b.line) > NEAR_LINES:
        return False
    return a.category == b.category or _similarity(a.title, b.title) >= 0.4


def _absorb(keep: Finding, other: Finding) -> None:
    keep.agents = sorted(set(keep.agents) | set(other.agents))
    if SEVERITY_RANK[other.severity] < SEVERITY_RANK[keep.severity]:
        # the more severe report wins the wording too
        keep.severity, keep.title, keep.explanation, keep.category = (
            other.severity,
            other.title,
            other.explanation,
            other.category,
        )
        keep.line = other.line if other.line is not None else keep.line
    keep.suggestion = keep.suggestion or other.suggestion
    keep.owasp_id = keep.owasp_id or other.owasp_id
    keep.confidence = max(keep.confidence, other.confidence)


def _resolve_path(path: str, by_path: dict[str, FileDiff]) -> str | None:
    path = path.strip().removeprefix("b/").removeprefix("./")
    if path in by_path:
        return path
    matches = [p for p in by_path if p.endswith("/" + path) or path.endswith("/" + p)]
    return matches[0] if len(matches) == 1 else None


def _words(text: str) -> set[str]:
    words = (
        w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in _WORD.findall(text.lower())
    )
    return set(words) - _STOP


def _similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _slug(s: str) -> str:
    return "_".join(_WORD.findall(s.lower()))[:64] or "general"

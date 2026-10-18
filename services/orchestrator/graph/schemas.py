"""What the agents must return. OpenAI structured outputs (strict JSON schema) enforce this shape, so every field
is required; "optional" fields are nullable instead of defaulted."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class AgentFinding(BaseModel):
    file: str = Field(description="Path exactly as written after '###' in the diff")
    line: int | None = Field(
        description="New-file line number from the left margin of the diff. null for a file-level comment."
    )
    severity: Severity
    category: str = Field(description="short snake_case label, e.g. sql_injection, resource_leak, naming")
    owasp_id: str | None = Field(description="OWASP Top 10 (2021) id like 'A03' for security issues, else null")
    title: str = Field(description="One-line summary, at most 80 characters")
    explanation: str = Field(description="Why this is a problem and what can go wrong. 1-3 sentences.")
    suggestion: str | None = Field(
        description="Replacement code for the commented line(s) if there is a concrete fix, else null. "
        "Plain code, no markdown fences."
    )
    confidence: float = Field(description="0-1: how sure you are that this is a real problem")


class AgentReport(BaseModel):
    findings: list[AgentFinding]


class ReviewSummary(BaseModel):
    summary: str = Field(description="2-4 sentences of markdown: what the PR does and its main risks")


@dataclass
class Finding:
    """A finding after merging: possibly reported by several agents."""

    agents: list[str]
    file: str
    line: int | None
    severity: Severity
    category: str
    owasp_id: str | None
    title: str
    explanation: str
    suggestion: str | None
    confidence: float
    fingerprint: str = ""


@dataclass
class AgentRun:
    agent: str
    seconds: float
    tokens_in: int = 0
    tokens_out: int = 0
    findings: int = 0
    error: str | None = None
    started_at: float = 0.0


@dataclass
class ReviewContext:
    repo: str
    number: int
    title: str
    files: list  # list[common.diff.FileDiff]
    conventions: list[str] = field(default_factory=list)
    frequent_issues: list[str] = field(default_factory=list)

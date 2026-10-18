"""SQLAlchemy models. See docs/PLAN.md §3 for the data model diagram."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunStatus(enum.StrEnum):
    queued = "queued"
    fetching = "fetching"
    reviewing = "reviewing"
    saved = "saved"
    posting = "posting"
    posted = "posted"
    skipped = "skipped"
    failed = "failed"


class Severity(enum.StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class PatternKind(enum.StrEnum):
    frequent_issue = "frequent_issue"
    convention = "convention"


def _pg_enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class Installation(TimestampMixin, Base):
    """A GitHub App installation (one per account/org that installed the App)."""

    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account_login: Mapped[str] = mapped_column(String(255))

    repositories: Mapped[list["Repository"]] = relationship(back_populates="installation")


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # GitHub repo id
    installation_id: Mapped[int | None] = mapped_column(ForeignKey("installations.id", ondelete="SET NULL"))
    full_name: Mapped[str] = mapped_column(String(255), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    installation: Mapped[Installation | None] = relationship(back_populates="repositories")
    pull_requests: Mapped[list["PullRequest"]] = relationship(back_populates="repository")
    patterns: Mapped[list["RepoPattern"]] = relationship(back_populates="repository")


class PullRequest(TimestampMixin, Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(255), default="")
    base_ref: Mapped[str] = mapped_column(String(255), default="")
    head_ref: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(20), default="open")
    draft: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    merged: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repository] = relationship(back_populates="pull_requests")
    review_runs: Mapped[list["ReviewRun"]] = relationship(back_populates="pull_request")


class ReviewRun(TimestampMixin, Base):
    """One review of one head SHA. UNIQUE(pr_id, head_sha) is what de-duplicates repeated webhooks."""

    __tablename__ = "review_runs"
    __table_args__ = (UniqueConstraint("pr_id", "head_sha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id", ondelete="CASCADE"))
    head_sha: Mapped[str] = mapped_column(String(40))
    base_sha: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[RunStatus] = mapped_column(
        _pg_enum(RunStatus, "run_status"), default=RunStatus.queued, server_default=RunStatus.queued.value
    )
    files_reviewed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0, server_default="0")
    summary: Mapped[str | None] = mapped_column(Text)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))
    github_review_id: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    pull_request: Mapped[PullRequest] = relationship(back_populates="review_runs")
    findings: Mapped[list["Finding"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Finding(TimestampMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_run_severity", "run_id", "severity"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_runs.id", ondelete="CASCADE"))
    agents: Mapped[list[str]] = mapped_column(ARRAY(String(32)))  # several agents can report the same thing
    severity: Mapped[Severity] = mapped_column(_pg_enum(Severity, "severity"))
    category: Mapped[str] = mapped_column(String(64))
    owasp_id: Mapped[str | None] = mapped_column(String(8))  # e.g. "A03"
    file: Mapped[str] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)  # None = file/PR level, goes in the summary
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)  # stable hash used by the Learner
    posted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger)

    run: Mapped[ReviewRun] = relationship(back_populates="findings")


class RepoPattern(TimestampMixin, Base):
    """What the Learner knows about a repo: recurring issues and observed conventions."""

    __tablename__ = "repo_patterns"
    __table_args__ = (UniqueConstraint("repo_id", "kind", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    kind: Mapped[PatternKind] = mapped_column(_pg_enum(PatternKind, "pattern_kind"))
    key: Mapped[str] = mapped_column(String(255))  # normalised identity, so repeats bump `frequency`
    pattern: Mapped[str] = mapped_column(Text)
    frequency: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    weight: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")  # lowered by 👎 feedback
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repository: Mapped[Repository] = relationship(back_populates="patterns")


class WebhookDelivery(Base):
    """Every X-GitHub-Delivery id we accepted — GitHub redelivers, so this makes ingestion idempotent."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    repo_id: Mapped[int | None] = mapped_column(BigInteger)
    result: Mapped[str] = mapped_column(String(32))  # queued | duplicate | ignored
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

"""System of record. Postgres in production; SQLite works for local runs and tests."""
from __future__ import annotations

import enum
from typing import ClassVar
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict] = {dict: JSON, list: JSON}


class CaptureStatus(str, enum.Enum):
    pending = "pending"
    fetched = "fetched"
    blocked = "blocked"  # not in registry, robots.txt disallow, or source disabled
    failed = "failed"
    extracted = "extracted"


class CandidateStatus(str, enum.Enum):
    extracted = "extracted"
    rejected = "rejected"  # failed qualification gate (never shown to reviewers)
    qualified = "qualified"
    verified = "verified"
    duplicate = "duplicate"
    pending_review = "pending_review"
    approved = "approved"
    rejected_review = "rejected_review"
    exported = "exported"


class EmailStatus(str, enum.Enum):
    valid = "valid"
    invalid = "invalid"
    catch_all = "catch_all"
    risky = "risky"
    disposable = "disposable"
    role_based = "role_based"
    syntax_error = "syntax_error"
    domain_error = "domain_error"
    unknown = "unknown"


# Only these count toward a delivered batch.
COUNTABLE_EMAIL_STATUSES = {EmailStatus.valid}
# Delivered on a separate, non-counted tab.
SIDECAR_EMAIL_STATUSES = {EmailStatus.catch_all}


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class Source(Base):
    """Compliance registry: a domain is only fetched when it is listed here and allowed."""

    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(64), default="forum")
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    respect_robots: Mapped[bool] = mapped_column(Boolean, default=True)
    render_js: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_rps: Mapped[float | None] = mapped_column(Float, nullable=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    tos_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tos_reviewed_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    category: Mapped[Category] = relationship()


class Query(Base):
    __tablename__ = "queries"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    text: Mapped[str] = mapped_column(Text)
    template_key: Mapped[str] = mapped_column(String(128))  # stable key used by the yield loop
    site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Capture(Base):
    """Immutable evidence: what the page said when we read it."""

    __tablename__ = "captures"
    # The same page can hold requests for different categories, so uniqueness is per category.
    __table_args__ = (UniqueConstraint("canonical_url", "category_id", name="uq_capture_url_category"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(String(1024), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    query_id: Mapped[int | None] = mapped_column(ForeignKey("queries.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    status: Mapped[CaptureStatus] = mapped_column(Enum(CaptureStatus, native_enum=False), default=CaptureStatus.pending)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    html_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    name_norm: Mapped[str] = mapped_column(String(255), index=True)
    email_norm: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Candidate(Base):
    """One person seeking one item, as evidenced by one capture."""

    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("capture_id", "email_norm", "request_fingerprint", name="uq_candidate_capture_person_item"),
        Index("ix_candidates_status_category", "status", "category_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    capture_id: Mapped[int] = mapped_column(ForeignKey("captures.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True)

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_norm: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    item_sought: Mapped[str | None] = mapped_column(String(120), nullable=True)
    item_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_posted: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_posted_raw: Mapped[str | None] = mapped_column(String(120), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)  # field -> verbatim quote
    intent_phrase: Mapped[str | None] = mapped_column(String(120), nullable=True)
    extractor: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    email_status: Mapped[EmailStatus | None] = mapped_column(Enum(EmailStatus, native_enum=False), nullable=True)
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus, native_enum=False), default=CandidateStatus.extracted
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list] = mapped_column(JSON, default=list)  # e.g. repeat_person, fuzzy_match:123

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    capture: Mapped[Capture] = relationship()
    category: Mapped[Category] = relationship()


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_norm: Mapped[str] = mapped_column(String(320), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[EmailStatus] = mapped_column(Enum(EmailStatus, native_enum=False))
    sub_status: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DuplicateLink(Base):
    __tablename__ = "duplicate_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    duplicate_of_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    reason: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float, default=100.0)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    reviewer: Mapped[str] = mapped_column(String(120))
    decision: Mapped[str] = mapped_column(String(16))  # approve | reject
    edits: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("category_id", "number", name="uq_batch_category_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    number: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    file_path: Mapped[str] = mapped_column(Text)
    record_count: Mapped[int] = mapped_column(Integer)
    sidecar_count: Mapped[int] = mapped_column(Integer, default=0)
    available_total: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[Category] = relationship()


class BatchItem(Base):
    __tablename__ = "batch_items"
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), primary_key=True, unique=True)
    counted: Mapped[bool] = mapped_column(Boolean, default=True)


class Suppression(Base):
    """Opt-outs and do-not-contact addresses. Checked before any spend."""

    __tablename__ = "suppression"
    email_norm: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageEvent(Base):
    """Metered spend, so cost per approved record is measurable per run."""

    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # search | llm | email_verification | fetch
    units: Mapped[float] = mapped_column(Float, default=1.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(120))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

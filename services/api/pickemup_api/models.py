"""Canonical-ish investigation schema (directive §9), MVP subset.

Postgres-compatible: JSON columns, String PKs, DateTime(timezone).
Every analytical row keeps provenance + algo version for traceability.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followers: Mapped[int | None] = mapped_column(Integer)
    following: Mapped[int | None] = mapped_column(Integer)
    verified: Mapped[bool | None] = mapped_column(Boolean)
    default_profile: Mapped[bool | None] = mapped_column(Boolean)
    origin_mode: Mapped[str] = mapped_column(String, index=True)
    gt_label: Mapped[str | None] = mapped_column(String)   # fixtures/synthetic only
    region: Mapped[str | None] = mapped_column(String)     # macro-region code (estimated unless live profile location)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lang: Mapped[str | None] = mapped_column(String)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    urls: Mapped[list] = mapped_column(JSON, default=list)
    mentions: Mapped[list] = mapped_column(JSON, default=list)
    repost_of: Mapped[str | None] = mapped_column(String, index=True)
    quote_of: Mapped[str | None] = mapped_column(String, index=True)
    reply_to: Mapped[str | None] = mapped_column(String)
    retweet_count: Mapped[int | None] = mapped_column(Integer)
    origin_mode: Mapped[str] = mapped_column(String, index=True)
    source_url: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_posts_author_created", "author_id", "created_at"),)


class DetectionResult(Base):
    __tablename__ = "detection_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, index=True)     # automation|cluster|edge|narrative
    subject_id: Mapped[str] = mapped_column(String, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Float)
    algo_version: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # signals, evidence ids, caveats
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_detect_kind_subject", "kind", "subject_id"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)  # queued|running|done|failed|dead|cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|review|closed
    analyst: Mapped[str] = mapped_column(String, default="analyst")
    findings: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CaseMember(Base):
    __tablename__ = "case_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String)     # account|post|cluster|narrative
    ref_id: Mapped[str] = mapped_column(String)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseNote(Base):
    __tablename__ = "case_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    author: Mapped[str] = mapped_column(String, default="analyst")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="medium")
    dedupe_key: Mapped[str] = mapped_column(String, unique=True)
    subject_id: Mapped[str] = mapped_column(String)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="new", index=True)  # new|acked|false_positive|suppressed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only audit trail (evidence integrity, directive §15)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String, default="system")
    action: Mapped[str] = mapped_column(String, index=True)
    subject: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class KV(Base):
    """Small persistent state: worker heartbeat, daily budget counters."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class CustomTerm(Base):
    """Operator-extensible trigger-word watchlist merged into the moderation
    matrix at scan time. The DB is the source of truth: additions/edits
    re-score the whole corpus via a queued analyze_content_risk job."""
    __tablename__ = "custom_term"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    severity: Mapped[str] = mapped_column(String(12), default="watch")  # watch|flag|severe
    note: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(40), default="analyst")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Entity(Base):
    """Typed entity extracted from post text; lineage lives in EntityPost links."""
    __tablename__ = "entity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    etype: Mapped[str] = mapped_column(String(12), index=True)     # URL|DOMAIN|HASHTAG|MENTION|EMAIL|PHONE
    value: Mapped[str] = mapped_column(String(300), index=True)    # normalized (lowercased) value
    label: Mapped[str] = mapped_column(String(300), default="")    # as-first-seen display form
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ux_entity_type_value", "etype", "value", unique=True),)


class EntityPost(Base):
    """Evidence link: which post produced which entity, how often."""
    __tablename__ = "entity_post"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    post_id: Mapped[str] = mapped_column(String, index=True)
    author_id: Mapped[str] = mapped_column(String, index=True, default="")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ux_entity_post", "entity_id", "post_id", unique=True),)


class Report(Base):
    """Generated analyst report (template or LLM-narrated) + delivery trail.

    `facts` stores the exact deterministic fact-pack the narrative was built
    from, and `facts_sha256` fingerprints it — a report can always be audited
    back to the evidence state that produced it.
    """
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, index=True)   # snapshot|cluster:<id>|account:<id>|alerts|investigation:<q>
    title: Mapped[str] = mapped_column(String, default="")
    language: Mapped[str] = mapped_column(String, default="en")
    mode: Mapped[str] = mapped_column(String, default="template")   # template|llm
    model: Mapped[str | None] = mapped_column(String)
    content_md: Mapped[str] = mapped_column(Text)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    facts_sha256: Mapped[str] = mapped_column(String, default="")
    llm_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String, default="analyst")  # analyst|monitor
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReportDelivery(Base):
    """Every external email send of a report — opt-in and audited by design."""
    __tablename__ = "report_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    channel: Mapped[str] = mapped_column(String, default="email")
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    recipient_kind: Mapped[str] = mapped_column(String, default="user")  # user|authorities|custom
    status: Mapped[str] = mapped_column(String, default="sent")          # sent|failed
    detail: Mapped[str] = mapped_column(Text, default="")                # message-id / error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

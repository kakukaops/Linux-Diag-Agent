"""M2 PostgreSQL ORM models — 14 tables (ADR-006, ADR-007, ADR-018).

All tables are append-mostly; updates occur only for incremental ingestion
(lkml thread summaries, bug status changes, hit_count increments).

BM25 full-text search uses PostgreSQL tsvector + GIN indexes (ADR-006).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════════════════════
# Commit graph
# ═══════════════════════════════════════════════════════════════════════════

class KernelCommit(Base):
    """One row per unique git commit (OLK kernel or mainline linux-stable)."""
    __tablename__ = "kernel_commit"

    hash = Column(Text, primary_key=True)
    # short_hash is a generated column in PG — handled via migration DDL, not ORM
    author_name = Column(Text)
    author_email = Column(Text)
    commit_date = Column(DateTime(timezone=True), nullable=False, index=True)
    subject = Column(Text, nullable=False)
    body = Column(Text)
    fixes_refs = Column(ARRAY(Text))               # SHA list from Fixes: trailers
    reported_by = Column(ARRAY(Text))
    closes_refs = Column(ARRAY(Text))
    subsystem = Column(Text, index=True)           # inferred from file paths (M4)
    # ADR-018 columns
    origin = Column(Text, nullable=False, default="olk")  # 'olk' | 'mainline'
    upstream_commit = Column(Text, index=True)     # mainline SHA (NULL = native)
    olk_inclusion_type = Column(Text)              # open set: mainline/stable/hulk/...
    affected_versions = Column(ARRAY(Text))        # ['OLK-6.6','OLK-5.10'] or ['mainline']
    metadata_ = Column("metadata", JSONB, server_default="{}")
    # body_tsv is a generated column — DDL in migration, ORM uses server_default trick
    body_tsv = Column(TSVECTOR)                    # managed by DB trigger / generated col

    __table_args__ = (
        Index("idx_kc_date", "commit_date"),
        Index("idx_kc_subsystem", "subsystem"),
        Index("idx_kc_upstream", "upstream_commit"),
        Index("idx_kc_origin", "origin"),
        Index("idx_kc_body_tsv", "body_tsv", postgresql_using="gin"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Discussion graph — LKML
# ═══════════════════════════════════════════════════════════════════════════

class LkmlThread(Base):
    """One row per LKML mail thread (identified by root Message-ID)."""
    __tablename__ = "lkml_thread"

    id = Column(Integer, primary_key=True, autoincrement=True)
    root_message_id = Column(Text, nullable=False, unique=True)
    subject = Column(Text, nullable=False)
    list_name = Column(Text, nullable=False)        # e.g. 'linux-kernel'
    date_start = Column(DateTime(timezone=True), nullable=False, index=True)
    date_end = Column(DateTime(timezone=True))
    message_count = Column(Integer, default=0)
    # LLM-generated three-part summary (M3, WBS 2.3)
    summary_problem = Column(Text)
    summary_solution = Column(Text)
    summary_outcome = Column(Text)
    summary_generated_at = Column(DateTime(timezone=True))

    messages = relationship("LkmlMessage", back_populates="thread", lazy="dynamic")

    __table_args__ = (
        Index("idx_lt_date_start", "date_start"),
        Index("idx_lt_list_name", "list_name"),
    )


class LkmlMessage(Base):
    """One row per individual LKML email."""
    __tablename__ = "lkml_message"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Text, nullable=False, unique=True)
    thread_id = Column(Integer, ForeignKey("lkml_thread.id"), nullable=False, index=True)
    in_reply_to = Column(Text)                      # parent Message-ID
    author_name = Column(Text)
    author_email = Column(Text)
    date = Column(DateTime(timezone=True), nullable=False, index=True)
    subject = Column(Text)
    body = Column(Text)
    body_tsv = Column(TSVECTOR)                     # GIN-indexed tsvector

    thread = relationship("LkmlThread", back_populates="messages")

    __table_args__ = (
        Index("idx_lm_date", "date"),
        Index("idx_lm_body_tsv", "body_tsv", postgresql_using="gin"),
    )


class LkmlPatch(Base):
    """Patch emails extracted from LKML (has diff content)."""
    __tablename__ = "lkml_patch"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Text, ForeignKey("lkml_message.message_id"),
                        nullable=False, unique=True)
    patch_id = Column(Text, index=True)             # git patch-id hash
    series_subject = Column(Text)
    patch_number = Column(Integer)
    series_total = Column(Integer)
    diff_stat = Column(Text)                        # condensed diff stat
    files_changed = Column(ARRAY(Text))             # list of modified file paths


class LkmlReview(Base):
    """Review/ack/nack trailers extracted from LKML messages."""
    __tablename__ = "lkml_review"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Text, ForeignKey("lkml_message.message_id"),
                        nullable=False, index=True)
    review_type = Column(Text, nullable=False)      # Reviewed-by/Acked-by/Tested-by/NACK/...
    reviewer_name = Column(Text)
    reviewer_email = Column(Text)
    date = Column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint("message_id", "review_type", "reviewer_email",
                         name="uq_lkml_review"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bug graph
# ═══════════════════════════════════════════════════════════════════════════

class Bug(Base):
    """Normalized bug record — aggregates bugzilla.kernel.org + syzbot reports."""
    __tablename__ = "bug"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False)           # 'bugzilla_kernel' | 'syzbot'
    external_id = Column(Text, nullable=False)      # BZ id or syzbot title hash
    title = Column(Text, nullable=False)
    status = Column(Text)                           # RESOLVED/CONFIRMED/... or syzbot status
    component = Column(Text, index=True)
    subsystem = Column(Text, index=True)
    severity = Column(Text)
    reporter = Column(Text)
    assignee = Column(Text)
    created_at = Column(DateTime(timezone=True), index=True)
    updated_at = Column(DateTime(timezone=True))
    closed_at = Column(DateTime(timezone=True))
    kernel_versions = Column(ARRAY(Text))
    description = Column(Text)
    resolution = Column(Text)
    body_tsv = Column(TSVECTOR)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_bug_source_ext"),
        Index("idx_bug_created_at", "created_at"),
        Index("idx_bug_body_tsv", "body_tsv", postgresql_using="gin"),
    )


class Cve(Base):
    """CVE records from NVD JSON feed (ADR-011)."""
    __tablename__ = "cve"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cve_id = Column(Text, nullable=False, unique=True)  # e.g. CVE-2024-35892
    published_at = Column(DateTime(timezone=True), index=True)
    last_modified_at = Column(DateTime(timezone=True))
    description = Column(Text)
    cvss_v3_score = Column(Numeric(4, 1))
    cvss_v3_vector = Column(Text)
    cwe_ids = Column(ARRAY(Text))
    affected_products = Column(JSONB)               # raw NVD configurations
    references = Column(JSONB)                      # [{url, tags}, ...]
    body_tsv = Column(TSVECTOR)

    __table_args__ = (
        Index("idx_cve_published_at", "published_at"),
        Index("idx_cve_body_tsv", "body_tsv", postgresql_using="gin"),
    )


class SyzbotCrash(Base):
    """syzbot crash report (HTML-scraped from syzkaller.appspot.com)."""
    __tablename__ = "syzbot_crash"

    id = Column(Integer, primary_key=True, autoincrement=True)
    syzbot_id = Column(Text, nullable=False, unique=True)   # URL hash / title slug
    title = Column(Text, nullable=False)
    status = Column(Text)                           # open | fixed | invalid
    subsystem = Column(Text, index=True)
    first_seen = Column(DateTime(timezone=True), index=True)
    last_seen = Column(DateTime(timezone=True))
    fix_commit = Column(Text)                       # SHA if fixed
    reproducer_c = Column(Text)                     # C reproducer (may be large)
    reproducer_syz = Column(Text)                   # syz reproducer
    kernel_config_url = Column(Text)
    stack_trace = Column(Text)
    stack_signature = Column(Text, index=True)      # normalized stack hash for dedup
    body_tsv = Column(TSVECTOR)

    __table_args__ = (
        Index("idx_sc_first_seen", "first_seen"),
        Index("idx_sc_body_tsv", "body_tsv", postgresql_using="gin"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Graph Link tables (M4)
# ═══════════════════════════════════════════════════════════════════════════

class LinkCommitBug(Base):
    """commit ↔ bug relationship (Fixes:/Reported-by:/Closes: trailers, M4)."""
    __tablename__ = "link_commit_bug"

    id = Column(Integer, primary_key=True, autoincrement=True)
    commit_hash = Column(Text, ForeignKey("kernel_commit.hash"), nullable=False, index=True)
    bug_id = Column(Integer, ForeignKey("bug.id"), nullable=False, index=True)
    link_type = Column(Text, nullable=False)        # 'fixes' | 'reported_by' | 'closes'
    confidence = Column(Numeric(3, 2), default=1.0)
    source = Column(Text)                           # 'trailer' | 'llm_inferred' | 'nvd'

    __table_args__ = (
        UniqueConstraint("commit_hash", "bug_id", "link_type", name="uq_lcb"),
    )


class LinkCommitMessage(Base):
    """commit ↔ LKML message/patch relationship (M4 patch-commit linker)."""
    __tablename__ = "link_commit_message"

    id = Column(Integer, primary_key=True, autoincrement=True)
    commit_hash = Column(Text, ForeignKey("kernel_commit.hash"), nullable=False, index=True)
    message_id = Column(Text, ForeignKey("lkml_message.message_id"),
                        nullable=False, index=True)
    link_type = Column(Text, nullable=False)        # 'patch' | 'discussion' | 'link_trailer'
    match_method = Column(Text)                     # 'patch_id' | 'subject' | 'link_trailer'
    confidence = Column(Numeric(3, 2), default=1.0)

    __table_args__ = (
        UniqueConstraint("commit_hash", "message_id", "link_type", name="uq_lcm"),
    )


class LinkCommitCve(Base):
    """commit ↔ CVE relationship (NVD reference URLs + trailer extraction, WBS 3.3)."""
    __tablename__ = "link_commit_cve"

    id = Column(Integer, primary_key=True, autoincrement=True)
    commit_hash = Column(Text, ForeignKey("kernel_commit.hash"), nullable=False, index=True)
    cve_id = Column(Text, ForeignKey("cve.cve_id"), nullable=False, index=True)
    link_type = Column(Text, nullable=False)         # 'nvd_ref' | 'trailer'
    confidence = Column(Numeric(3, 2), default=1.0)
    source = Column(Text)                            # 'nvd' | 'trailer'

    __table_args__ = (
        UniqueConstraint("commit_hash", "cve_id", "link_type", name="uq_lcc"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# System tables
# ═══════════════════════════════════════════════════════════════════════════

class LlmResponseCache(Base):
    """M1 PG cache for deterministic LLM responses (M1 §6)."""
    __tablename__ = "llm_response_cache"

    cache_key = Column(Text, primary_key=True)
    provider = Column(Text, nullable=False)
    model = Column(Text, nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    response = Column(JSONB, nullable=False)
    usage = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_hit_at = Column(DateTime(timezone=True), server_default=func.now())
    hit_count = Column(Integer, default=0)
    cost_saved_usd = Column(Numeric, default=0)

    __table_args__ = (
        Index("idx_llm_cache_created", "created_at"),
        Index("idx_llm_cache_last_hit", "last_hit_at"),
    )


class IngestRun(Base):
    """Ingestion run audit log — one row per ingester invocation."""
    __tablename__ = "ingest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ingester = Column(Text, nullable=False, index=True)  # 'lkml' | 'bugzilla' | ...
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    status = Column(Text)                           # 'running' | 'ok' | 'error'
    rows_inserted = Column(Integer, default=0)
    rows_updated = Column(Integer, default=0)
    checkpoint = Column(JSONB)                      # resumable cursor (date, offset, etc.)
    error_message = Column(Text)

    __table_args__ = (
        Index("idx_ir_ingester", "ingester"),
        Index("idx_ir_started_at", "started_at"),
    )


class EvalCase(Base):
    """30-case evaluation dataset (v1.2, M8)."""
    __tablename__ = "eval_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Text, nullable=False, unique=True)  # e.g. 'case-001'
    fault_type = Column(Text, nullable=False)        # 'oom' | 'panic' | 'lockup' | ...
    kernel_version = Column(Text)
    description = Column(Text)
    ground_truth_commits = Column(ARRAY(Text))
    ground_truth_bugs = Column(ARRAY(Text))
    source = Column(Text)                           # 'syzbot' | 'lkml' | 'bugzilla' | ...
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    metadata_ = Column("metadata", JSONB, server_default="{}")


class EvalResult(Base):
    """Evaluation run result — one row per (case, judge, run)."""
    __tablename__ = "eval_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Text, ForeignKey("eval_cases.case_id"), nullable=False, index=True)
    run_id = Column(Text, nullable=False, index=True)
    judge_name = Column(Text, nullable=False)
    # 5-dimension rubric (M8 §3.2)
    score_root_cause = Column(Integer)              # 0/1/2
    score_evidence = Column(Integer)
    score_fix = Column(Integer)
    score_readability = Column(Integer)
    score_safety = Column(Integer)
    final_label = Column(Text)                      # 'A' | 'B' | 'C' | 'D'
    notes = Column(Text)
    judged_at = Column(DateTime(timezone=True), server_default=func.now())
    report_md_path = Column(Text)
    report_json_path = Column(Text)

    __table_args__ = (
        UniqueConstraint("case_id", "run_id", "judge_name", name="uq_eval_result"),
    )

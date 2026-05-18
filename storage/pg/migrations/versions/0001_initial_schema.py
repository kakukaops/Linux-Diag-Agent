"""Initial schema — 14 tables (M2 design, ADR-006/018).

Revision ID: 0001
Revises:
Create Date: 2026-05-18

Notes:
- kernel_commit.short_hash: STORED generated column (not in ORM metadata)
- kernel_commit.body_tsv / lkml_message.body_tsv / bug.body_tsv / cve.body_tsv /
  syzbot_crash.body_tsv: STORED generated tsvector columns for BM25 (ADR-006)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── kernel_commit ─────────────────────────────────────────────────────
    op.create_table(
        "kernel_commit",
        sa.Column("hash", sa.Text, primary_key=True),
        # STORED generated column: not expressible via ORM, use raw DDL below
        sa.Column("author_name", sa.Text),
        sa.Column("author_email", sa.Text),
        sa.Column("commit_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text),
        sa.Column("fixes_refs", ARRAY(sa.Text)),
        sa.Column("reported_by", ARRAY(sa.Text)),
        sa.Column("closes_refs", ARRAY(sa.Text)),
        sa.Column("subsystem", sa.Text),
        sa.Column("origin", sa.Text, nullable=False, server_default="olk"),
        sa.Column("upstream_commit", sa.Text),
        sa.Column("olk_inclusion_type", sa.Text),
        sa.Column("affected_versions", ARRAY(sa.Text)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("body_tsv", TSVECTOR),
    )
    # Add STORED generated columns via raw DDL (not supported by Alembic column API)
    op.execute("""
        ALTER TABLE kernel_commit
            ADD COLUMN short_hash TEXT
                GENERATED ALWAYS AS (substr(hash, 1, 12)) STORED
    """)
    op.execute("""
        ALTER TABLE kernel_commit
            ALTER COLUMN body_tsv
            DROP DEFAULT
    """)
    op.execute("""
        ALTER TABLE kernel_commit DROP COLUMN body_tsv
    """)
    op.execute("""
        ALTER TABLE kernel_commit
            ADD COLUMN body_tsv TSVECTOR
                GENERATED ALWAYS AS (
                    to_tsvector('english',
                        coalesce(subject, '') || ' ' || coalesce(body, ''))
                ) STORED
    """)
    op.create_index("idx_kc_date", "kernel_commit", ["commit_date"])
    op.create_index("idx_kc_subsystem", "kernel_commit", ["subsystem"])
    op.create_index("idx_kc_upstream", "kernel_commit", ["upstream_commit"])
    op.create_index("idx_kc_origin", "kernel_commit", ["origin"])
    op.create_index("idx_kc_body_tsv", "kernel_commit", ["body_tsv"],
                    postgresql_using="gin")

    # ── lkml_thread ───────────────────────────────────────────────────────
    op.create_table(
        "lkml_thread",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("root_message_id", sa.Text, nullable=False, unique=True),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("list_name", sa.Text, nullable=False),
        sa.Column("date_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_end", sa.DateTime(timezone=True)),
        sa.Column("message_count", sa.Integer, server_default="0"),
        sa.Column("summary_problem", sa.Text),
        sa.Column("summary_solution", sa.Text),
        sa.Column("summary_outcome", sa.Text),
        sa.Column("summary_generated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_lt_date_start", "lkml_thread", ["date_start"])
    op.create_index("idx_lt_list_name", "lkml_thread", ["list_name"])

    # ── lkml_message ─────────────────────────────────────────────────────
    op.create_table(
        "lkml_message",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Text, nullable=False, unique=True),
        sa.Column("thread_id", sa.Integer,
                  sa.ForeignKey("lkml_thread.id"), nullable=False),
        sa.Column("in_reply_to", sa.Text),
        sa.Column("author_name", sa.Text),
        sa.Column("author_email", sa.Text),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("body_tsv", TSVECTOR),
    )
    op.execute("""
        ALTER TABLE lkml_message DROP COLUMN body_tsv
    """)
    op.execute("""
        ALTER TABLE lkml_message
            ADD COLUMN body_tsv TSVECTOR
                GENERATED ALWAYS AS (
                    to_tsvector('english',
                        coalesce(subject, '') || ' ' || coalesce(body, ''))
                ) STORED
    """)
    op.create_index("idx_lm_date", "lkml_message", ["date"])
    op.create_index("idx_lm_thread_id", "lkml_message", ["thread_id"])
    op.create_index("idx_lm_body_tsv", "lkml_message", ["body_tsv"],
                    postgresql_using="gin")

    # ── lkml_patch ───────────────────────────────────────────────────────
    op.create_table(
        "lkml_patch",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Text,
                  sa.ForeignKey("lkml_message.message_id"), nullable=False, unique=True),
        sa.Column("patch_id", sa.Text),
        sa.Column("series_subject", sa.Text),
        sa.Column("patch_number", sa.Integer),
        sa.Column("series_total", sa.Integer),
        sa.Column("diff_stat", sa.Text),
        sa.Column("files_changed", ARRAY(sa.Text)),
    )
    op.create_index("idx_lp_patch_id", "lkml_patch", ["patch_id"])

    # ── lkml_review ──────────────────────────────────────────────────────
    op.create_table(
        "lkml_review",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Text,
                  sa.ForeignKey("lkml_message.message_id"), nullable=False),
        sa.Column("review_type", sa.Text, nullable=False),
        sa.Column("reviewer_name", sa.Text),
        sa.Column("reviewer_email", sa.Text),
        sa.Column("date", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("message_id", "review_type", "reviewer_email",
                            name="uq_lkml_review"),
    )
    op.create_index("idx_lr_message_id", "lkml_review", ["message_id"])
    op.create_index("idx_lr_date", "lkml_review", ["date"])

    # ── bug ───────────────────────────────────────────────────────────────
    op.create_table(
        "bug",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text),
        sa.Column("component", sa.Text),
        sa.Column("subsystem", sa.Text),
        sa.Column("severity", sa.Text),
        sa.Column("reporter", sa.Text),
        sa.Column("assignee", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("kernel_versions", ARRAY(sa.Text)),
        sa.Column("description", sa.Text),
        sa.Column("resolution", sa.Text),
        sa.Column("body_tsv", TSVECTOR),
        sa.UniqueConstraint("source", "external_id", name="uq_bug_source_ext"),
    )
    op.execute("""
        ALTER TABLE bug DROP COLUMN body_tsv
    """)
    op.execute("""
        ALTER TABLE bug
            ADD COLUMN body_tsv TSVECTOR
                GENERATED ALWAYS AS (
                    to_tsvector('english',
                        coalesce(title, '') || ' ' || coalesce(description, ''))
                ) STORED
    """)
    op.create_index("idx_bug_created_at", "bug", ["created_at"])
    op.create_index("idx_bug_subsystem", "bug", ["subsystem"])
    op.create_index("idx_bug_body_tsv", "bug", ["body_tsv"], postgresql_using="gin")

    # ── cve ───────────────────────────────────────────────────────────────
    op.create_table(
        "cve",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("cve_id", sa.Text, nullable=False, unique=True),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_modified_at", sa.DateTime(timezone=True)),
        sa.Column("description", sa.Text),
        sa.Column("cvss_v3_score", sa.Numeric(4, 1)),
        sa.Column("cvss_v3_vector", sa.Text),
        sa.Column("cwe_ids", ARRAY(sa.Text)),
        sa.Column("affected_products", JSONB),
        sa.Column("references", JSONB),
        sa.Column("body_tsv", TSVECTOR),
    )
    op.execute("""
        ALTER TABLE cve DROP COLUMN body_tsv
    """)
    op.execute("""
        ALTER TABLE cve
            ADD COLUMN body_tsv TSVECTOR
                GENERATED ALWAYS AS (
                    to_tsvector('english',
                        coalesce(cve_id, '') || ' ' || coalesce(description, ''))
                ) STORED
    """)
    op.create_index("idx_cve_published_at", "cve", ["published_at"])
    op.create_index("idx_cve_body_tsv", "cve", ["body_tsv"], postgresql_using="gin")

    # ── syzbot_crash ─────────────────────────────────────────────────────
    op.create_table(
        "syzbot_crash",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("syzbot_id", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text),
        sa.Column("subsystem", sa.Text),
        sa.Column("first_seen", sa.DateTime(timezone=True)),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("fix_commit", sa.Text),
        sa.Column("reproducer_c", sa.Text),
        sa.Column("reproducer_syz", sa.Text),
        sa.Column("kernel_config_url", sa.Text),
        sa.Column("stack_trace", sa.Text),
        sa.Column("stack_signature", sa.Text),
        sa.Column("body_tsv", TSVECTOR),
    )
    op.execute("""
        ALTER TABLE syzbot_crash DROP COLUMN body_tsv
    """)
    op.execute("""
        ALTER TABLE syzbot_crash
            ADD COLUMN body_tsv TSVECTOR
                GENERATED ALWAYS AS (
                    to_tsvector('english',
                        coalesce(title, '') || ' ' || coalesce(stack_trace, ''))
                ) STORED
    """)
    op.create_index("idx_sc_first_seen", "syzbot_crash", ["first_seen"])
    op.create_index("idx_sc_stack_sig", "syzbot_crash", ["stack_signature"])
    op.create_index("idx_sc_body_tsv", "syzbot_crash", ["body_tsv"],
                    postgresql_using="gin")

    # ── link_commit_bug ───────────────────────────────────────────────────
    op.create_table(
        "link_commit_bug",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("commit_hash", sa.Text,
                  sa.ForeignKey("kernel_commit.hash"), nullable=False),
        sa.Column("bug_id", sa.Integer,
                  sa.ForeignKey("bug.id"), nullable=False),
        sa.Column("link_type", sa.Text, nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), server_default="1.0"),
        sa.Column("source", sa.Text),
        sa.UniqueConstraint("commit_hash", "bug_id", "link_type", name="uq_lcb"),
    )
    op.create_index("idx_lcb_commit", "link_commit_bug", ["commit_hash"])
    op.create_index("idx_lcb_bug", "link_commit_bug", ["bug_id"])

    # ── link_commit_message ───────────────────────────────────────────────
    op.create_table(
        "link_commit_message",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("commit_hash", sa.Text,
                  sa.ForeignKey("kernel_commit.hash"), nullable=False),
        sa.Column("message_id", sa.Text,
                  sa.ForeignKey("lkml_message.message_id"), nullable=False),
        sa.Column("link_type", sa.Text, nullable=False),
        sa.Column("match_method", sa.Text),
        sa.Column("confidence", sa.Numeric(3, 2), server_default="1.0"),
        sa.UniqueConstraint("commit_hash", "message_id", "link_type", name="uq_lcm"),
    )
    op.create_index("idx_lcm_commit", "link_commit_message", ["commit_hash"])
    op.create_index("idx_lcm_message", "link_commit_message", ["message_id"])

    # ── llm_response_cache ────────────────────────────────────────────────
    op.create_table(
        "llm_response_cache",
        sa.Column("cache_key", sa.Text, primary_key=True),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("response", JSONB, nullable=False),
        sa.Column("usage", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("last_hit_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("hit_count", sa.Integer, server_default="0"),
        sa.Column("cost_saved_usd", sa.Numeric, server_default="0"),
    )
    op.create_index("idx_llm_cache_created", "llm_response_cache", ["created_at"])
    op.create_index("idx_llm_cache_last_hit", "llm_response_cache", ["last_hit_at"])

    # ── ingest_runs ───────────────────────────────────────────────────────
    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ingester", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text),
        sa.Column("rows_inserted", sa.Integer, server_default="0"),
        sa.Column("rows_updated", sa.Integer, server_default="0"),
        sa.Column("checkpoint", JSONB),
        sa.Column("error_message", sa.Text),
    )
    op.create_index("idx_ir_ingester", "ingest_runs", ["ingester"])
    op.create_index("idx_ir_started_at", "ingest_runs", ["started_at"])

    # ── eval_cases ────────────────────────────────────────────────────────
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.Text, nullable=False, unique=True),
        sa.Column("fault_type", sa.Text, nullable=False),
        sa.Column("kernel_version", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("ground_truth_commits", ARRAY(sa.Text)),
        sa.Column("ground_truth_bugs", ARRAY(sa.Text)),
        sa.Column("source", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("metadata", JSONB, server_default="{}"),
    )

    # ── eval_results ──────────────────────────────────────────────────────
    op.create_table(
        "eval_results",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.Text,
                  sa.ForeignKey("eval_cases.case_id"), nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("judge_name", sa.Text, nullable=False),
        sa.Column("score_root_cause", sa.Integer),
        sa.Column("score_evidence", sa.Integer),
        sa.Column("score_fix", sa.Integer),
        sa.Column("score_readability", sa.Integer),
        sa.Column("score_safety", sa.Integer),
        sa.Column("final_label", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("judged_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("report_md_path", sa.Text),
        sa.Column("report_json_path", sa.Text),
        sa.UniqueConstraint("case_id", "run_id", "judge_name", name="uq_eval_result"),
    )
    op.create_index("idx_er_case_id", "eval_results", ["case_id"])
    op.create_index("idx_er_run_id", "eval_results", ["run_id"])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("eval_results")
    op.drop_table("eval_cases")
    op.drop_table("ingest_runs")
    op.drop_table("llm_response_cache")
    op.drop_table("link_commit_message")
    op.drop_table("link_commit_bug")
    op.drop_table("syzbot_crash")
    op.drop_table("cve")
    op.drop_table("bug")
    op.drop_table("lkml_review")
    op.drop_table("lkml_patch")
    op.drop_table("lkml_message")
    op.drop_table("lkml_thread")
    op.drop_table("kernel_commit")

"""Add link_commit_symbol — KG edge between a commit and the C symbols it
touches in its diff. v2.3 KG gap fix #1: enables "find commits that
modified this function" queries that bridge the symptom-symbol → fix-symbol
vocabulary gap (see kasan-002 case study).

Source: parsed from `git show --unified=0` hunk headers (the function /
struct name that git auto-detects appears after `@@ ... @@`).

Backfill is incremental and idempotent (ON CONFLICT DO NOTHING) — the
extractor in ingest/kernel_commit/symbol_extractor.py walks commits not
yet covered.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "link_commit_symbol",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("commit_hash", sa.Text(), sa.ForeignKey("kernel_commit.hash"),
                  nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False,
                  comment="C symbol name (function or struct) modified by this commit"),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="function",
                  comment="'function' | 'struct' | 'macro' | 'unknown'"),
        sa.UniqueConstraint("commit_hash", "symbol", "file_path", name="uq_lcs"),
    )
    op.create_index("ix_lcs_symbol", "link_commit_symbol", ["symbol"])
    op.create_index("ix_lcs_commit", "link_commit_symbol", ["commit_hash"])
    op.create_index("ix_lcs_file",   "link_commit_symbol", ["file_path"])


def downgrade() -> None:
    op.drop_index("ix_lcs_file", table_name="link_commit_symbol")
    op.drop_index("ix_lcs_commit", table_name="link_commit_symbol")
    op.drop_index("ix_lcs_symbol", table_name="link_commit_symbol")
    op.drop_table("link_commit_symbol")

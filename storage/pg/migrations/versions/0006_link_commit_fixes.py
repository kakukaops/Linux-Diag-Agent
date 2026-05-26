"""Add link_commit_fixes + link_commit_revert tables (v2.1 P0a/P0b).

Materializes the patch-lineage cross-graph edges that were previously buried
in `kernel_commit.fixes_refs[]` text array and Revert subject text.

P0a: link_commit_fixes — 87K rows from existing fixes_refs[] array
P0b: link_commit_revert — ~6K rows from "Revert " subject + "This reverts commit"

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # link_commit_fixes — commit A fixes commit B (via "Fixes: <sha>" trailer)
    op.create_table(
        "link_commit_fixes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fixer_hash", sa.Text(), sa.ForeignKey("kernel_commit.hash"),
                  nullable=False, index=True),
        sa.Column("fixed_hash", sa.Text(), sa.ForeignKey("kernel_commit.hash"),
                  nullable=False, index=True),
        sa.Column("source", sa.Text(), nullable=False,
                  comment="'trailer' (Fixes: line) | 'subject' (Revert prefix)"),
        sa.Column("confidence", sa.Numeric(3, 2), server_default="0.95"),
        sa.UniqueConstraint("fixer_hash", "fixed_hash", name="uq_lcfx"),
    )

    # link_commit_revert — commit A reverts commit B
    op.create_table(
        "link_commit_revert",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("reverter_hash", sa.Text(), sa.ForeignKey("kernel_commit.hash"),
                  nullable=False, index=True),
        sa.Column("reverted_hash", sa.Text(), sa.ForeignKey("kernel_commit.hash"),
                  nullable=False, index=True),
        sa.Column("source", sa.Text(), nullable=False,
                  comment="'subject_revert' | 'body_reverts_commit'"),
        sa.Column("confidence", sa.Numeric(3, 2), server_default="0.9"),
        sa.UniqueConstraint("reverter_hash", "reverted_hash", name="uq_lcrv"),
    )


def downgrade() -> None:
    op.drop_table("link_commit_revert")
    op.drop_table("link_commit_fixes")

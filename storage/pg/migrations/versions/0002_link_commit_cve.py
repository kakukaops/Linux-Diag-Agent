"""Add link_commit_cve table (WBS 3.3).

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "link_commit_cve",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("commit_hash", sa.Text(), sa.ForeignKey("kernel_commit.hash"),
                  nullable=False, index=True),
        sa.Column("cve_id", sa.Text(), sa.ForeignKey("cve.cve_id"),
                  nullable=False, index=True),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), server_default="1.0"),
        sa.Column("source", sa.Text()),
        sa.UniqueConstraint("commit_hash", "cve_id", "link_type", name="uq_lcc"),
    )
    op.create_index("ix_link_commit_cve_commit_hash", "link_commit_cve", ["commit_hash"])
    op.create_index("ix_link_commit_cve_cve_id", "link_commit_cve", ["cve_id"])


def downgrade() -> None:
    op.drop_table("link_commit_cve")

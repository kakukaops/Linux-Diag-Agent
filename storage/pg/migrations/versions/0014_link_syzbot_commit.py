"""Add link_syzbot_commit — materialised edge table for syzbot ↔ commit.

After the v2.3 syzbot ingestion fix (5,772 fixed bugs each carrying
1-8 fix SHAs across mainline + stable backports, 14,785 total), the
data lives in syzbot_crash.fix_commits jsonb. This migration adds an
explicit edge table so the ReAct agent can ask:
  - "Which syzbot bugs does commit <sha> fix?" (reverse lookup)
  - "All fixed syzbot bugs in subsystem X" (JOIN with kernel_commit)

Mirrors link_commit_cve in structure (uq_lsc unique constraint).
Built by a separate Python step (see graph.linker.link_syzbot_commits)
— this migration is structure-only.

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "link_syzbot_commit",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("syzbot_id", sa.Text, sa.ForeignKey("syzbot_crash.syzbot_id"),
                  nullable=False),
        sa.Column("commit_hash", sa.Text, sa.ForeignKey("kernel_commit.hash"),
                  nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default="syzbot_fix_url",
                  comment="'syzbot_fix_url' — extracted from /upstream/fixed page URLs"),
        sa.UniqueConstraint("syzbot_id", "commit_hash", name="uq_lsc"),
    )
    op.create_index("ix_lsc_syzbot", "link_syzbot_commit", ["syzbot_id"])
    op.create_index("ix_lsc_commit", "link_syzbot_commit", ["commit_hash"])


def downgrade() -> None:
    op.drop_index("ix_lsc_commit", table_name="link_syzbot_commit")
    op.drop_index("ix_lsc_syzbot", table_name="link_syzbot_commit")
    op.drop_table("link_syzbot_commit")

"""Add syzbot_crash.fix_commits jsonb — full fix-commit list incl. backports.

The existing `fix_commit text` column held only one SHA. syzbot's fixed
bug pages actually carry multiple fix commits (mainline + per-stable-branch
backports) — we lost that signal. New column mirrors the design of
cve.fix_commits (JSONB array of SHAs).

The primary `fix_commit` text column is kept for back-compat and is set
to `fix_commits[0]` on write.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "syzbot_crash",
        sa.Column("fix_commits", JSONB, nullable=True,
                  comment="Full list of fix commit SHAs (mainline + stable backports)"),
    )


def downgrade() -> None:
    op.drop_column("syzbot_crash", "fix_commits")

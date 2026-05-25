"""Add fix_commits jsonb column to cve table."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cve", sa.Column("fix_commits", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cve", "fix_commits")

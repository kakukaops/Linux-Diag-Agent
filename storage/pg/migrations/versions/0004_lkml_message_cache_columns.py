"""Add cache-tracking columns to lkml_message (ADR-025).

`lkml_message` changes semantics from "bulk corpus" to "smart cache":
  fetched_via — how the row entered the cache: 'bulk' | 'targeted' | 'lazy'
                | 'discovery'. NULL for rows ingested before this migration
                (legacy bulk).
  cached_at   — when the row was fetched into the cache.

Both nullable: an ADD COLUMN with no default is metadata-only in modern
PostgreSQL, so it does not rewrite the (large) lkml_message table.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lkml_message", sa.Column("fetched_via", sa.Text(), nullable=True))
    op.add_column(
        "lkml_message",
        sa.Column("cached_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lkml_message", "cached_at")
    op.drop_column("lkml_message", "fetched_via")

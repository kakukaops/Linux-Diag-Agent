"""Add link_author_subsystem — materialised author/subsystem activity.

Closes v2.3 KG gap (per docs/v2/KG_design_and_acceptance.md §四): the
"who maintains this subsystem?" affordance. An experienced engineer
mentally maps "Eric Dumazet → net/tcp" and uses it as a credibility
signal when reading patches; we need an explicit edge so retrieval
can do the same.

This is a fully DERIVED table — refreshed by a SQL aggregation over
kernel_commit. No new source data; just a materialisation that
indexes (author_email, subsystem) → commit_count for O(1) lookups.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "link_author_subsystem",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("author_email", sa.Text, nullable=False),
        sa.Column("author_name",  sa.Text, nullable=True),
        sa.Column("subsystem",    sa.Text, nullable=False),
        sa.Column("commit_count", sa.Integer, nullable=False),
        sa.Column("first_commit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_commit_date",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True),
                   server_default=sa.func.now()),
        sa.UniqueConstraint("author_email", "subsystem", name="uq_las"),
    )
    op.create_index("ix_las_subsystem", "link_author_subsystem", ["subsystem"])
    op.create_index("ix_las_author",    "link_author_subsystem", ["author_email"])


def downgrade() -> None:
    op.drop_index("ix_las_author",    table_name="link_author_subsystem")
    op.drop_index("ix_las_subsystem", table_name="link_author_subsystem")
    op.drop_table("link_author_subsystem")

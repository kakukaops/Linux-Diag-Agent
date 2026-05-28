"""Add maintainer_section + maintainer_section_file from MAINTAINERS file.

The kernel's MAINTAINERS file is the authoritative source for who owns
which subsystem. Pre-v2.3 we approximated this with link_author_subsystem
(commit-count aggregation), which is good for credibility scoring but
weaker than the formal MAINTAINERS ownership.

Schema:
  maintainer_section          — one row per MAINTAINERS section
                                ("3C59X NETWORK DRIVER", "X86 ARCHITECTURE", ...)
  maintainer_section_person   — M: / R: lines (maintainer / reviewer)
  maintainer_section_file     — F: / X: / N: globs

All tables are TRUNCATEd + INSERTed at each ingest; idempotent.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintainer_section",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False, unique=True,
                   comment="Section title from MAINTAINERS"),
        sa.Column("status", sa.Text, nullable=True,
                   comment="S: line — Maintained / Supported / Orphan / Odd Fixes / etc."),
        sa.Column("mailing_list", sa.Text, nullable=True,
                   comment="L: line"),
        sa.Column("git_tree", sa.Text, nullable=True,
                   comment="T: line"),
    )

    op.create_table(
        "maintainer_section_person",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("section_id", sa.Integer,
                   sa.ForeignKey("maintainer_section.id"), nullable=False),
        sa.Column("role", sa.Text, nullable=False,
                   comment="'M' (maintainer) or 'R' (reviewer)"),
        sa.Column("name", sa.Text, nullable=True),
        sa.Column("email", sa.Text, nullable=True),
        sa.UniqueConstraint("section_id", "role", "email",
                             name="uq_msp"),
    )
    op.create_index("ix_msp_section", "maintainer_section_person", ["section_id"])
    op.create_index("ix_msp_email", "maintainer_section_person", ["email"])

    op.create_table(
        "maintainer_section_file",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("section_id", sa.Integer,
                   sa.ForeignKey("maintainer_section.id"), nullable=False),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False,
                   comment="'F' (include glob), 'X' (exclude glob), 'N' (regex)"),
        sa.UniqueConstraint("section_id", "pattern", "kind",
                             name="uq_msf"),
    )
    op.create_index("ix_msf_section", "maintainer_section_file", ["section_id"])
    op.create_index("ix_msf_pattern", "maintainer_section_file", ["pattern"])


def downgrade() -> None:
    op.drop_index("ix_msf_pattern", table_name="maintainer_section_file")
    op.drop_index("ix_msf_section", table_name="maintainer_section_file")
    op.drop_table("maintainer_section_file")
    op.drop_index("ix_msp_email", table_name="maintainer_section_person")
    op.drop_index("ix_msp_section", table_name="maintainer_section_person")
    op.drop_table("maintainer_section_person")
    op.drop_table("maintainer_section")

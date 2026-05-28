"""Add stack_signature columns to bug + lkml_message.

Closes v2.3 KG gap #2: enable cross-table "find similar past crashes"
lookup. Previously `stack_signature` lived only on `syzbot_crash`; this
migration extends it to bug bodies and LKML messages so a new dmesg
report can be hashed once and matched against all three sub-graphs.

The signature column is text (32-char SHA-256 prefix) with a B-tree
index for equality lookup. Backfill is done by a separate Python step
(see kg.signature_backfill) — this migration is structure-only.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bug",
                   sa.Column("stack_signature", sa.Text, nullable=True,
                             comment="32-char SHA-256 prefix of normalized "
                                     "kernel stack trace extracted from the "
                                     "bug body; NULL if no plausible trace"))
    op.add_column("lkml_message",
                   sa.Column("stack_signature", sa.Text, nullable=True,
                             comment="32-char SHA-256 prefix of normalized "
                                     "kernel stack trace from message body; "
                                     "NULL if message has no trace"))
    op.create_index("ix_bug_stack_sig", "bug", ["stack_signature"])
    op.create_index("ix_lkml_message_stack_sig", "lkml_message",
                     ["stack_signature"])


def downgrade() -> None:
    op.drop_index("ix_lkml_message_stack_sig", table_name="lkml_message")
    op.drop_index("ix_bug_stack_sig", table_name="bug")
    op.drop_column("lkml_message", "stack_signature")
    op.drop_column("bug", "stack_signature")

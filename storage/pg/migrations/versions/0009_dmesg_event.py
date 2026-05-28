"""Add dmesg_event table — persist parsed user input for symptom-signature lookup.

Closes v2.3 KG gap: previously user dmesg/sosreport input was parsed
in-memory by the agent and discarded after diagnosis. We had no way to
ask "have we seen this crash signature before?" because new inputs
weren't compared to anything.

This migration creates a node table that captures, per diagnostic
session: the raw input, the parsed fault_kind, kernel_version,
extracted call_trace, and its stack_signature. Once populated, the
ReAct tool `find_similar_crashes` can hash the incoming trace and
INTERSECT against:
  - dmesg_event (past user reports)
  - syzbot_crash.stack_signature (known syzbot crashes)
  - bug.stack_signature (post-0008 backfill)
  - lkml_message.stack_signature (post-0008 backfill)

so the LLM sees "this exact stack matched 3 historical reports — see X, Y, Z".

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmesg_event",
        sa.Column("event_id", sa.Text, primary_key=True,
                   comment="UUID or content-hash; assigned at ingest time"),
        sa.Column("ingested_at", sa.DateTime(timezone=True),
                   server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.Text, nullable=False,
                   comment="'user-input' | 'sosreport' | 'replay' | 'eval-case'"),
        sa.Column("fault_kind", sa.Text, nullable=True,
                   comment="Triage classification: oom/oops/lockup/panic/..."),
        sa.Column("kernel_version", sa.Text, nullable=True),
        sa.Column("olk_version_tag", sa.Text, nullable=True,
                   comment="'OLK-6.6' | 'OLK-5.10' | NULL"),
        sa.Column("raw_text", sa.Text, nullable=True,
                   comment="Full input text (may be large; truncate at write "
                           "time if needed)"),
        sa.Column("call_trace", sa.Text, nullable=True,
                   comment="Extracted call-trace block"),
        sa.Column("stack_signature", sa.Text, nullable=True,
                   comment="kg.signature.stack_signature(call_trace)"),
        sa.Column("metadata", sa.dialects.postgresql.JSONB, nullable=True,
                   comment="kernel events, hostname, kernel_events list, etc."),
    )
    op.create_index("ix_dmesg_event_signature", "dmesg_event", ["stack_signature"])
    op.create_index("ix_dmesg_event_kind", "dmesg_event", ["fault_kind"])
    op.create_index("ix_dmesg_event_ingested_at", "dmesg_event", ["ingested_at"])


def downgrade() -> None:
    op.drop_index("ix_dmesg_event_ingested_at", table_name="dmesg_event")
    op.drop_index("ix_dmesg_event_kind", table_name="dmesg_event")
    op.drop_index("ix_dmesg_event_signature", table_name="dmesg_event")
    op.drop_table("dmesg_event")

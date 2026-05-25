"""Add agent_tool_trace table (M22 T-012).

Stores per-step tool call records from the ReAct investigation loop.
Enables post-mortem auditing of what the agent did and why.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tool_trace",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args_json", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_before", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index("idx_att_thread_id", "agent_tool_trace", ["thread_id"])


def downgrade() -> None:
    op.drop_index("idx_att_thread_id", table_name="agent_tool_trace")
    op.drop_table("agent_tool_trace")

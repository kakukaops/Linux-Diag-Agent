"""Add fault_taxonomy + link_commit_fault_domain tables.

Closes v2.3 KG gap #3: the fault-domain edge that lets retrieval ask
"show me recent OOM-related commits" or "RCU stall fixes in net/" —
queries an experienced engineer asks but our v2.2 graph could not
answer without re-running BM25 on body text every time.

fault_taxonomy is a small flat catalog (one row per SOP fault_kind);
link_commit_fault_domain stores the (commit, domain) edges with
source + confidence so consumers can filter by provenance.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


_SEED_DOMAINS = [
    ("oom",            "Out-of-memory kills"),
    ("panic",          "Kernel panic"),
    ("softlockup",     "CPU soft lockup"),
    ("hardlockup",     "CPU hard lockup (NMI watchdog)"),
    ("rcu_stall",      "RCU sched / preempt stall"),
    ("io_hang",        "Hung task / blocked on I/O"),
    ("deadlock",       "Lockdep / lock-ordering deadlock"),
    ("network",        "Networking subsystem fault"),
    ("perf_regression", "Performance regression after upgrade"),
    ("sched_anomaly",  "Scheduler / wakeup anomaly"),
    ("hardware",       "Hardware-origin fault (MCE/EDAC/firmware)"),
]


def upgrade() -> None:
    op.create_table(
        "fault_taxonomy",
        sa.Column("domain", sa.Text, primary_key=True,
                   comment="Canonical fault domain name (matches SOP fault_kinds)"),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("parent", sa.Text, nullable=True,
                   comment="Optional parent domain for hierarchy"),
    )

    # Seed the taxonomy
    fault_taxonomy = sa.table(
        "fault_taxonomy",
        sa.column("domain", sa.Text),
        sa.column("description", sa.Text),
        sa.column("parent", sa.Text),
    )
    op.bulk_insert(fault_taxonomy, [
        {"domain": d, "description": desc, "parent": None}
        for d, desc in _SEED_DOMAINS
    ])

    op.create_table(
        "link_commit_fault_domain",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("commit_hash", sa.Text,
                   sa.ForeignKey("kernel_commit.hash"), nullable=False),
        sa.Column("domain", sa.Text,
                   sa.ForeignKey("fault_taxonomy.domain"), nullable=False),
        sa.Column("source", sa.Text, nullable=False,
                   comment="'subject-keyword' | 'body-keyword' | 'sop-mapping' | 'llm-inferred'"),
        sa.Column("confidence", sa.Numeric(3, 2), server_default="0.7",
                   comment="0-1; subject hits = 0.8, body-only = 0.6, llm = ≤0.5"),
        sa.UniqueConstraint("commit_hash", "domain", name="uq_lcfd"),
    )
    op.create_index("ix_lcfd_domain", "link_commit_fault_domain", ["domain"])
    op.create_index("ix_lcfd_commit", "link_commit_fault_domain", ["commit_hash"])


def downgrade() -> None:
    op.drop_index("ix_lcfd_commit", table_name="link_commit_fault_domain")
    op.drop_index("ix_lcfd_domain", table_name="link_commit_fault_domain")
    op.drop_table("link_commit_fault_domain")
    op.drop_table("fault_taxonomy")

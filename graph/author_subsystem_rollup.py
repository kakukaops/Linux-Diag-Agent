"""Rebuild link_author_subsystem from kernel_commit (single SQL).

Derived table — wipe + recompute. Run after weekly kernel_commit ingest.

  python -m graph.author_subsystem_rollup
"""

from __future__ import annotations

import logging
import sys


def rebuild(engine) -> int:
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE link_author_subsystem"))
        conn.execute(text("""
            INSERT INTO link_author_subsystem
                (author_email, author_name, subsystem,
                 commit_count, first_commit_date, last_commit_date)
            SELECT author_email,
                   MIN(author_name) AS author_name,
                   subsystem,
                   count(*) AS n,
                   MIN(commit_date),
                   MAX(commit_date)
              FROM kernel_commit
             WHERE author_email IS NOT NULL
               AND author_email <> ''
               AND subsystem IS NOT NULL
             GROUP BY author_email, subsystem
            HAVING count(*) >= 3          -- noise floor: drop one-offs
        """))
        n = conn.execute(text(
            "SELECT count(*) FROM link_author_subsystem"
        )).scalar() or 0
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")
    from storage.pg.engine import get_engine
    n = rebuild(get_engine())
    print(f"link_author_subsystem rebuilt: {n:,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

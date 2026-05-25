"""Entry point:

  python -m ingest.lkml incremental            # bulk by list+date (offline-mode snapshot)
  python -m ingest.lkml backfill_referenced    # ADR-025 L1: fetch commit-referenced messages
"""

from __future__ import annotations

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from ingest.lkml.ingester import LkmlIngester


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    if cmd == "incremental":
        report = LkmlIngester().run()
        sys.exit(0 if report.status == "ok" else 1)
    elif cmd == "backfill_referenced":
        from ingest.lkml.backfill import backfill_referenced
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        backfill_referenced(limit=limit)
        sys.exit(0)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""CLI entry point for the link_commit_symbol backfill (v2.3 KG gap #1).

Usage:
    python -m ingest.kernel_commit.backfill_symbols \
        --olk-6.6 /data1/lingqu/codes/OLK-6.6/kernel \
        --olk-5.10 /data1/lingqu/codes/OLK-5.10/kernel \
        --since 2023-01-01 \
        --batch-size 500

Idempotent — re-running picks up where the last run left off (commits
already in link_commit_symbol are skipped via WHERE NOT EXISTS).
"""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--olk-6.6", dest="olk_66",
                    default="/data1/lingqu/codes/OLK-6.6/kernel")
    ap.add_argument("--olk-5.10", dest="olk_510",
                    default="/data1/lingqu/codes/OLK-5.10/kernel")
    ap.add_argument("--since", default=None,
                    help="ISO date (YYYY-MM-DD). Prioritise commits since.")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--max-batches", type=int, default=None,
                    help="Cap on iterations (for testing). Default: unbounded.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from ingest.kernel_commit.symbol_extractor import run_backfill_loop

    repos = {
        "OLK-6.6":  args.olk_66,
        "OLK-5.10": args.olk_510,
    }
    totals = run_backfill_loop(
        repos=repos,
        batch_size=args.batch_size,
        since_date=args.since,
        max_batches=args.max_batches,
    )
    print(f"\nDone. Processed {totals['processed']:,} commits, "
          f"inserted {totals['rows']:,} symbol rows, "
          f"skipped {totals['skipped']:,}, "
          f"{totals['batches']} batches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

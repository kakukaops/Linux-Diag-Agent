"""Entry point: python -m ingest.lkml incremental"""

from __future__ import annotations

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from ingest.lkml.ingester import LkmlIngester


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    ingester = LkmlIngester()
    if cmd == "incremental":
        report = ingester.run()
        sys.exit(0 if report.status == "ok" else 1)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

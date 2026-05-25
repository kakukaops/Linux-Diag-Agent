"""CLI: python -m ingest.gitee backfill [--limit N] [--token TOKEN]"""

from __future__ import annotations

import logging
import os
import sys

import click


@click.group()
def cli() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


@cli.command()
@click.option("--limit", default=None, type=int,
              help="Cap number of issues fetched (for testing).")
@click.option("--token", default=lambda: os.environ.get("GITEE_TOKEN"),
              help="Optional gitee PAT — raises rate limit from 60 to higher.")
def backfill(limit: int | None, token: str | None) -> None:
    """Reference-driven backfill of gitee issues cited by OLK commits."""
    from ingest.gitee.backfill import backfill as run
    report = run(limit=limit, token=token)
    print(f"\nDone: fetched={report['fetched']}  not_found={report['not_found']}  "
          f"server_error={report['server_error']}  errors={report['errors']}  "
          f"cited_total={report['total_cited']}")
    sys.exit(0 if report["errors"] == 0 else 1)


if __name__ == "__main__":
    cli()

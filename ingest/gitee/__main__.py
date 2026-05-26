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


def _resolve_token(cli_token: str | None) -> str | None:
    """Resolve gitee token by priority: CLI flag > env var > configs/local.yaml."""
    if cli_token:
        return cli_token
    env = os.environ.get("GITEE_TOKEN")
    if env:
        return env
    try:
        from configs.config import get_config
        tok = (get_config().ingestion.gitee.token or "").strip()
        if tok and tok != "PASTE_YOUR_GITEE_TOKEN_HERE":
            return tok
    except Exception:
        pass
    return None


@cli.command()
@click.option("--limit", default=None, type=int,
              help="Cap number of issues fetched (for testing).")
@click.option("--token", default=None,
              help="Optional gitee PAT. Falls back to GITEE_TOKEN env var, "
                   "then configs/local.yaml ingestion.gitee.token.")
def backfill(limit: int | None, token: str | None) -> None:
    """Reference-driven backfill of gitee issues cited by OLK commits."""
    from ingest.gitee.backfill import backfill as run
    resolved = _resolve_token(token)
    if resolved:
        print(f"[gitee] using token (suffix=...{resolved[-4:]})")
    else:
        print("[gitee] no token — anonymous 60/min, expect throttling")
    report = run(limit=limit, token=resolved)
    print(f"\nDone: fetched={report['fetched']}  not_found={report['not_found']}  "
          f"server_error={report['server_error']}  errors={report['errors']}  "
          f"cited_total={report['total_cited']}")
    sys.exit(0 if report["errors"] == 0 else 1)


if __name__ == "__main__":
    cli()

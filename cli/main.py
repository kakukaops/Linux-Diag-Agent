"""CLI entry point for diag-agent (WBS 3.11)."""

from __future__ import annotations

import json
import logging
import sys
import time

import click

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
def cli(debug: bool) -> None:
    """Linux kernel fault diagnosis agent."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)


# ── search subcommand ─────────────────────────────────────────────────────────


@cli.command("search")
@click.argument("question")
@click.option("--version", "-v", default=None, help="Kernel version hint (e.g. OLK-6.6).")
@click.option("--limit", "-n", default=10, show_default=True, help="Max results per route.")
@click.option(
    "--routes",
    default=None,
    help="Comma-separated route list (code,docs,lkml,bug,syzbot,commit,cve). Default: all.",
)
@click.option("--json-output", "json_output", is_flag=True, help="Output raw JSON.")
@click.option("--no-llm", is_flag=True, help="Skip LLM query parsing (regex only).")
@click.option(
    "--metrics-file",
    default=None,
    help="Write latency metrics JSON to this file.",
)
def search_cmd(
    question: str,
    version: str | None,
    limit: int,
    routes: str | None,
    json_output: bool,
    no_llm: bool,
    metrics_file: str | None,
) -> None:
    """Search the knowledge base with QUESTION."""
    from retrieval.query_parser import parse_query
    from retrieval.engine import retrieve
    from retrieval.schema import RouteTag

    t0 = time.monotonic()

    # Parse query
    query = parse_query(question, use_llm=not no_llm)
    if version:
        query.kernel_version = version
    if limit:
        query.limit_per_route = limit
    if routes:
        route_names = [r.strip() for r in routes.split(",")]
        query.routes = [RouteTag(r) for r in route_names]

    # Retrieve
    result = retrieve(query)
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    # Metrics
    metrics = {
        "total_ms": elapsed_ms,
        "item_count": len(result.items),
        "reranked": result.reranked,
        "latency_per_route": result.latency_ms,
    }
    if metrics_file:
        import pathlib
        pathlib.Path(metrics_file).write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )

    # Output
    if json_output:
        click.echo(json.dumps(
            {
                "query": query.model_dump(),
                "items": [e.model_dump() for e in result.items],
                "metrics": metrics,
            },
            indent=2,
            default=str,
        ))
        return

    # Human-readable table
    click.echo(f"\nQuery: {question}")
    if query.kernel_version:
        click.echo(f"Version: {query.kernel_version}")
    click.echo(f"Found {len(result.items)} results in {elapsed_ms}ms"
               f"{' (reranked)' if result.reranked else ''}\n")

    for i, ev in enumerate(result.items, 1):
        click.echo(f"[{i:2d}] [{ev.route.value:8s}] score={ev.score:.3f}")
        click.echo(f"      {ev.title}")
        if ev.body:
            preview = ev.body[:120].replace("\n", " ")
            click.echo(f"      {preview}…")
        click.echo()


# ── version subcommand ────────────────────────────────────────────────────────


@cli.command("version")
def version_cmd() -> None:
    """Print diag-agent version."""
    try:
        from importlib.metadata import version
        click.echo(version("linux-diag-agent"))
    except Exception:
        click.echo("dev")


if __name__ == "__main__":
    cli()

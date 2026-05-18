"""Observability CLI commands (ADR-016 D5 / WBS 12.2).

Provides:
  diag-agent logs query    — grep/filter JSONL log files
  diag-agent metrics show  — display metrics JSON snapshots
  diag-agent trace view    — render a trace (by trace_id) as ASCII tree
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click


@click.group("logs")
def logs_group() -> None:
    """Query JSONL log files."""


@click.group("metrics")
def metrics_group() -> None:
    """Display file-based metrics snapshots."""


@click.group("trace")
def trace_group() -> None:
    """Inspect OpenTelemetry JSONL trace files."""


# ── logs query ────────────────────────────────────────────────────────────────


@logs_group.command("query")
@click.option("--module", default=None, help="Log module (e.g. llm, ingest, agent).")
@click.option("--trace-id", default=None, help="Filter by trace_id.")
@click.option("--since", default="24h", show_default=True,
              help="Time window: e.g. 1h, 6h, 24h, 7d.")
@click.option("--level", default=None, type=click.Choice(["ERROR", "WARN", "INFO", "DEBUG"]),
              help="Minimum log level to show.")
@click.option("--grep", default=None, help="Regex to filter log entries.")
@click.option("--limit", default=100, show_default=True, help="Max entries to print.")
@click.option("--json-output", "json_out", is_flag=True, help="Output raw JSON lines.")
def logs_query(
    module: str | None,
    trace_id: str | None,
    since: str,
    level: str | None,
    grep: str | None,
    limit: int,
    json_out: bool,
) -> None:
    """Query JSONL log files."""
    from configs.config import get_config
    cfg = get_config()
    log_dir = Path(cfg.observability.log_dir)
    cutoff = _parse_since(since)

    pattern = re.compile(grep, re.IGNORECASE) if grep else None
    entries: list[dict[str, Any]] = []

    # Scan date directories
    for date_dir in sorted(log_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.fromisoformat(date_dir.name).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dir_date < cutoff - timedelta(days=1):
            break

        # Pick module file(s)
        if module:
            files = [date_dir / f"{module}.jsonl"]
        else:
            files = list(date_dir.glob("*.jsonl"))

        for f in files:
            if not f.exists():
                continue
            for line in f.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = entry.get("ts") or entry.get("timestamp") or ""
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                if trace_id and entry.get("trace_id") != trace_id:
                    continue
                if level and _level_rank(entry.get("level", "INFO")) < _level_rank(level):
                    continue
                if pattern and not pattern.search(line):
                    continue
                entries.append(entry)
                if len(entries) >= limit * 2:
                    break

    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    entries = entries[:limit]

    if json_out:
        for e in entries:
            click.echo(json.dumps(e))
    else:
        for e in entries:
            _print_log_entry(e)

    click.echo(f"\n({len(entries)} entries)", err=True)


# ── metrics show ──────────────────────────────────────────────────────────────


@metrics_group.command("show")
@click.option("--module", default=None, help="Module name (e.g. llm, ingest_health, sync_audit).")
@click.option("--field", default=None, help="Specific field to extract.")
@click.option("--json-output", "json_out", is_flag=True, help="Output raw JSON.")
def metrics_show(module: str | None, field: str | None, json_out: bool) -> None:
    """Display metrics JSON snapshots from data/metrics/."""
    from configs.config import get_config
    cfg = get_config()
    metrics_dir = Path(cfg.app.data_dir) / "metrics"

    if not metrics_dir.exists():
        click.echo("No metrics directory found.", err=True)
        return

    files = list(metrics_dir.glob("*.json"))
    if module:
        files = [f for f in files if module in f.stem]

    if not files:
        click.echo(f"No metrics files found for module={module!r}", err=True)
        return

    for f in sorted(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            click.echo(f"Error reading {f}: {exc}", err=True)
            continue

        if field:
            value = _deep_get(data, field)
            click.echo(f"{f.stem}.{field} = {value}")
            continue

        if json_out:
            click.echo(json.dumps({f.stem: data}, indent=2))
        else:
            click.echo(f"\n── {f.stem} ──")
            _print_metrics(data, indent=2)


# ── trace view ────────────────────────────────────────────────────────────────


@trace_group.command("view")
@click.argument("trace_id")
@click.option("--since", default="7d", show_default=True)
def trace_view(trace_id: str, since: str) -> None:
    """Render a trace as an ASCII tree by trace_id."""
    from configs.config import get_config
    cfg = get_config()
    traces_dir = Path(cfg.app.data_dir) / "traces"
    cutoff = _parse_since(since)

    spans: list[dict[str, Any]] = []
    if traces_dir.exists():
        for f in traces_dir.glob(f"{trace_id}*.jsonl"):
            for line in f.read_text(errors="replace").splitlines():
                try:
                    spans.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Also search JSONL logs for trace_id
    if not spans:
        log_dir = Path(cfg.observability.log_dir)
        for date_dir in sorted(log_dir.iterdir(), reverse=True)[:7]:
            if not date_dir.is_dir():
                continue
            for f in date_dir.glob("*.jsonl"):
                for line in f.read_text(errors="replace").splitlines():
                    try:
                        e = json.loads(line)
                        if e.get("trace_id") == trace_id:
                            spans.append(e)
                    except json.JSONDecodeError:
                        pass

    if not spans:
        click.echo(f"No spans found for trace_id={trace_id!r}", err=True)
        return

    spans.sort(key=lambda s: s.get("ts", s.get("timestamp", "")))
    _render_trace_tree(trace_id, spans)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_since(since: str) -> datetime:
    """Parse '1h', '24h', '7d' → datetime cutoff."""
    now = datetime.now(timezone.utc)
    if since.endswith("h"):
        return now - timedelta(hours=int(since[:-1]))
    if since.endswith("d"):
        return now - timedelta(days=int(since[:-1]))
    return now - timedelta(hours=24)


_LEVEL_RANKS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "WARNING": 2, "ERROR": 3}

def _level_rank(level: str) -> int:
    return _LEVEL_RANKS.get(level.upper(), 1)


def _print_log_entry(e: dict[str, Any]) -> None:
    ts = e.get("ts", "")[:19]
    level = e.get("level", "INFO")[:5]
    msg = e.get("message") or e.get("msg") or json.dumps(e)[:120]
    tid = e.get("trace_id", "")
    prefix = f"{ts} [{level}]"
    if tid:
        prefix += f" tid={tid[:8]}"
    click.echo(f"{prefix} {msg}")


def _print_metrics(data: Any, indent: int = 0) -> None:
    pad = " " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                click.echo(f"{pad}{k}:")
                _print_metrics(v, indent + 2)
            else:
                click.echo(f"{pad}{k}: {v}")
    elif isinstance(data, list):
        for i, item in enumerate(data[:10]):
            click.echo(f"{pad}[{i}] {item}")
    else:
        click.echo(f"{pad}{data}")


def _deep_get(data: Any, field: str) -> Any:
    """Dot-notation field lookup."""
    parts = field.split(".")
    cur = data
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _render_trace_tree(trace_id: str, spans: list[dict[str, Any]]) -> None:
    click.echo(f"\nTrace: {trace_id}")
    click.echo(f"{'─'*60}")
    for i, span in enumerate(spans):
        ts = span.get("ts", "")[:19]
        name = span.get("name") or span.get("module") or span.get("provider") or "span"
        dur = span.get("latency_ms") or span.get("duration_ms") or ""
        dur_str = f" ({dur}ms)" if dur else ""
        level = span.get("level", "")
        icon = "✗" if level in ("ERROR", "WARN") else "·"
        prefix = "└─" if i == len(spans) - 1 else "├─"
        click.echo(f"  {prefix} [{ts}] {icon} {name}{dur_str}")
    click.echo(f"{'─'*60}\n({len(spans)} spans)")

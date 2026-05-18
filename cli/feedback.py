"""User feedback loop CLI (WBS 12.1).

Allows users to annotate diagnosis outputs as correct/incorrect and
optionally provide the true root cause. Feedback is stored in the
`diagnosis_feedback` PG table for future fine-tuning data.

Usage: diag-agent feedback annotate <report_json_path>
"""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.group("feedback")
def feedback_group() -> None:
    """Manage diagnosis feedback and annotation data."""


@feedback_group.command("annotate")
@click.argument("report_json", type=click.Path(exists=True))
@click.option("--correct/--incorrect", default=None, required=True,
              help="Was the diagnosis correct?")
@click.option("--true-cause", default=None, help="Actual root cause (if known).")
@click.option("--notes", default="", help="Free-form annotation notes.")
@click.option("--annotator", default=None, help="Annotator name/ID.")
def annotate_cmd(
    report_json: str,
    correct: bool | None,
    true_cause: str | None,
    notes: str,
    annotator: str | None,
) -> None:
    """Annotate a diagnosis report JSON with correctness feedback."""
    from storage.pg.engine import get_engine
    from sqlalchemy import text

    report = json.loads(Path(report_json).read_text(encoding="utf-8"))
    fault = report.get("fault", {})
    system = report.get("system", {})

    engine = get_engine()
    _ensure_table(engine)

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO diagnosis_feedback
                       (fault_kind, fault_summary, olk_version_tag, hostname,
                        is_correct, true_cause, annotator_notes, annotator,
                        report_json)
                VALUES (:fk, :fs, :olk, :host, :ic, :tc, :notes, :ann, :rj)
            """),
            {
                "fk": fault.get("kind"),
                "fs": fault.get("summary"),
                "olk": system.get("olk_version_tag"),
                "host": system.get("hostname"),
                "ic": correct,
                "tc": true_cause,
                "notes": notes,
                "ann": annotator,
                "rj": json.dumps(report),
            },
        )
    click.echo(f"Feedback recorded. correct={correct}, true_cause={true_cause or 'N/A'}")


@feedback_group.command("export")
@click.option("--output", required=True, help="Output JSONL file for training data.")
@click.option("--only-correct", is_flag=True, help="Export only confirmed-correct diagnoses.")
def export_cmd(output: str, only_correct: bool) -> None:
    """Export annotated feedback as JSONL for fine-tuning."""
    from storage.pg.engine import get_engine
    from sqlalchemy import text

    engine = get_engine()
    where = "WHERE is_correct = TRUE" if only_correct else ""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT fault_kind, fault_summary, olk_version_tag,
                   is_correct, true_cause, report_json, created_at
              FROM diagnosis_feedback
              {where}
             ORDER BY created_at DESC
        """)).fetchall()

    out = Path(output)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            entry = {
                "fault_kind": row[0],
                "fault_summary": row[1],
                "olk_version_tag": row[2],
                "is_correct": row[3],
                "true_cause": row[4],
                "report": json.loads(row[5]) if row[5] else {},
                "created_at": str(row[6]),
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    click.echo(f"Exported {len(rows)} feedback records to {out}")


def _ensure_table(engine: object) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:  # type: ignore[union-attr]
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS diagnosis_feedback (
                id              SERIAL PRIMARY KEY,
                fault_kind      TEXT,
                fault_summary   TEXT,
                olk_version_tag TEXT,
                hostname        TEXT,
                is_correct      BOOLEAN,
                true_cause      TEXT,
                annotator_notes TEXT,
                annotator       TEXT,
                report_json     JSONB,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """))

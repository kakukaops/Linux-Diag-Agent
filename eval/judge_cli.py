"""Interactive judge CLI for manual evaluation (WBS 7.15).

Presents each eval result to a human judge who rates:
  A (root-cause correct): 1 (fully correct) | 0.5 (partial) | 0 (wrong)
  R (report readability): 1-10

Usage:
  python -m eval.judge_cli --results eval/results/ --judge alice
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


@click.command()
@click.option("--results", required=True, type=click.Path(exists=True),
              help="Directory with per-case result JSON files.")
@click.option("--judge", required=True, help="Judge name/ID (e.g. alice, bob).")
@click.option("--output", default=None, help="Output file for judgements (default: <results>/judge_<name>.json).")
@click.option("--skip-judged", is_flag=True, help="Skip cases already judged by this judge.")
def main(results: str, judge: str, output: str | None, skip_judged: bool) -> None:
    """Interactively judge eval results."""
    results_dir = Path(results)
    out_path = Path(output) if output else results_dir / f"judge_{judge}.json"

    # Load existing judgements
    existing: dict[str, dict] = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))

    case_files = sorted(results_dir.glob("*.json"))
    case_files = [f for f in case_files if not f.name.startswith(("judge_", "summary"))]

    judgements: dict = dict(existing)
    judged = 0

    for case_file in case_files:
        case_id = case_file.stem
        if skip_judged and case_id in existing:
            continue

        result = json.loads(case_file.read_text(encoding="utf-8"))
        _display_case(result)

        try:
            a_score = _prompt_float("Root-cause correct (A)? [1=correct, 0.5=partial, 0=wrong, s=skip]: ",
                                    choices={"1": 1.0, "0.5": 0.5, "0": 0.0, "s": None})
            if a_score is None:
                click.echo("Skipped.\n")
                continue
            r_score = _prompt_int("Readability (R)? [1-10]: ", lo=1, hi=10)
            notes = click.prompt("Notes (optional)", default="")
        except (KeyboardInterrupt, EOFError):
            click.echo("\nInterrupted. Saving progress.")
            break

        judgements[case_id] = {
            "judge": judge,
            "a_score": a_score,
            "r_score": r_score,
            "notes": notes,
        }
        judged += 1
        out_path.write_text(json.dumps(judgements, indent=2), encoding="utf-8")
        click.echo(f"Saved. {len(judgements)}/{len(case_files)} judged.\n")

    click.echo(f"\nDone. Judged {judged} new cases. Total: {len(judgements)}. Output: {out_path}")


def _display_case(result: dict) -> None:
    click.echo(f"\n{'='*60}")
    click.echo(f"Case: {result.get('case_id')} | Fault: {result.get('fault_kind')} | SOP: {result.get('sop_name')}")
    click.echo(f"Q: {result.get('question','')[:120]}")
    click.echo(f"Recall@10: {result.get('recall_at_10',0):.1%}  Traceability: {result.get('traceability',0):.1%}")
    click.echo(f"Evidence count: {result.get('evidence_count',0)}")
    click.echo(f"\nReport preview:\n{result.get('report_md','')[:400]}")
    click.echo(f"{'='*60}")


def _prompt_float(msg: str, choices: dict[str, float | None]) -> float | None:
    while True:
        val = click.prompt(msg).strip()
        if val in choices:
            return choices[val]
        click.echo(f"Invalid. Choose from {list(choices.keys())}")


def _prompt_int(msg: str, lo: int, hi: int) -> int:
    while True:
        try:
            val = int(click.prompt(msg).strip())
            if lo <= val <= hi:
                return val
        except ValueError:
            pass
        click.echo(f"Enter a number between {lo} and {hi}")


if __name__ == "__main__":
    main()

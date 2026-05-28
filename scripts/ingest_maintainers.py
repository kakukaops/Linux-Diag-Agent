"""Parse kernel MAINTAINERS file → maintainer_section* tables.

The file format (described in MAINTAINERS preamble itself):

  SECTION TITLE
  M:	Name <email>           # maintainer
  R:	Name <email>           # reviewer
  L:	mailing-list@addr      # mailing list
  S:	Maintained|Supported|Orphan|Odd Fixes|...
  T:	git URL                # git tree
  F:	path/glob              # files (include)
  X:	path/glob              # files (exclude)
  N:	regex                  # name regex

Sections are separated by blank lines. The first non-blank line of a
section is the title.

Tables are TRUNCATEd before insert — this is a derived materialisation,
not an incremental ingest.

Usage:
  python scripts/ingest_maintainers.py \
    --maintainers /data1/lingqu/codes/OLK-6.6/kernel/MAINTAINERS
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


_PERSON_RE = re.compile(r"^(?P<name>[^<]+?)\s*<(?P<email>[^>]+)>\s*$")
_FIELD_RE = re.compile(r"^([A-Z]):\s+(.+?)\s*$")


@dataclass
class Section:
    name: str
    status: str | None = None
    mailing_list: str | None = None
    git_tree: str | None = None
    persons: list[tuple[str, str | None, str | None]] = field(default_factory=list)  # (role, name, email)
    files: list[tuple[str, str]] = field(default_factory=list)                       # (kind, pattern)


def parse(path: Path) -> list[Section]:
    """Parse MAINTAINERS into a list of Section objects."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    # Skip header (everything before the first "Maintainers List" marker line,
    # or before the first all-caps section title following a blank line).
    in_list = False
    sections: list[Section] = []
    cur: Section | None = None

    for line in lines:
        stripped = line.rstrip()

        # Detect start of the actual section list — we look for a stable
        # marker: the dashed underline that follows the "Maintainers List"
        # heading in the file.
        if not in_list:
            if stripped.startswith("----") or "Maintainers List" in stripped:
                in_list = True
            continue

        # Blank line ⇒ section boundary
        if not stripped:
            if cur is not None and cur.name:
                sections.append(cur)
            cur = None
            continue

        # Field line (`M:`, `F:`, `L:`, ...)
        m = _FIELD_RE.match(line)
        if m and cur is not None:
            tag, value = m.group(1), m.group(2)
            _consume_field(cur, tag, value)
            continue

        # Otherwise this is a section title.
        # Guard: the "----" underline after "Maintainers List" can leak into
        # a section title if in_list flips True on the heading line itself.
        # A title must contain at least one alnum char.
        if cur is None:
            title = stripped.strip()
            if not any(ch.isalnum() for ch in title):
                continue
            cur = Section(name=title)

    if cur is not None and cur.name:
        sections.append(cur)
    return sections


def _consume_field(sec: Section, tag: str, value: str) -> None:
    if tag in ("M", "R"):
        m = _PERSON_RE.match(value)
        if m:
            sec.persons.append((tag, m.group("name").strip(), m.group("email").strip().lower()))
        else:
            # Sometimes M:/R: has just a name without email
            sec.persons.append((tag, value.strip(), None))
    elif tag == "L":
        sec.mailing_list = value
    elif tag == "S":
        sec.status = value
    elif tag == "T":
        # may have multiple T: lines; keep the first
        if sec.git_tree is None:
            sec.git_tree = value
    elif tag in ("F", "X", "N"):
        sec.files.append((tag, value))


def ingest(engine, sections: list[Section]) -> dict:
    from sqlalchemy import text

    # OLK kernel's MAINTAINERS file sometimes redeclares the same section
    # (e.g. "HUAWEI ETHERNET DRIVER" appears multiple times to add fields
    # incrementally). Merge by name before insert.
    merged: dict[str, Section] = {}
    for sec in sections:
        if not sec.name:
            continue
        existing = merged.get(sec.name)
        if existing is None:
            merged[sec.name] = sec
        else:
            existing.status = existing.status or sec.status
            existing.mailing_list = existing.mailing_list or sec.mailing_list
            existing.git_tree = existing.git_tree or sec.git_tree
            existing.persons.extend(sec.persons)
            existing.files.extend(sec.files)
    sections = list(merged.values())

    stats = {"sections": 0, "persons": 0, "files": 0, "dupes_merged": len(merged) - sum(1 for _ in merged)}
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE maintainer_section_file, "
                           "maintainer_section_person, "
                           "maintainer_section RESTART IDENTITY"))
        for sec in sections:
            if not sec.name:
                continue
            row = conn.execute(text("""
                INSERT INTO maintainer_section
                    (name, status, mailing_list, git_tree)
                VALUES (:n, :s, :l, :t)
                RETURNING id
            """), {"n": sec.name[:200],
                    "s": sec.status,
                    "l": sec.mailing_list,
                    "t": sec.git_tree}).fetchone()
            sid = row[0]
            stats["sections"] += 1

            # Dedup people by (role, email) — some sections list the same
            # maintainer under multiple lines.
            seen_persons: set[tuple[str, str | None]] = set()
            for role, name, email in sec.persons:
                key = (role, email)
                if key in seen_persons:
                    continue
                seen_persons.add(key)
                conn.execute(text("""
                    INSERT INTO maintainer_section_person
                        (section_id, role, name, email)
                    VALUES (:sid, :r, :n, :e)
                    ON CONFLICT ON CONSTRAINT uq_msp DO NOTHING
                """), {"sid": sid, "r": role, "n": name, "e": email})
                stats["persons"] += 1

            seen_files: set[tuple[str, str]] = set()
            for kind, pattern in sec.files:
                if (kind, pattern) in seen_files:
                    continue
                seen_files.add((kind, pattern))
                conn.execute(text("""
                    INSERT INTO maintainer_section_file
                        (section_id, kind, pattern)
                    VALUES (:sid, :k, :p)
                    ON CONFLICT ON CONSTRAINT uq_msf DO NOTHING
                """), {"sid": sid, "k": kind, "p": pattern})
                stats["files"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--maintainers",
                    default="/data1/lingqu/codes/OLK-6.6/kernel/MAINTAINERS",
                    help="path to MAINTAINERS file")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    p = Path(args.maintainers)
    if not p.exists():
        logger.error("not found: %s", p)
        return 1

    sections = parse(p)
    logger.info("parsed %d sections from %s", len(sections), p)

    from storage.pg.engine import get_engine
    stats = ingest(get_engine(), sections)
    print()
    print("=== MAINTAINERS ingest ===")
    for k, v in stats.items():
        print(f"  {k:<14} {v:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

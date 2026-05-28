"""Ingest Linux Kernel CVE Project — kernel-team-maintained CVE → commit map.

The project lives at git.kernel.org/pub/scm/linux/security/vulns.git and
maintains, per CVE: a `.sha1` file with the upstream fix commit hash,
a `.dyad` file with per-stable-version (vulnerable, fixed) pairs, a
`.json` CVE 5.x record, and a `.mbox` of the announcement email.

Closes v2.3 KG gap: previously only NVD references were parsed for
cve.fix_commits, yielding 17% coverage (2,622 / 15,502 kernel CVEs).
The kernel project covers ~12K CVEs with a .sha1 each → projected
coverage > 80%.

Usage:
  python scripts/ingest_kernel_cve_project.py \
    --repo /tmp/vulns_probe \
    [--clone-if-missing] [--refresh]

  Then re-run `python -c "from graph.linker import link_nvd_commits;
  from storage.pg.engine import get_engine; link_nvd_commits(get_engine())"`
  to materialize link_commit_cve edges.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_VULNS_REPO_URL = "https://git.kernel.org/pub/scm/linux/security/vulns.git"


def ensure_repo(repo_path: Path, clone_if_missing: bool, refresh: bool) -> bool:
    """Make sure the vulns.git checkout exists and (optionally) is up-to-date."""
    if not repo_path.exists():
        if not clone_if_missing:
            logger.error("repo not found at %s; pass --clone-if-missing", repo_path)
            return False
        logger.info("cloning %s → %s", _VULNS_REPO_URL, repo_path)
        r = subprocess.run(
            ["git", "clone", "--depth", "1", _VULNS_REPO_URL, str(repo_path)],
            capture_output=True,
        )
        if r.returncode != 0:
            logger.error("clone failed: %s", r.stderr.decode("utf-8", errors="replace"))
            return False
        return True
    if refresh:
        logger.info("git pull on %s", repo_path)
        r = subprocess.run(["git", "-C", str(repo_path), "pull"],
                            capture_output=True)
        if r.returncode != 0:
            logger.warning("git pull non-zero: %s",
                           r.stderr.decode("utf-8", errors="replace"))
    return True


def walk_published_cves(repo_path: Path):
    """Yield (cve_id, fix_sha, dyad_pairs|None) from each published CVE."""
    pub = repo_path / "cve" / "published"
    if not pub.is_dir():
        raise FileNotFoundError(f"expected {pub} to exist")
    for year_dir in sorted(pub.iterdir()):
        if not year_dir.is_dir():
            continue
        for sha_file in sorted(year_dir.glob("*.sha1")):
            cve_id = sha_file.stem  # CVE-2024-26588
            try:
                sha = sha_file.read_text().strip()
            except Exception as exc:
                logger.warning("read failed %s: %s", sha_file, exc)
                continue
            if len(sha) < 8 or any(c not in "0123456789abcdef" for c in sha):
                continue   # garbage
            # Parse .dyad for stable backports
            dyad_file = sha_file.with_suffix(".dyad")
            backport_shas: list[str] = []
            if dyad_file.exists():
                for line in dyad_file.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) >= 4:
                        fix_sha = parts[3]
                        if (len(fix_sha) >= 8 and
                            all(c in "0123456789abcdef" for c in fix_sha) and
                            fix_sha != sha):
                            backport_shas.append(fix_sha)
            yield (cve_id, sha, backport_shas)


def merge_fix_commits(existing: list, new_shas: list[str]) -> list[str]:
    """Merge new SHAs into existing list, deduping by 12-char prefix.
    Existing SHAs (typically from NVD) are kept first; new SHAs append."""
    out: list[str] = list(existing or [])
    seen = {s.lower()[:12] for s in out if isinstance(s, str) and s}
    for sha in new_shas:
        if not sha:
            continue
        key = sha.lower()[:12]
        if key not in seen:
            seen.add(key)
            out.append(sha)
    return out


def ingest(engine, repo_path: Path) -> dict:
    """Walk all published CVEs and merge fix_commits into the cve table."""
    from sqlalchemy import text

    stats = {
        "cves_seen": 0,
        "cves_updated_existing": 0,
        "cves_inserted_new": 0,
        "shas_added": 0,
    }

    rows_to_upsert: list[dict] = []
    for cve_id, mainline_sha, backports in walk_published_cves(repo_path):
        stats["cves_seen"] += 1
        all_shas = [mainline_sha] + backports
        rows_to_upsert.append({"cve_id": cve_id, "shas": all_shas})

    logger.info("scanned %d CVEs from kernel.org vulns repo", stats["cves_seen"])

    CHUNK = 500
    with engine.begin() as conn:
        for i in range(0, len(rows_to_upsert), CHUNK):
            chunk = rows_to_upsert[i:i + CHUNK]
            for r in chunk:
                # Read existing
                row = conn.execute(
                    text("SELECT id, fix_commits FROM cve WHERE cve_id = :c"),
                    {"c": r["cve_id"]},
                ).fetchone()
                if row is None:
                    # Insert minimal new row — NVD ingestion will fill description later.
                    conn.execute(text("""
                        INSERT INTO cve (cve_id, fix_commits)
                        VALUES (:c, CAST(:fc AS jsonb))
                        ON CONFLICT (cve_id) DO NOTHING
                    """), {"c": r["cve_id"],
                            "fc": json.dumps(r["shas"])})
                    stats["cves_inserted_new"] += 1
                    stats["shas_added"] += len(r["shas"])
                else:
                    existing = row.fix_commits or []
                    merged = merge_fix_commits(existing, r["shas"])
                    added = len(merged) - len(existing)
                    if added > 0:
                        conn.execute(text("""
                            UPDATE cve SET fix_commits = CAST(:fc AS jsonb)
                             WHERE id = :id
                        """), {"id": row.id, "fc": json.dumps(merged)})
                        stats["cves_updated_existing"] += 1
                        stats["shas_added"] += added
            logger.info("processed %d / %d CVEs (added %d new SHAs so far)",
                        min(i + CHUNK, len(rows_to_upsert)),
                        len(rows_to_upsert),
                        stats["shas_added"])

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="/tmp/vulns_probe",
                    help="local checkout of vulns.git")
    ap.add_argument("--clone-if-missing", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="git pull before ingest")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    repo_path = Path(args.repo).resolve()
    if not ensure_repo(repo_path, args.clone_if_missing, args.refresh):
        return 1

    from storage.pg.engine import get_engine
    eng = get_engine()
    stats = ingest(eng, repo_path)
    print()
    print("=== Linux Kernel CVE Project ingest ===")
    print(f"  CVEs scanned:               {stats['cves_seen']:>8,}")
    print(f"  Existing CVE rows updated:  {stats['cves_updated_existing']:>8,}")
    print(f"  New CVE rows inserted:      {stats['cves_inserted_new']:>8,}")
    print(f"  Total fix-SHAs added:       {stats['shas_added']:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

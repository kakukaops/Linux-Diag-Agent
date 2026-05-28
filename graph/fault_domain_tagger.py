"""Tag commits with fault_domain via subject/body keyword matching.

Heuristic — every match carries a confidence < 1.0 and an explicit
source (`subject-keyword` or `body-keyword`). Downstream filters can
weight by source.

Run once after kernel_commit ingestion (or any time the keyword
dictionary changes):

  python -m graph.fault_domain_tagger
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# (domain, [subject-keyword regexes], [body-keyword regexes])
#
# Subject hits get conf=0.8 (high — kernel subject prefix is highly
# canonical: 'mm: fix oom_score race' is unambiguously oom-related).
# Body-only hits get conf=0.6 — body text can mention symptoms without
# being a fix for that domain.
_DOMAIN_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    "oom": (
        [r"\boom[\b_:]", r"out[- ]of[- ]memory", r"memcg\b", r"memory\.high",
         r"memory\.max\b", r"\boom_kill", r"\boom_killer"],
        [r"\bOOM\b", r"out of memory", r"memcg accounting",
         r"oom_score_adj", r"oom-kill"],
    ),
    "panic": (
        [r"\bpanic\b", r"\bBUG: ", r"die_handler", r"kernel_die"],
        [r"\bkernel panic\b", r"\bBUG:\s+unable", r"die_handler"],
    ),
    "softlockup": (
        [r"soft.?lockup\b", r"watchdog: BUG.*softlockup"],
        [r"\bsoft lockup\b", r"watchdog: BUG: soft lockup"],
    ),
    "hardlockup": (
        [r"hard.?lockup\b", r"NMI watchdog"],
        [r"\bhard lockup\b", r"NMI watchdog: Watchdog detected"],
    ),
    "rcu_stall": (
        [r"\brcu\b.*stall", r"rcu_sched.*stall", r"rcu_preempt.*stall"],
        [r"rcu_sched self-detected stall", r"INFO: rcu_sched detected stalls",
         r"rcu_preempt detected stalls"],
    ),
    "io_hang": (
        [r"\bhung[_ ]task\b", r"\bblk_mq\b", r"\bnvme\b.*timeout",
         r"\bscsi\b.*timeout"],
        [r"hung_task_timeout_secs", r"blk_mq_get_tag", r"task blocked for",
         r"nvme_timeout"],
    ),
    "deadlock": (
        [r"\bdeadlock\b", r"\blockdep\b", r"\bABBA\b", r"circular.*lock"],
        [r"WARNING: possible circular locking dependency",
         r"WARNING: held lock freed", r"lockdep"],
    ),
    "network": (
        [r"^(net|tcp|udp|ipv4|ipv6|netfilter|nft|skb|sock|wireguard|tls|ipsec):",
         r"^(nf|nfnetlink):",],
        [r"\bskb_put\b", r"\btcp_sendmsg\b", r"\bsock_recvmsg\b"],
    ),
    "perf_regression": (
        [r"\bregress(?:ion|es|ed)?\b.*perf", r"perf.*regress"],
        [r"\bperformance regression", r"\bbisected to commit"],
    ),
    "sched_anomaly": (
        [r"^sched(?:_fair|_rt|_deadline)?:", r"wakeup latency", r"sched: fix"],
        [r"sched_entity", r"\bcfs_rq\b", r"wakeup latency"],
    ),
    "hardware": (
        [r"\b(?:MCE|EDAC|machine.check)\b", r"PCIe.*error", r"\bECC\b"],
        [r"\bHardware Error", r"Machine Check Exception",
         r"EDAC.*Corrected Error", r"PCIe Bus Error"],
    ),
}


def _compile(pats: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in pats]


def _build_matchers():
    return {
        d: (_compile(subj), _compile(body))
        for d, (subj, body) in _DOMAIN_KEYWORDS.items()
    }


def tag_commits(engine, batch: int = 5000, since_date: str | None = None) -> dict:
    """Walk kernel_commit and emit (commit, domain) edges where the body or
    subject matches a domain's keyword set. Idempotent via ON CONFLICT."""
    from sqlalchemy import text
    matchers = _build_matchers()

    where = ["body IS NOT NULL"]
    params: dict = {"batch": batch}
    if since_date:
        where.append("commit_date >= :since")
        params["since"] = since_date
    where_sql = " AND ".join(where)

    inserted = {d: 0 for d in matchers}
    total = 0
    offset = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT hash, subject, body
                  FROM kernel_commit
                 WHERE {where_sql}
                 ORDER BY hash
                 LIMIT :batch OFFSET :offset
            """), {**params, "offset": offset}).fetchall()
        if not rows:
            break
        edges: list[dict] = []
        for r in rows:
            subj = r.subject or ""
            body = r.body or ""
            for domain, (subj_pats, body_pats) in matchers.items():
                if any(p.search(subj) for p in subj_pats):
                    edges.append({"h": r.hash, "d": domain,
                                   "src": "subject-keyword", "c": 0.80})
                elif any(p.search(body) for p in body_pats):
                    edges.append({"h": r.hash, "d": domain,
                                   "src": "body-keyword", "c": 0.60})
        if edges:
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO link_commit_fault_domain
                        (commit_hash, domain, source, confidence)
                    VALUES (:h, :d, :src, :c)
                    ON CONFLICT ON CONSTRAINT uq_lcfd DO NOTHING
                """), edges)
            for e in edges:
                inserted[e["d"]] += 1
        total += len(rows)
        offset += batch
        logger.info("scanned=%d, edges-so-far=%d", total, sum(inserted.values()))
        if len(rows) < batch:
            break
    return {"scanned": total, "inserted_per_domain": inserted}


def main() -> int:
    import argparse
    import logging as _logging
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; only scan commits after")
    ap.add_argument("--batch", type=int, default=5000)
    args = ap.parse_args()

    _logging.basicConfig(level=_logging.INFO,
                          format="%(asctime)s %(levelname)s %(message)s")

    from storage.pg.engine import get_engine
    eng = get_engine()
    result = tag_commits(eng, batch=args.batch, since_date=args.since)
    print(result)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

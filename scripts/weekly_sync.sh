#!/usr/bin/env bash
# weekly_sync.sh — M3 ingestion cron script (M3 §4)
# Runs every Sunday at 03:00 UTC via systemd timer or cron.
#
# Usage: ./scripts/weekly_sync.sh [--dry-run]
#
# Phases:
#   1. Network-intensive sources (4-way parallel): lkml / bugzilla / syzbot / nvd
#   2. Kernel git ETL (dual-repo)
#   3. (Monthly, first Sunday) Zenodo dataset refresh
#   4. Cross-Graph Linker rebuild
#   5. Health check + alerting

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
RUN_ID="$(date +%Y%m%d-%H%M)"
LOG_DIR="${REPO_ROOT}/data/logs/$(date +%Y-%m-%d)"
LOG_FILE="${LOG_DIR}/sync-${RUN_ID}.log"
DRY_RUN=0

# Parse args
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
  esac
done

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

ts() { date "+%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }
run_py() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] python -m $*"
    return 0
  fi
  python -m "$@"
}

log "=== Weekly sync ${RUN_ID} starting (dry_run=${DRY_RUN}) ==="
cd "$REPO_ROOT"

# Activate virtualenv if present
if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
fi

# ── Phase 1: Network sources (parallel) ─────────────────────────────────
log "Phase 1: network sources (parallel)"
(run_py ingest.lkml       incremental 2>&1 | sed 's/^/[lkml] /'       ) &  PID_LKML=$!
(run_py ingest.bugzilla   incremental 2>&1 | sed 's/^/[bugzilla] /'   ) &  PID_BZ=$!
(run_py ingest.syzbot     incremental 2>&1 | sed 's/^/[syzbot] /'     ) &  PID_SZ=$!
(run_py ingest.nvd        incremental 2>&1 | sed 's/^/[nvd] /'        ) &  PID_NVD=$!

PHASE1_FAILED=0
wait $PID_LKML     || { log "WARN: lkml failed";     PHASE1_FAILED=1; }
wait $PID_BZ       || { log "WARN: bugzilla failed";  PHASE1_FAILED=1; }
wait $PID_SZ       || { log "WARN: syzbot failed";    PHASE1_FAILED=1; }
wait $PID_NVD      || { log "WARN: nvd failed";       PHASE1_FAILED=1; }

log "Phase 1 done (failed=${PHASE1_FAILED})"

# ── Phase 2: Kernel git ETL ──────────────────────────────────────────────
log "Phase 2: kernel_commit ETL"
run_py ingest.kernel_commit incremental || log "WARN: kernel_commit failed"
log "Phase 2 done"

# ── Phase 3: Zenodo (monthly, first Sunday ≤ day 7) ─────────────────────
if [[ "$(date +%d)" -le "07" ]]; then
  log "Phase 3: Zenodo dataset refresh"
  run_py ingest.zenodo incremental || log "WARN: zenodo failed"
  log "Phase 3 done"
else
  log "Phase 3: skipped (not first Sunday)"
fi

# ── Phase 4: Cross-Graph Linker ──────────────────────────────────────────
log "Phase 4: Cross-Graph Linker"
run_py graph.linker run_all 2>&1 || log "WARN: linker failed"
log "Phase 4 done"

# ── Phase 5: Health check ────────────────────────────────────────────────
log "Phase 5: health check"
run_py scripts.sync_audit || log "WARN: sync_audit returned non-zero"
log "Phase 5 done"

log "=== Weekly sync ${RUN_ID} complete ==="

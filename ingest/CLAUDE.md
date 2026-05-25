# ingest/ — Ingestion Pipelines

## Overview

Six pipelines, all extend `BaseIngester` (`ingest/base.py`). Each has a `__main__.py` entry point and stores progress in `ingest_runs` table (written only at run END).

```
ingest/
  base.py            BaseIngester, RunReport, Quarantine, CheckpointStore
  lkml/              lore.kernel.org Atom+raw fetcher → lkml_message
  bugzilla/          bugzilla.kernel.org REST → bug
  syzbot/            syzbot.kernel.org HTML scraper → syzbot_crash
  nvd/               NVD API v2.0 → cve (+ fix_commits extraction)
  zenodo/            Zenodo dataset import (skipped in test phase)
  kernel_commit/     OLK git log → kernel_commit (+ OLK inclusion parsing)
```

## Rate limits — DO NOT reduce without re-testing

| Source | Delay | Location |
|--------|-------|----------|
| lore.kernel.org | 1.5s per request | `lkml/fetcher.py _REQUEST_DELAY_S` |
| NVD API | 6.0s per request | `nvd/ingester.py _DELAY_S` |
| Bugzilla | 1.5s per request | `bugzilla/ingester.py` |
| syzbot | 1.5s per request | `syzbot/scraper.py` |

## Per-source API constraints

### lore.kernel.org (lkml/)
- `?x=mbox&since=` returns HTML — not supported, do not use
- Correct: Atom feed `/{list}/?q=d:YYYYMMDD..&x=A&o=N` (200 entries/page) + `/{list}/{msg-id}/raw`
- `_list_message_urls()` paginates Atom until entries < 200; then `_fetch_raw()` per message
- All mbox is buffered in memory before parse + insert — large lists (linux-mm, 30d) take 1-2h

### NVD API (nvd/)
- Endpoint: `https://services.nvd.nist.gov/rest/json/cves/2.0`
- Filter: `keywordSearch=linux kernel` (NOT `virtualMatchString` with CPE — returns 404)
- Max date window: 120 days → implemented as 119-day sliding chunks with `_MAX_WINDOW = 119`
- Paginate with `startIndex` + `resultsPerPage=2000`
- `fix_commits` column: extracted by `nvd/extractor.py` from GitHub/kernel.org commit URLs in `references`

### Bugzilla (bugzilla/)
- Endpoint: `https://bugzilla.kernel.org/rest/bug`
- Date filter param: `last_change_time` with format `YYYY-MM-DD` (not `changed_after`, not ISO datetime)
- Scope: kernel.org only (ADR-011); Red Hat BZ deferred to v1.1

### kernel_commit (kernel_commit/)
- Phase A: OLK repos (OLK-6.6 + OLK-5.10) via git log on `origin/<branch>`
- Phase B: linux-stable.git mainline auxiliary repo
- Non-UTF-8 bytes in commit messages: always decode with `errors="replace"`, never `text=True`
- OLK inclusion parsing: `tag ∈ {mainline, stable}` → backport; all others → openEuler-native
- See `kernel_commit/olk_inclusion.py` and `kernel_commit/trailer_parser.py`

## Checkpoint pattern

`BaseIngester.run()` calls `CheckpointStore.load()` at start, passes checkpoint dict to `incremental()`, then saves the returned dict on success. Schema per ingester:

| Ingester | Checkpoint keys |
|----------|----------------|
| lkml | `since_per_list`, `last_message_id_per_list`, `summary_deferred_queue` |
| nvd | `last_modified` (ISO datetime string) |
| bugzilla | `last_change_time` (YYYY-MM-DD) |
| syzbot | `last_seen_ids` (set of crash IDs) |
| kernel_commit | `last_sha_per_branch` (branch → full SHA) |

To reset a pipeline: `DELETE FROM ingest_runs WHERE ingester = '<name>';`

## SQL conventions in ingesters

- jsonb params: `CAST(:name AS jsonb)` — never `:name::jsonb` (psycopg3 rejects it)
- `"references"` must be double-quoted (PG reserved word)
- All upserts use `ON CONFLICT (...) DO UPDATE SET` or `DO NOTHING`
- Failed rows go to `Quarantine` (file-based, under `data/quarantine/<source>/<run_id>/`)

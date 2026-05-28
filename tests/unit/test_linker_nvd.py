"""Regression tests for graph.linker.

Three Pre-v2.3 bugs in the linker that this file guards against:

1. **link_nvd_commits**: `WHERE … LIMIT :limit` with default 500 and no
   pagination — silently processed only the first 500 CVEs and stopped.
   Capped link_commit_cve at 547 rows (out of 2,870 upper bound).

2. **_link_trailer_to_bug**: same LIMIT-truncation trap, AND queried
   `bug.id = :extracted_number` when the bugzilla bug number actually
   lives in `bug.external_id`. Result: 1,717 commits citing
   bugzilla.kernel.org produced just 1 link.

3. **_link_link_trailer_to_message**: same LIMIT trap (default 1000)
   for Link: lore.kernel.org trailers. Should walk all candidate commits.

Asserts the SQL each function executes does not re-introduce LIMIT.
"""

from unittest.mock import MagicMock, patch

from graph.linker import (
    link_nvd_commits,
    _link_trailer_to_bug,
    _link_link_trailer_to_message,
    _link_olk_upstream,
)


def _capture_executes(conn_mock):
    """Pull all SQL strings (TextClause `.text`) the mock conn was asked to run."""
    sqls = []
    for call in conn_mock.execute.call_args_list:
        stmt = call.args[0]
        text_str = getattr(stmt, "text", str(stmt))
        sqls.append(text_str)
    return sqls


def test_link_nvd_commits_sql_has_no_LIMIT_clause():
    """The original bug: bulk linker silently truncated at LIMIT 500."""
    with patch("graph.linker.text") as mock_text:
        eng = MagicMock()
        conn = eng.begin.return_value.__enter__.return_value
        conn.execute.return_value.scalar.return_value = 0
        # Wrap each text() call so we can inspect what SQL was passed.
        produced: list[str] = []
        def fake_text(s):
            produced.append(s)
            t = MagicMock()
            t.text = s
            return t
        mock_text.side_effect = fake_text

        link_nvd_commits(eng)

    # The bulk INSERT must use INSERT…SELECT with JSON-array LATERAL expansion
    # and must NOT contain any LIMIT clause.
    bulk = [s for s in produced if "INSERT INTO link_commit_cve" in s]
    assert bulk, "expected exactly one bulk INSERT INTO link_commit_cve"
    assert len(bulk) == 1, f"expected 1 bulk insert, got {len(bulk)}"
    sql = bulk[0]
    assert "json_array_elements_text" in sql, "must explode fix_commits via LATERAL"
    assert "LIMIT" not in sql.upper(), \
        "regression: LIMIT clause re-introduced — this is the v1 bug"
    assert "ON CONFLICT" in sql, "must dedupe via ON CONFLICT to be idempotent"


def test_link_nvd_commits_returns_row_delta():
    """Function returns (after_count - before_count), not the raw INSERT
    affected-row count, so calling it again should return 0."""
    eng = MagicMock()
    conn = eng.begin.return_value.__enter__.return_value
    # First scalar() call = before (547), second = after (2864)
    conn.execute.return_value.scalar.side_effect = [547, 2864]

    n = link_nvd_commits(eng)
    assert n == 2864 - 547 == 2317


def test_link_nvd_commits_idempotent_returns_zero():
    """A second invocation finds ON CONFLICT collisions and inserts 0 rows;
    before/after counts match → return 0."""
    eng = MagicMock()
    conn = eng.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar.side_effect = [2864, 2864]

    n = link_nvd_commits(eng)
    assert n == 0


def _capture_sql(target_fn, *args, **kw) -> list[str]:
    """Run target_fn with text() patched and return all SQL strings issued."""
    produced: list[str] = []
    def fake_text(s):
        produced.append(s)
        t = MagicMock(); t.text = s; return t
    with patch("graph.linker.text", side_effect=fake_text):
        target_fn(*args, **kw)
    return produced


def test_link_trailer_to_bug_does_not_truncate():
    """Regression: pre-v2.3 used `LIMIT :batch_size`, capping bugzilla
    linking to the first N commits. The fixed function streams all
    candidates via WHERE body ILIKE '%bugzilla.kernel.org/show_bug%'."""
    conn = MagicMock()
    # First execute() builds bug_idx; iterating over it should yield nothing.
    conn.execute.return_value.__iter__ = MagicMock(return_value=iter([]))

    from graph.linker import LinkReport
    sqls = _capture_sql(_link_trailer_to_bug, conn, 1000, LinkReport())

    select_bug_idx = [s for s in sqls if "FROM bug WHERE source = 'bugzilla_kernel'" in s]
    assert select_bug_idx, "must build a (external_id → bug.id) index"

    # No SELECT … LIMIT in the candidate-commit query
    for s in sqls:
        if "FROM kernel_commit" in s:
            assert "LIMIT" not in s.upper(), \
                f"regression: LIMIT clause re-introduced — {s[:200]}"


def test_link_trailer_to_bug_uses_external_id_not_pk():
    """Regression: pre-v2.3 looked up `bug.id = :extracted_number` instead
    of `(source, external_id)`. The index-build SQL must select
    external_id, not just id."""
    conn = MagicMock()
    conn.execute.return_value = []

    from graph.linker import LinkReport
    sqls = _capture_sql(_link_trailer_to_bug, conn, 1000, LinkReport())

    bug_idx_sql = next(s for s in sqls if "FROM bug WHERE source" in s)
    assert "external_id" in bug_idx_sql, \
        "bug-id lookup must use external_id (the bugzilla bug number), not just bug.id"


def test_link_olk_upstream_does_not_truncate():
    """Regression: pre-v2.3 had a hard-coded `LIMIT 500` for OLK→upstream
    stub insertion. With 14,661 distinct orphan upstream SHAs, the
    function would have needed 30+ invocations to finish. The fix bulk-
    inserts via INSERT…SELECT grouped by upstream_commit."""
    conn = MagicMock()
    conn.execute.return_value.scalar.return_value = 0   # before/after counts

    from graph.linker import LinkReport
    sqls = _capture_sql(_link_olk_upstream, conn, LinkReport())

    insert_sqls = [s for s in sqls if "INSERT INTO kernel_commit" in s]
    assert insert_sqls, "should issue an INSERT into kernel_commit (stub rows)"
    for s in insert_sqls:
        assert "LIMIT" not in s.upper(), \
            f"regression: LIMIT clause re-introduced — {s[:200]}"
        assert "ON CONFLICT" in s, "must dedupe via ON CONFLICT"
    # The new bulk version uses GROUP BY to dedupe upstream SHAs.
    assert any("GROUP BY" in s.upper() for s in insert_sqls), \
        "bulk insert must GROUP BY upstream_commit to dedupe"

    # Stub-subject format is critical for graph/reconcile.py — see
    # graph/CLAUDE.md "do not modify". Assert preservation.
    assert any("stub upstream for OLK" in s for s in insert_sqls), \
        "stub-subject format must be preserved (graph/reconcile.py depends on it)"


def test_link_link_trailer_to_message_does_not_truncate():
    """Regression: pre-v2.3 LIMIT :batch_size on the lkml message linker too."""
    conn = MagicMock()
    conn.execute.return_value = []

    from graph.linker import LinkReport
    sqls = _capture_sql(_link_link_trailer_to_message, conn, 1000, LinkReport())

    for s in sqls:
        if "FROM kernel_commit" in s:
            assert "LIMIT" not in s.upper(), \
                f"regression: LIMIT clause re-introduced — {s[:200]}"


def test_batch_size_under_threshold_logs_warning_but_does_not_truncate(caplog):
    """API-compat: batch_size argument is accepted but ignored. Tiny values
    log a warning (so callers notice the deprecation)."""
    import logging
    caplog.set_level(logging.WARNING)
    eng = MagicMock()
    conn = eng.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar.side_effect = [0, 100]

    link_nvd_commits(eng, batch_size=50)

    msg = " ".join(r.message for r in caplog.records)
    assert "batch_size" in msg and "ignored" in msg

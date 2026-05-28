"""Regression tests for graph.linker.link_nvd_commits.

Pre-v2.3 the function had `WHERE ... LIMIT :limit` with default 500 and
no pagination — it silently processed only the first 500 CVEs and stopped.
That capped link_commit_cve at 547 rows (out of a 2,870-row upper bound).
The fix replaced the per-row loop with a single INSERT…SELECT that walks
all CVE rows. These tests assert the SQL the function executes does not
re-introduce the LIMIT.
"""

from unittest.mock import MagicMock, patch

from graph.linker import link_nvd_commits


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

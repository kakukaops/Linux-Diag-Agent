"""Integration tests for L2 graph query tools (v2 T-007/008/009).

Exercise check_backport_status / get_regression_fixes / get_commit_diff
against the real `kernel_commit` DB + local OLK git repos. Each test is
self-grounding: it queries the DB for a real example, then asserts the tool
agrees. Skipped when the DB / repos are unavailable.
"""

import pytest
from sqlalchemy import text


@pytest.fixture(scope="module")
def engine():
    try:
        from storage.pg.engine import get_engine
        eng = get_engine()
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return eng
    except Exception:
        pytest.skip("PostgreSQL not available")


# ── T-007 check_backport_status ─────────────────────────────────────────────

def test_check_backport_status_known_backport(engine):
    from graph.queries import check_backport_status
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT hash, upstream_commit, affected_versions FROM kernel_commit
             WHERE upstream_commit IS NOT NULL AND upstream_commit <> ''
               AND array_length(affected_versions, 1) > 0
             LIMIT 1
        """)).fetchone()
    if not row:
        pytest.skip("no backport commits in DB")
    olk_hash, upstream, versions = row

    result = check_backport_status(upstream, versions[0])
    assert result["backported"] is True
    assert result["olk_commit_hash"] == olk_hash
    assert result["olk_version"] == versions[0]
    assert result["inclusion_type"] in ("mainline", "stable")


def test_check_backport_status_unknown_sha(engine):
    from graph.queries import check_backport_status
    result = check_backport_status("deadbeef" * 5, "OLK-6.6")  # fake 40-char
    assert result["backported"] is False


def test_check_backport_status_normalizes_version(engine):
    from graph.queries import check_backport_status
    result = check_backport_status("deadbeef" * 5, "v6.6")
    assert result["olk_version"] == "OLK-6.6"   # 'v6.6' → 'OLK-6.6'


# ── T-008 get_regression_fixes ──────────────────────────────────────────────

def test_get_regression_fixes_finds_fixing_commit(engine):
    from graph.queries import get_regression_fixes
    # A commit with a Fixes: ref → fixes_refs[0] is a commit that has a
    # regression fix (namely, this commit).
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT hash, fixes_refs FROM kernel_commit
             WHERE array_length(fixes_refs, 1) > 0
             LIMIT 1
        """)).fetchone()
    if not row:
        pytest.skip("no commits with fixes_refs")
    fixing_hash, fixes_refs = row
    buggy_ref = fixes_refs[0]

    result = get_regression_fixes(buggy_ref)
    assert result["has_regression_fix"] is True
    assert any(fc["hash"] == fixing_hash for fc in result["fixing_commits"])


def test_get_regression_fixes_clean_commit(engine):
    from graph.queries import get_regression_fixes
    result = get_regression_fixes("deadbeef" * 5)  # fake — nothing fixes it
    assert result["has_regression_fix"] is False
    assert result["fixing_commits"] == []


# ── T-009 get_commit_diff ───────────────────────────────────────────────────

def test_get_commit_diff_real_commit(engine):
    from graph.git_diff import get_commit_diff
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT hash FROM kernel_commit
             WHERE array_length(affected_versions, 1) > 0
             LIMIT 1
        """)).fetchone()
    if not row:
        pytest.skip("no commits in DB")

    result = get_commit_diff(row[0])
    if not result["found"]:
        pytest.skip("OLK git repo not available")
    assert result["repo"] in ("OLK-6.6", "OLK-5.10")
    assert result["diff"].startswith("commit ")   # `git show` header


def test_get_commit_diff_unknown_hash(engine):
    from graph.git_diff import get_commit_diff
    result = get_commit_diff("deadbeef" * 5)
    assert result["found"] is False

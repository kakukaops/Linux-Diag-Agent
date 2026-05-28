"""Unit tests for M22 ReAct tool registration (agent/react/tools/).

All tests use mocks / stubs — no DB, no network, no CodeGraph.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.react.tools.registry import build_registry


# ── build_registry ────────────────────────────────────────────────────────────

def test_build_registry_has_all_categories():
    reg = build_registry()
    names = {t.name for t in reg.for_route("kernel")}
    # Category A
    assert {"search_commits", "search_lkml", "search_bugs", "search_syzbot",
            "search_cve", "search_code", "lookup_symbol"} <= names
    # Category D
    assert {"get_commit_detail", "get_commit_diff", "check_backport_status",
            "get_regression_fixes", "get_function_source", "get_call_graph",
            "expand_query_from_symbol", "browse_subsystem_fixes",
            "find_commits_touching_symbol", "find_similar_crashes",
            "get_patch_series"} <= names
    # Category B
    assert {"parse_dmesg", "parse_sosreport", "extract_call_trace"} <= names


def test_kernel_route_does_not_expose_change_only_tools():
    """search_lkml/search_bugs should not appear in hardware route."""
    reg = build_registry()
    hw_names = {t.name for t in reg.for_route("hardware")}
    assert "search_lkml" not in hw_names
    assert "search_bugs" not in hw_names
    # But search_commits and search_cve should be there
    assert "search_commits" in hw_names
    assert "search_cve" in hw_names


def test_hardware_route_has_log_tools():
    reg = build_registry()
    hw_names = {t.name for t in reg.for_route("hardware")}
    assert "parse_dmesg" in hw_names


def test_change_route_has_commit_tools_not_kernel_only():
    reg = build_registry()
    change_names = {t.name for t in reg.for_route("change")}
    assert "get_commit_detail" in change_names
    assert "get_commit_diff" in change_names
    # check_backport_status is kernel-only
    assert "check_backport_status" not in change_names


def test_unknown_route_has_all_tools():
    reg = build_registry()
    unknown_names = {t.name for t in reg.for_route("unknown")}
    kernel_names = {t.name for t in reg.for_route("kernel")}
    # unknown should be a superset of kernel
    assert kernel_names <= unknown_names


def test_schemas_are_valid_tool_schemas():
    reg = build_registry()
    schemas = reg.schemas("kernel")
    assert len(schemas) > 0
    for s in schemas:
        assert s.type == "function"
        assert s.function.name
        assert s.function.description


# ── retrieval tools (mocked DB) ───────────────────────────────────────────────

def _fake_evidence(title="fix: mm oom", score=0.9, commit_hash="abc123def456"):
    from retrieval.schema import Evidence, RouteTag
    return Evidence(route=RouteTag.commit, score=score, title=title,
                    commit_hash=commit_hash, body="Body text")


def test_search_commits_formats_results():
    from agent.react.tools.retrieval_tools import _search_commits
    with patch("retrieval.recall.commit.recall", return_value=[_fake_evidence()]):
        result = _search_commits(keywords="oom cgroup")
    assert "fix: mm oom" in result
    assert "abc123" in result
    assert "0.90" in result


def test_search_commits_empty_returns_no_results():
    from agent.react.tools.retrieval_tools import _search_commits
    with patch("retrieval.recall.commit.recall", return_value=[]):
        result = _search_commits(keywords="nonexistent xyz")
    assert "No results" in result


def test_search_cve_by_id():
    from agent.react.tools.retrieval_tools import _search_cve
    from retrieval.schema import Evidence, RouteTag
    fake = Evidence(route=RouteTag.cve, score=1.0, title="CVE-2024-50022",
                    cve_id="CVE-2024-50022", body="memory corruption in mm/")
    with patch("retrieval.recall.cve.recall", return_value=[fake]):
        result = _search_cve(cve_id="CVE-2024-50022")
    assert "CVE-2024-50022" in result


def test_search_code_codegraph_unavailable():
    from agent.react.tools.retrieval_tools import _search_code
    from clients.codegraph.client import CodeGraphError
    with patch("clients.codegraph.client.get_codegraph_client") as mock_get:
        mock_get.return_value.search_code.side_effect = CodeGraphError("timeout")
        result = _search_code(query="oom_kill_process")
    assert "unavailable" in result.lower()


def test_expand_query_from_symbol_extracts_neighbors():
    """tcp_send_mss's body cites mss_now / size_goal / tcp_current_mss — those
    are the candidates the LLM should retry BM25 with when the symptom symbol
    itself returns nothing."""
    from agent.react.tools.code_tools import _expand_query_from_symbol
    fake_source = """
        unsigned int tcp_send_mss(struct sock *sk, int *size_goal, int flags) {
            int mss_now = tcp_current_mss(sk);
            *size_goal = tcp_xmit_size_goal(sk, mss_now, !(flags & MSG_OOB));
            return mss_now;
        }
    """
    with patch("clients.codegraph.client.get_codegraph_client") as mock_get:
        client = mock_get.return_value
        client.resolve_repo.return_value = "olk-kernel"
        client.passthrough.return_value = {
            "content": [{"type": "text", "text": fake_source}]
        }
        result = _expand_query_from_symbol(symbol="tcp_send_mss",
                                            kernel_version="OLK-6.6")
    # Should surface neighbor identifiers, drop stopwords, drop the symbol itself
    assert "mss_now" in result
    assert "tcp_current_mss" in result
    assert "tcp_xmit_size_goal" in result
    assert "MSG_OOB" in result
    assert "tcp_send_mss" not in result.split("from ")[1].split("source")[0] \
           or result.count("tcp_send_mss") == 1  # only in header
    # Should drop C keywords / common
    assert "struct" not in result
    assert "return" not in result.split("Candidate")[1]


def test_browse_subsystem_fixes_formats_results():
    """When net: fix commits are returned, format them with hash + date + subject."""
    from agent.react.tools.code_tools import _browse_subsystem_fixes
    import datetime as _dt
    class _Row:
        def __init__(self, h, s, d, o):
            self.hash, self.subject, self.commit_date, self.origin = h, s, d, o
    fake_rows = [
        _Row("9ab5cf19fb0e4680f95e506d6c544259bf1111c4",
             "net: fix crash when config small gso_max_size/gso_ipv4_max_size",
             _dt.datetime(2024, 10, 23), "mainline"),
        _Row("8615d2e1fb7dc6570b463d893aad1cfd82e65329",
             "net: fix crash when config small gso_max_size/gso_ipv4_max_size",
             _dt.datetime(2024, 10, 23), "olk"),
    ]
    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = fake_rows
        result = _browse_subsystem_fixes(subsystem_prefixes="net,tcp",
                                          contains="crash")
    assert "9ab5cf19fb0e" in result
    assert "gso_max_size" in result
    assert "mainline" in result


def test_browse_subsystem_fixes_empty():
    from agent.react.tools.code_tools import _browse_subsystem_fixes
    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = []
        result = _browse_subsystem_fixes(subsystem_prefixes="ext4",
                                          contains="nonexistent")
    assert "no fix commits" in result.lower()


def test_find_commits_touching_symbol_formats_results():
    """Reverse lookup against link_commit_symbol returns commits + subjects."""
    from agent.react.tools.code_tools import _find_commits_touching_symbol
    import datetime as _dt
    class _Row:
        def __init__(self, h, fp, k, subj, d, o):
            self.commit_hash = h
            self.file_path = fp
            self.kind = k
            self.subject = subj
            self.commit_date = d
            self.origin = o
    fake_rows = [
        _Row("9ab5cf19fb0e", "net/core/rtnetlink.c", "struct",
             "net: fix crash when config small gso_max_size",
             _dt.datetime(2024, 10, 23), "mainline"),
        _Row("ad04a1fad56a", "net/core/rtnetlink.c", "struct",
             "net: fix crash when config small gso_max_size",
             _dt.datetime(2025, 7, 3), "olk"),
    ]
    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = fake_rows
        result = _find_commits_touching_symbol(symbol="ifla_policy")
    assert "ifla_policy" in result
    assert "9ab5cf19fb0e" in result
    assert "ad04a1fad56a" in result
    assert "net/core/rtnetlink.c" in result


def test_find_commits_touching_symbol_empty():
    from agent.react.tools.code_tools import _find_commits_touching_symbol
    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = []
        result = _find_commits_touching_symbol(symbol="nonexistent_sym_xyz")
    assert "no commits" in result.lower()


def test_find_similar_crashes_rejects_empty_input():
    from agent.react.tools.code_tools import _find_similar_crashes
    assert "required" in _find_similar_crashes(trace_text="").lower()


def test_find_similar_crashes_returns_signature_and_matches():
    """Stable signature is computed and queries fire against all 4 sources."""
    from agent.react.tools.code_tools import _find_similar_crashes
    trace = (
        "Call Trace:\n"
        " kmalloc_trace+0x123/0x200\n"
        " skb_put+0x4a/0x90\n"
        " tcp_send_mss+0x12a/0x1f0\n"
        " tcp_sendmsg_locked+0x6b3/0xf30\n"
    )
    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.connect.return_value.__enter__.return_value
        # Each subsequent execute() call returns an empty fetchall.
        conn.execute.return_value.fetchall.return_value = []
        result = _find_similar_crashes(trace_text=trace)
    assert "stack_signature:" in result
    # 4 SELECTs (syzbot, bug, lkml, dmesg) should have fired
    assert conn.execute.call_count == 4
    assert "no matching past reports" in result.lower()


def test_find_commits_touching_symbol_rejects_empty():
    from agent.react.tools.code_tools import _find_commits_touching_symbol
    assert "required" in _find_commits_touching_symbol(symbol="").lower()
    assert "required" in _find_commits_touching_symbol(symbol="  ").lower()


def test_browse_subsystem_fixes_rejects_empty_prefix():
    from agent.react.tools.code_tools import _browse_subsystem_fixes
    result = _browse_subsystem_fixes(subsystem_prefixes="")
    assert "required" in result.lower()


def test_expand_query_from_symbol_no_source():
    """When CodeGraph returns no source, surface clear message instead of garbage."""
    from agent.react.tools.code_tools import _expand_query_from_symbol
    with patch("clients.codegraph.client.get_codegraph_client") as mock_get:
        client = mock_get.return_value
        client.resolve_repo.return_value = "olk-kernel"
        client.passthrough.return_value = {"content": [{"type": "text", "text": ""}]}
        client.search_code.return_value = []
        result = _expand_query_from_symbol(symbol="nonexistent_xyz",
                                            kernel_version="OLK-6.6")
    assert "no source" in result.lower() or "not found" in result.lower()


# ── log tools ─────────────────────────────────────────────────────────────────

_OOM_DMESG = (
    "[ 123.456] Out of memory: Kill process 1234 (python3) score 900 or sacrifice child\n"
    "[ 123.457] Killed process 1234 (python3) total-vm:2048kB, anon-rss:1024kB\n"
)


def test_parse_dmesg_detects_oom():
    from agent.react.tools.log_tools import _parse_dmesg
    result = _parse_dmesg(text=_OOM_DMESG)
    assert "OOM" in result.upper() or "out of memory" in result.lower()


def test_parse_dmesg_no_events():
    from agent.react.tools.log_tools import _parse_dmesg
    result = _parse_dmesg(text="nothing happened today\n")
    assert "No kernel events" in result


def test_extract_call_trace():
    from agent.react.tools.log_tools import _extract_call_trace
    text = (
        "Call Trace:\n"
        " [<ffffffff8012abcd>] oom_kill_process+0x1a/0x30\n"
        " [<ffffffff8011aaaa>] out_of_memory+0x200/0x400\n"
    )
    result = _extract_call_trace(text=text)
    assert "oom_kill_process" in result
    assert "out_of_memory" in result


def test_extract_call_trace_no_trace():
    from agent.react.tools.log_tools import _extract_call_trace
    result = _extract_call_trace(text="no trace here")
    assert "No call trace" in result


def test_parse_sosreport_bad_path():
    from agent.react.tools.log_tools import _parse_sosreport
    # sosreport parser degrades gracefully — returns empty summary or error text
    result = _parse_sosreport(path="/nonexistent/path/sos.tar.xz")
    # must at least return a string with recognizable fields
    assert isinstance(result, str) and len(result) > 0


# ── code tools (mocked) ───────────────────────────────────────────────────────

def test_get_commit_detail_not_found():
    from agent.react.tools.code_tools import _get_commit_detail
    with patch("storage.pg.engine.get_engine") as mock_eng:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_eng.return_value.connect.return_value = mock_conn
        result = _get_commit_detail(commit_hash="deadbeef1234")
    assert "not found" in result


def test_get_commit_detail_found():
    from agent.react.tools.code_tools import _get_commit_detail
    FakeRow = MagicMock()
    FakeRow.hash = "deadbeef1234abcd"
    FakeRow.subject = "mm: fix oom score"
    FakeRow.body = "This patch fixes..."
    FakeRow.author_name = "Linus Torvalds"
    FakeRow.commit_date = "2024-01-15"
    FakeRow.olk_inclusion_type = "mainline"
    FakeRow.upstream_commit = "upstream123"
    FakeRow.affected_versions = ["OLK-6.6"]
    with patch("storage.pg.engine.get_engine") as mock_eng:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = FakeRow
        mock_eng.return_value.connect.return_value = mock_conn
        result = _get_commit_detail(commit_hash="deadbeef1234")
    assert "mm: fix oom score" in result
    assert "Linus Torvalds" in result


def test_check_backport_status_found():
    from agent.react.tools.code_tools import _check_backport_status
    with patch("graph.queries.check_backport_status",
               return_value={
                   "backported": True,
                   "olk_commit_hash": "abc123",
                   "olk_commit_subject": "mm: fix oom",
                   "inclusion_type": "mainline",
               }):
        result = _check_backport_status(upstream_sha="abc123", olk_version="OLK-6.6")
    assert "BACKPORTED" in result


def test_check_backport_status_not_found():
    from agent.react.tools.code_tools import _check_backport_status
    with patch("graph.queries.check_backport_status",
               return_value={"backported": False, "upstream_sha": "abc123",
                             "olk_version": "OLK-6.6"}):
        result = _check_backport_status(upstream_sha="abc123", olk_version="OLK-6.6")
    assert "NOT backported" in result


def test_get_regression_fixes_none():
    from agent.react.tools.code_tools import _get_regression_fixes
    with patch("graph.queries.get_regression_fixes",
               return_value={"commit_hash": "abc", "has_regression_fix": False,
                             "fixing_commits": []}):
        result = _get_regression_fixes(commit_hash="abc123")
    assert "No known regression" in result


def test_get_commit_diff_not_found():
    from agent.react.tools.code_tools import _get_commit_diff
    with patch("graph.git_diff.get_commit_diff",
               return_value={"found": False, "error": "commit not found"}):
        result = _get_commit_diff(commit_hash="abc123")
    assert "not found" in result.lower() or "Diff not found" in result


def test_get_commit_diff_found():
    from agent.react.tools.code_tools import _get_commit_diff
    with patch("graph.git_diff.get_commit_diff",
               return_value={"found": True, "repo": "OLK-6.6",
                             "diff": "diff --git a/mm/oom.c b/mm/oom.c\n+fix",
                             "truncated": False}):
        result = _get_commit_diff(commit_hash="abc123")
    assert "diff --git" in result
    assert "OLK-6.6" in result

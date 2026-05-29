"""Tests for agent/dmesg_event_writer.py — the diagnosis-session
persistence hook that closes the "I've seen this before" feedback
loop with find_similar_crashes."""

from unittest.mock import MagicMock, patch

from agent.dmesg_event_writer import write_dmesg_event


def _state(**override):
    base = {
        "raw_input": "OLK-6.6: KASAN slab-out-of-bounds in skb_put",
        "fault_kind": "oops",
        "kernel_version": "OLK-6.6",
        "olk_version_tag": "6.6.0-28",
        "input_type": "user-input",
        "react_verdict": "diagnosed",
        "react_iterations": 12,
        "react_tokens_used": 150_000,
        "groundedness": "grounded",
        "kernel_events": [],
        "react_tool_trace": [{"tool": "find_similar_crashes"}],
    }
    base.update(override)
    return base


def test_write_dmesg_event_runs_insert():
    with patch("storage.pg.engine.get_engine") as mock_eng:
        write_dmesg_event(_state())
    # engine.begin() should have been entered → conn.execute called once
    conn = mock_eng.return_value.begin.return_value.__enter__.return_value
    assert conn.execute.call_count == 1
    sql = conn.execute.call_args.args[0]
    assert "INSERT INTO dmesg_event" in getattr(sql, "text", str(sql))
    assert "ON CONFLICT" in getattr(sql, "text", str(sql))


def test_write_dmesg_event_idempotent_event_id_from_content():
    """Same raw_input + fault_kind should produce same event_id, so
    re-runs hit ON CONFLICT DO NOTHING and don't accumulate duplicates."""
    captured_ids = []

    def capture(sql, params):
        captured_ids.append(params["eid"])
        return MagicMock()

    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.begin.return_value.__enter__.return_value
        conn.execute.side_effect = capture
        write_dmesg_event(_state())
        write_dmesg_event(_state())
    assert len(captured_ids) == 2
    assert captured_ids[0] == captured_ids[1]


def test_write_dmesg_event_distinct_input_distinct_id():
    captured_ids = []

    def capture(sql, params):
        captured_ids.append(params["eid"])
        return MagicMock()

    with patch("storage.pg.engine.get_engine") as mock_eng:
        conn = mock_eng.return_value.begin.return_value.__enter__.return_value
        conn.execute.side_effect = capture
        write_dmesg_event(_state(raw_input="case A trace"))
        write_dmesg_event(_state(raw_input="case B trace"))
    assert captured_ids[0] != captured_ids[1]


def test_write_dmesg_event_skips_empty_input():
    """No raw_input means nothing to persist — must not write."""
    with patch("storage.pg.engine.get_engine") as mock_eng:
        write_dmesg_event(_state(raw_input=""))
    mock_eng.assert_not_called()


def test_write_dmesg_event_returns_state_unchanged():
    """Must be a graph node: return state to keep the pipeline running."""
    with patch("storage.pg.engine.get_engine"):
        state_in = _state()
        state_out = write_dmesg_event(state_in)
    assert state_out is state_in


def test_write_dmesg_event_db_failure_is_swallowed():
    """If the DB write fails we must NOT break the agent — the report
    has already been produced. The whole point of this node is the
    learning loop; failure shouldn't bring the user request down."""
    with patch("storage.pg.engine.get_engine") as mock_eng:
        mock_eng.return_value.begin.side_effect = RuntimeError("DB down")
        # Must not raise
        state = write_dmesg_event(_state())
    assert state is not None

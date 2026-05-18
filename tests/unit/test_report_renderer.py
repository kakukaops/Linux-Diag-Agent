"""Unit tests for report renderer (WBS 7.10)."""

import pytest
from agent.report.renderer import render_md, render_json


SAMPLE_STATE = {
    "fault_kind": "oom",
    "fault_summary": "OOM kill of process oom_victim score 800",
    "olk_version_tag": "OLK-6.6",
    "hostname": "testhost",
    "final_analysis": "Memory overcommit caused OOM kill due to lack of swap.",
    "hypotheses": [
        {"id": "h1", "text": "Memory leak in oom_victim", "confidence": 0.8, "status": "confirmed", "supporting_evidence": []},
    ],
    "claims": [
        {"text": "Process had anon-rss 80MB at kill time", "evidence_refs": ["abc1234"], "verified": True},
    ],
    "evidence": [
        {"route": "commit", "title": "mm: fix OOM scoring", "score": 0.9, "commit_hash": "abc1234", "bug_id": None, "cve_id": None},
    ],
    "candidate_analyses": ["analysis 1", "analysis 2"],
    "sop_name": "oom",
}


def test_render_md_contains_fault():
    md = render_md(SAMPLE_STATE)
    assert "oom" in md.lower()
    assert "OOM kill" in md
    assert "OLK-6.6" in md


def test_render_md_contains_hypothesis():
    md = render_md(SAMPLE_STATE)
    assert "h1" in md
    assert "Memory leak" in md


def test_render_md_contains_evidence_table():
    md = render_md(SAMPLE_STATE)
    assert "mm: fix OOM scoring" in md


def test_render_json_schema():
    report = render_json(SAMPLE_STATE)
    assert report["schema_version"] == 1
    assert report["fault"]["kind"] == "oom"
    assert "generated_at" in report
    assert len(report["top_evidence"]) == 1
    assert report["top_evidence"][0]["commit_hash"] == "abc1234"


def test_render_json_system():
    report = render_json(SAMPLE_STATE)
    assert report["system"]["hostname"] == "testhost"
    assert report["system"]["olk_version_tag"] == "OLK-6.6"

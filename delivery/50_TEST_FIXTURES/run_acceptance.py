"""Zero-dependency acceptance validator for the rebuilt system.

依赖：标准库 only。不引入任何第三方包（pytest / pydantic / sqlalchemy 等都不需要）。

用法
====
    # 列所有可用 module
    python run_acceptance.py --list

    # 跑单 module
    python run_acceptance.py --module M1
    python run_acceptance.py --module M5
    python run_acceptance.py --module tool_contracts
    python run_acceptance.py --module behavioral_contracts --result-json /path/to/case.json

    # 跑所有（不含 H/J/L 需要外部依赖的项目）
    python run_acceptance.py --all

退出码
======
    0  全部 PASS
    1  至少一项 FAIL
    2  脚本错误（缺参数 / 文件不存在）

每个 check 的逻辑都嵌在本文件，重建者可单文件拷走在新仓库直接跑。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable


# ── 公共工具 ─────────────────────────────────────────────────────────────

class Result:
    def __init__(self, name: str, ok: bool, message: str = ""):
        self.name = name
        self.ok = ok
        self.message = message

    def __str__(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        out = f"  [{mark}] {self.name}"
        if self.message:
            out += f"\n        {self.message}"
        return out


def _assert(name: str, cond: bool, message: str = "") -> Result:
    return Result(name, bool(cond), message)


# ── M2 · Schema check（要求 psycopg / psycopg2 任一可用） ─────────────────

def check_M2_schema(dsn: str | None = None) -> list[Result]:
    """连 PG 验证 schema。dsn 未提供时跳过。"""
    if not dsn:
        return [Result("M2 schema (skipped, no DSN)", True, "set $DSN to run")]
    try:
        try:
            import psycopg as _pg                                # noqa: F401
            conn = _pg.connect(dsn)
        except ImportError:
            import psycopg2 as _pg                               # noqa: F401
            conn = _pg.connect(dsn)
    except Exception as exc:                                      # noqa: BLE001
        return [Result("M2 schema (connect)", False, f"DB unreachable: {exc}")]

    results = []
    EXPECTED_TABLES = {
        "kernel_commit", "lkml_thread", "lkml_message", "lkml_patch", "lkml_review",
        "bug", "cve", "syzbot_crash",
        "link_commit_bug", "link_commit_message", "link_commit_cve",
        "link_commit_symbol", "link_commit_fixes", "link_commit_revert",
        "link_syzbot_commit",
        "llm_response_cache", "ingest_runs", "dmesg_event",
        "eval_cases", "eval_results",
    }
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema='public' AND table_name = ANY(%s)
    """, (list(EXPECTED_TABLES),))
    present = {row[0] for row in cur.fetchall()}
    results.append(_assert(
        "M2/1 · 20 tables exist",
        present == EXPECTED_TABLES,
        f"missing: {sorted(EXPECTED_TABLES - present)}",
    ))

    cur.execute("""
        SELECT count(*) FROM information_schema.columns
         WHERE column_name='body_tsv' AND is_generated='ALWAYS'
    """)
    body_tsv_count = cur.fetchone()[0]
    results.append(_assert(
        "M2/2 · 5 GENERATED body_tsv columns",
        body_tsv_count == 5,
        f"got {body_tsv_count}, expected 5",
    ))

    cur.execute("""
        SELECT count(*) FROM pg_indexes
         WHERE indexdef ILIKE '%USING gin%body_tsv%'
    """)
    gin_count = cur.fetchone()[0]
    results.append(_assert(
        "M2/3 · 5 GIN body_tsv indexes",
        gin_count >= 5,
        f"got {gin_count}, expected >= 5",
    ))

    cur.execute("""
        SELECT column_name, is_generated FROM information_schema.columns
         WHERE table_name='kernel_commit' AND column_name='short_hash'
    """)
    row = cur.fetchone()
    results.append(_assert(
        "M2/4 · kernel_commit.short_hash is GENERATED",
        row is not None and row[1] == "ALWAYS",
        f"got {row}",
    ))

    conn.close()
    return results


# ── M1 · LLM Provider contract（用 mock，零外部依赖） ──────────────────────

def check_M1_provider() -> list[Result]:
    """用 mock provider 验证 M1 行为契约。"""
    results = []
    try:
        from llm.provider.openai_compat import _build_params
        from llm.provider.base import ChatRequest, Message
    except ImportError as exc:
        return [Result("M1 provider (import)", False, f"{exc}; module not on path")]

    # C4 · max_tokens 默认 ≤ 4096
    req = ChatRequest(messages=[Message(role="user", content="ping")])
    params = _build_params(req, default_model="x")
    results.append(_assert(
        "M1/C4 · max_tokens defaults to ≤ 4096",
        params.get("max_tokens", 0) > 0 and params["max_tokens"] <= 4096,
        f"got max_tokens={params.get('max_tokens')}",
    ))

    # C3 · temperature 默认 0.0
    results.append(_assert(
        "M1/C3 · temperature defaults to 0.0",
        params.get("temperature") == 0.0,
        f"got temperature={params.get('temperature')}",
    ))

    # C5 · httpx trust_env=False（看源码字符串）
    try:
        import inspect
        from llm.provider.openai_compat import OpenAICompatProvider
        src = inspect.getsource(OpenAICompatProvider.__init__)
    except Exception:                                              # noqa: BLE001
        src = ""
    results.append(_assert(
        "M1/C5 · httpx trust_env=False",
        "trust_env=False" in src,
        "source must construct httpx.Client(trust_env=False)",
    ))
    results.append(_assert(
        "M1/C6 · OpenAI SDK max_retries=0",
        "max_retries=0" in src,
        "source must pass max_retries=0 to openai.OpenAI(...)",
    ))

    return results


# ── M5 · Retrieval ───────────────────────────────────────────────────────

def check_M5_retrieval() -> list[Result]:
    results = []
    try:
        from retrieval.schema import RetrievalQuery, RouteTag
        from retrieval.engine import retrieve
    except ImportError as exc:
        return [Result("M5 retrieval (import)", False,
                       f"{exc}; need full retrieval module + DB")]

    try:
        q = RetrievalQuery(
            raw_question="OOM kill in memcg /app/java",
            kernel_version="OLK-6.6",
            keywords=["oom_kill_process", "mem_cgroup_out_of_memory",
                      "CONSTRAINT_MEMCG"],
            routes=[RouteTag(r) for r in ("commit","lkml","bug","syzbot","cve")],
            limit_per_route=10,
        )
        result = retrieve(q)
    except Exception as exc:                                      # noqa: BLE001
        return [Result("M5 retrieval (call)", False, f"{exc}")]

    items = list(getattr(result, "items", []) or [])
    results.append(_assert(
        "M5/1 · evidence pool ≥ 5",
        len(items) >= 5,
        f"got {len(items)}",
    ))

    n_commit = sum(1 for e in items if str(getattr(e, "route", "")).endswith("commit")
                                       and getattr(e, "commit_hash", None))
    results.append(_assert(
        "M5/2 · ≥ 1 commit hit with non-empty commit_hash",
        n_commit >= 1,
        f"got {n_commit}",
    ))

    stubs = [e for e in items if (getattr(e, "title", "") or "")
                                    .startswith("[stub upstream")]
    results.append(_assert(
        "M5/4 · no stub commits in pool",
        len(stubs) == 0,
        f"stub leaked: {[e.title for e in stubs[:3]]}",
    ))

    return results


# ── M6 · dmesg parser ────────────────────────────────────────────────────

def check_M6_parser() -> list[Result]:
    results = []
    try:
        from mcp_servers.dmesg_journal.extractor import extract_events, EventKind
        from agent.react.tools.log_tools import _extract_call_trace
    except ImportError as exc:
        return [Result("M6 parser (import)", False, str(exc))]

    samples = [
        ("oom-kill canonical",
         "[12345.677000] oom-kill:constraint=CONSTRAINT_MEMCG,task=java,pid=8821",
         EventKind.oom),
        ("oom-kill 6.x Killed",
         "[12345.677500] Out of memory: Killed process 8821 (java) total-vm:1G",
         EventKind.oom),
        ("oom-kill 5.x legacy",
         "[ 142.312462] oom_kill_process: Kill process 1234 (java) score 800",
         EventKind.oom),
    ]
    for label, sample, expected_kind in samples:
        events = extract_events(sample)
        ok = (len(events) == 1 and events[0].kind == expected_kind)
        results.append(_assert(
            f"M6/1 · OOM recognised ({label})",
            ok,
            f"got events={[e.kind for e in events]}",
        ))

    # call trace with [timestamp] prefix
    trace = """[12345.680111] Call Trace:
[12345.680112]  <TASK>
[12345.681001]  dump_stack_lvl+0x4d/0x6c
[12345.683003]  oom_kill_process+0x10c/0x110
[12345.685005]  mem_cgroup_out_of_memory+0xed/0x100
[12345.687007]  try_charge_memcg+0x71f/0x880"""
    out = _extract_call_trace(text=trace)
    frame_count = out.count("\n") if "Call trace" in out else 0
    results.append(_assert(
        "M6/2 · extract_call_trace strips [timestamp] prefix",
        "try_charge_memcg" in out and "oom_kill_process" in out and frame_count >= 4,
        out[:200],
    ))

    return results


# ── 30 · tool_contracts ──────────────────────────────────────────────────

def check_tool_contracts() -> list[Result]:
    results = []
    try:
        from agent.react.tools import build_registry
    except ImportError as exc:
        return [Result("tool_contracts (import)", False, str(exc))]
    reg = build_registry()

    # total tool count = 32
    try:
        all_tools = reg.for_route("kernel+vmcore")
    except Exception:
        all_tools = []
    results.append(_assert(
        "tools/1 · ≥ 32 tools registered in full registry",
        len(all_tools) >= 32,
        f"got {len(all_tools)}",
    ))

    kernel_tools = reg.for_route("kernel")
    results.append(_assert(
        "tools/2 · route=kernel exposes ≥ 24 tools",
        len(kernel_tools) >= 24,
        f"got {len(kernel_tools)}",
    ))

    hardware_tools = reg.for_route("hardware")
    results.append(_assert(
        "tools/3 · route=hardware exposes _HW tools (≥ 4)",
        len(hardware_tools) >= 4,
        f"got {len(hardware_tools)}",
    ))

    # Each tool has JSON Schema
    bad = []
    for t in kernel_tools:
        params = getattr(t, "parameters", None) or {}
        if not isinstance(params, dict) or params.get("type") != "object":
            bad.append(t.name)
    results.append(_assert(
        "tools/4 · all tool params are JSON Schema 'object'",
        not bad,
        f"non-object params: {bad[:5]}",
    ))

    return results


# ── 40 · behavioral_contracts（需要 result JSON） ─────────────────────────

def check_behavioral_contracts(result_path: str | None = None) -> list[Result]:
    """检查 ReAct 答案是否含强制小节 + 引用真实工具输出。"""
    if not result_path:
        return [Result("behavioral_contracts (no --result-json)", True,
                       "skipped; pass --result-json /path/to/case_result.json to enable")]
    try:
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except Exception as exc:                                      # noqa: BLE001
        return [Result("behavioral_contracts (load result)", False, str(exc))]

    answer = (data.get("react_final_answer")
              or data.get("final_analysis")
              or "")
    trace = data.get("react_tool_trace") or []
    results = []

    if not answer:
        results.append(_assert(
            "behavioral/0 · result has react_final_answer",
            False,
            "react_final_answer is empty — case likely insufficient_evidence",
        ))
        return results

    # 1 · 候选假设
    has_candidates = ("### 候选假设" in answer) or ("### Candidate hypotheses" in answer)
    results.append(_assert(
        "behavioral/1 · contains '候选假设' / 'Candidate hypotheses'",
        has_candidates,
        "agent skipped hypothesis enumeration",
    ))

    # 2 · 自我批驳
    has_critique = ("### 自我批驳" in answer) or ("### Critique" in answer)
    results.append(_assert(
        "behavioral/2 · contains '自我批驳' / 'Critique'",
        has_critique,
        "agent skipped self-critique",
    ))

    # 3 · 证据来源
    has_trace = ("### 证据来源" in answer) or ("### Evidence trace" in answer)
    results.append(_assert(
        "behavioral/3 · contains '证据来源' / 'Evidence trace'",
        has_trace,
        "agent skipped evidence trace section",
    ))

    # 4 · 引用 SHA 必须在 trace 中出现
    cited_shas = set(re.findall(r"\b[0-9a-f]{12,40}\b", answer))
    trace_shas: set[str] = set()
    for entry in trace:
        preview = entry.get("result_preview") or ""
        trace_shas.update(re.findall(r"\b[0-9a-f]{12,40}\b", preview))
    fabricated = {s for s in cited_shas
                  if not any(s.startswith(t[:12]) or t.startswith(s[:12])
                              for t in trace_shas)}
    results.append(_assert(
        "behavioral/4 · all cited SHAs appear in tool trace",
        not fabricated,
        f"fabricated: {sorted(fabricated)[:5]}",
    ))

    # 5 · 无 DSML 幻觉
    dsml_markers = ["DSML", "<tool_calls>", "<invoke "]
    leaked = [m for m in dsml_markers if m in answer]
    results.append(_assert(
        "behavioral/5 · no DSML / tool-call hallucination tokens",
        not leaked,
        f"leaked: {leaked}",
    ))

    return results


# ── runner ───────────────────────────────────────────────────────────────

MODULES: dict[str, Callable[..., list[Result]]] = {
    "M1":               lambda **_: check_M1_provider(),
    "M2":               lambda **kw: check_M2_schema(dsn=kw.get("dsn") or os.environ.get("DSN")),
    "M5":               lambda **_: check_M5_retrieval(),
    "M6":               lambda **_: check_M6_parser(),
    "tool_contracts":   lambda **_: check_tool_contracts(),
    "behavioral_contracts": lambda **kw: check_behavioral_contracts(kw.get("result_json")),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", choices=list(MODULES) + ["all"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dsn", help="PG DSN for M2 (or $DSN env var)")
    ap.add_argument("--result-json", help="path to case result JSON for behavioral check")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("Available modules:")
        for m in MODULES:
            print(f"  - {m}")
        return 0

    selected = list(MODULES) if (args.all or args.module == "all") \
               else ([args.module] if args.module else [])
    if not selected:
        ap.error("specify --module <name> or --all or --list")
        return 2

    total_results: list[Result] = []
    for mod in selected:
        print(f"\n── {mod} ──")
        rs = MODULES[mod](dsn=args.dsn, result_json=args.result_json)
        for r in rs:
            print(r)
        total_results.extend(rs)

    failed = [r for r in total_results if not r.ok]
    print(f"\n{'='*50}")
    print(f"SUMMARY: {len(total_results) - len(failed)}/{len(total_results)} PASS")
    if failed:
        print(f"  failures:")
        for r in failed:
            print(f"    × {r.name}")
        return 1
    print("  ✓ all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

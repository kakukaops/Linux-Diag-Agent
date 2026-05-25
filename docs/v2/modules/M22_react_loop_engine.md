# M22 — ReAct Loop Engine 设计文档

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格（2026-05-20）。实际实现以代码为准。

| 字段 | 值 |
|------|---|
| 模块编号 | M22（v2 新增）|
| 状态 | Design Draft |
| 关联文档 | [../PRD.md](../PRD.md) · [../Architecture.md](../Architecture.md) · [M7](../../v1/modules/M7_diagnosis_agent.md)（被替代） |
| 关联 ADR | [ADR-019](../adr/ADR-019-hybrid-react-deterministic.md) · [ADR-024](../adr/ADR-024-insufficient-evidence-exit.md) |
| 最后更新 | 2026-05-20 |

---

## 1. 目标与边界

### 1.1 目标

把 v1 的固定 Diagnosis pipeline（`generate_hypotheses → verify_hypothesis → self_consistency`）替换为 **ReAct 调查子图**，让 LLM 在 Investigation 阶段自主选择和调用工具，同时保持 Triage（确定性）+ Report（确定性）的骨架（[ADR-019](../adr/ADR-019-hybrid-react-deterministic.md)）。

### 1.2 关键决策

| # | 决策 |
|---|------|
| D1 | ~~优先扩展 prebuilt~~ → **spike 已否决 prebuilt，采用自研 loop**（2026-05-22；`create_react_agent` 要求 LangChain `BaseChatModel`，与项目 provider 栈不兼容。见 [M22_spike_findings.md](../M22_spike_findings.md)）|
| D2 | 工具集 **按 triage route 动态裁剪**，每次最多暴露 ~15 个工具 |
| D3 | 硬约束：`MAX_ITER=15`、`TOKEN_BUDGET=50K`、单工具 timeout=30s |
| D4 | 工具异常 → `tool result: error: <msg>` 注入 message → LLM 自决；同 (tool, args) 失败 3 次 → 强制 insufficient_evidence |
| D5 | 每步工具调用后写 PG checkpoint（复用 v1 `agent/checkpointer.py`）|

### 1.3 在范围

- ReAct loop 控制器（迭代、预算、终止）
- 工具调用 dispatcher + 异常处理
- 工具集动态裁剪（route → tool subset）
- LLM tool schema 注册与序列化
- prompt 模板（per route system prompt）
- 与 PG checkpointer 集成（每步落库）

### 1.4 不在范围

- 具体工具实现（在 M9 / M10 / M11 / M12 / M4 / M5）
- 报告渲染（在 M7 / M23）
- 评测反馈（在 M23）
- ReAct 中 LLM 模型选择（同 v1，复用 `chat` role）

---

## 2. 整体架构

```
┌────────────────────────── M22 ReAct Loop Engine ─────────────────────────┐
│                                                                          │
│   Input: triage_state (含 route)                                          │
│                                                                          │
│   1. 加载工具集 = route_to_tools[route]                                    │
│   2. 渲染 system prompt = route_to_prompt[route]                          │
│   3. 进入 ReAct loop:                                                     │
│                                                                          │
│      ┌──────── LangGraph create_react_agent (扩展) ────────┐             │
│      │  observe → reason → tool_call → tool_dispatch       │             │
│      │  ↑__________________________________________|        │             │
│      │  hooks: budget_guard / route_filter / failure_handler │             │
│      └──────────────────────────────────────────────────────┘             │
│                                                                          │
│   4. 终止条件检查 → verdict ∈ {diagnosed, insufficient_evidence,           │
│                              max_iter_reached, budget_exhausted}          │
│   5. 输出 final_state（含 messages、tool_call_trace、verdict）             │
│                                                                          │
│   ★ 每步工具调用后 checkpoint 落 PG（复用 v1 PgCheckpointer）              │
└──────────────────────────────────────────────────────────────────────────┘
```

路径建议：`agent/react/` 新目录

```
agent/react/
  __init__.py
  loop.py              # run_react_loop() — 自研 ReAct 循环（不依赖 prebuilt）
  budget.py            # IterationCounter, TokenBudget, FailureCounter
  tool_registry.py     # tool registration + per-route subset
  prompts/
    kernel.md          # 标准 kernel 诊断 prompt
    kernel_vmcore.md   # 带 vmcore 的 prompt
    hardware.md        # 硬件路径 prompt
    change.md          # 变更关联 prompt
    unknown.md         # fallback 全工具 prompt
```

---

## 3. 工具集与路由裁剪

> **实施基础**：工具注册与发现协议见 [V2_v1_modules_supplements.md §3](../V2_v1_modules_supplements.md)（两种 transport `PYTHON` / `MCP_HTTP` 统一抽象、模块 import 时自注册、M22 dispatcher 不关心 transport）。下方表格和 `ROUTE_TO_TOOLS` dict 是**文档化的工具菜单**；运行时**实际通过 `ToolRegistry.for_route(route)` 动态派生**。

triage 输出的 `route ∈ {kernel, kernel+vmcore, hardware, change, unknown}` 决定 LLM 暴露工具：

| 路由 | 必加工具 | 可选 | 工具总数（约）|
|------|---------|------|-------------|
| `kernel` | 类别 A（7 个检索）、D（6 个代码补丁）、B（3 个解析器）| C（4 个监控）、G（5 个变更） | 14-23 |
| `kernel+vmcore` | 上面全部 + 类别 F（5 个 vmcore/drgn）| — | 21-28 |
| `hardware` | 类别 H（4 个硬件）+ 类别 A 中 hw 相关（搜 commit/cve 筛选） | 类别 G | 7-12 |
| `change` | 类别 G（5 个变更）+ A、D（精简）| C | 12-18 |
| `unknown` | 全工具集 | — | ~30 |

**实施细节**：

```python
ROUTE_TO_TOOLS: dict[str, list[str]] = {
    "kernel": [
        # Category A (Knowledge Retrieval)
        "search_commits", "search_lkml", "search_bugs", "search_syzbot",
        "search_cve", "search_code", "lookup_symbol",
        # Category D (Code & Patch)
        "get_commit_detail", "get_commit_diff", "check_backport_status",
        "get_regression_fixes", "get_function_source", "get_call_graph",
        # Category B (Log Parsing)
        "parse_dmesg", "parse_sosreport", "extract_call_trace",
    ],
    "kernel+vmcore": [
        # 上面 + 类别 F
        *ROUTE_TO_TOOLS["kernel"],
        "check_kdump_available", "fetch_debuginfo", "analyze_vmcore",
        "decode_stacktrace", "parse_taint_flags",
    ],
    "hardware": [
        # 类别 H + A 中 hw 子集
        "get_mce_log", "get_edac_errors", "get_ipmi_sel", "get_hardware_inventory",
        "search_commits", "search_cve",  # 限定 hw subsystem
    ],
    "change": [
        "get_package_history", "get_boot_history", "get_kernel_cmdline_diff",
        "get_config_drift", "correlate_fault_with_changes",
        "search_commits", "search_bugs",
        "get_commit_detail", "get_commit_diff",
    ],
    "unknown": ALL_TOOLS,  # 全工具集
}
```

---

## 4. ReAct Loop 控制器

### 4.1 State Schema

```python
class ReactState(TypedDict):
    # 输入（来自 triage）
    triage: TriageState
    route: Literal["kernel", "kernel+vmcore", "hardware", "change", "unknown"]

    # 循环状态
    messages: list[Message]
    iteration: int
    tokens_used: int

    # 工具调用轨迹（用于审计 + 失败检测）
    tool_call_history: list[ToolCallRecord]

    # 终止
    verdict: Literal["diagnosed", "insufficient_evidence",
                     "max_iter_reached", "budget_exhausted"] | None
    final_answer: str | None  # 仅 verdict=diagnosed 时

class ToolCallRecord(TypedDict):
    step: int
    tool: str
    args: dict
    result_summary: str
    error: str | None
    latency_ms: int
    timestamp: datetime
```

### 4.2 主循环（伪代码）

```python
def run(triage_state: TriageState) -> ReactState:
    state = init_state(triage_state)
    tools = ROUTE_TO_TOOLS[state["route"]]
    system_prompt = render_prompt(state["route"], triage_state)

    while True:
        # 终止条件检查（在 LLM 调用前，避免无谓花费）
        if state["iteration"] >= MAX_ITER:
            state["verdict"] = "max_iter_reached"; break
        if state["tokens_used"] >= TOKEN_BUDGET:
            state["verdict"] = "budget_exhausted"; break

        # LLM 推理
        resp = llm.chat(
            messages=[{"role": "system", "content": system_prompt}] + state["messages"],
            tools=[TOOL_SCHEMAS[t] for t in tools],
            temperature=0.0,
        )
        state["tokens_used"] += resp.usage.total_tokens
        state["iteration"] += 1

        # 检查 LLM 输出
        if resp.finish_reason == "stop" and resp.content:
            # LLM 输出 final answer
            if "<insufficient_evidence>" in resp.content:
                state["verdict"] = "insufficient_evidence"
            else:
                state["verdict"] = "diagnosed"
            state["final_answer"] = resp.content
            break

        # 执行工具调用
        for tc in resp.tool_calls:
            if not validate_schema(tc):
                inject_error_message(state, tc, "schema_error: expected ...")
                if count_invalid_calls(state) >= 3:
                    state["verdict"] = "max_iter_reached"; break
                continue

            if is_repeat_failure(state, tc, threshold=3):
                # 同 (tool, args) 失败 3 次 → 强制 insufficient
                state["verdict"] = "insufficient_evidence"; break

            try:
                result = dispatch_tool(tc, timeout=30)
                inject_tool_result(state, tc, result)
            except Exception as exc:
                inject_error_message(state, tc, f"error: {exc}")

        # checkpoint 落库
        checkpointer.save(state)

    return state
```

### 4.3 终止条件

| 终止原因 | 触发 | verdict |
|---------|------|---------|
| LLM 输出 final answer | `<final_answer>...</final_answer>` 标签 | `diagnosed` |
| LLM 主动 insufficient | `<insufficient_evidence>` 标签 | `insufficient_evidence` |
| 同 (tool, args) 失败 ≥3 次 | failure detector | `insufficient_evidence` |
| 迭代数 ≥ MAX_ITER | counter | `max_iter_reached` |
| token 累计 ≥ TOKEN_BUDGET | counter | `budget_exhausted` |
| 同 (tool, args) 重复调用 ≥3 次（非失败）| dedup detector | `max_iter_reached` |

---

## 5. Prompt 模板设计

每路由一份 system prompt 模板，构成 LLM 的 domain prompt 主体。模板存放在 `agent/react/prompts/*.md`。

**通用结构**（所有 prompt 都遵循）：

```markdown
你是一个 Linux 内核故障诊断助手。当前路由：{route}。

## 已知信息（来自 Triage 阶段）
- Kernel 版本：{kernel_version}
- 故障类型：{fault_kind}
- 现场摘要：{fault_summary}
- Taint flags：{taint_flags}
- 是否有 vmcore：{has_vmcore}

## 可用工具
{tools_description}

## 调查策略
（按路由插入）

## 终止条件
- 找到根因 → 输出 `<final_answer>...</final_answer>`，含 commit hash / bug ID 等可追溯引用
- 证据不足 → 输出 `<insufficient_evidence>` 并列出缺什么证据
- 不要重复调用相同工具（同 args 连续 3 次会被强制中断）

## 必须遵守
- 每条结论必须有 evidence_refs
- 禁止编造 commit hash / bug ID
- kernel_version 在所有工具调用里必须传对
```

**路由特定策略**：

| 路由 | 调查策略要点 |
|------|------------|
| `kernel` | 先 `search_commits` + `search_lkml` 找历史修复 → `check_backport_status` 验证；遇到具体函数名调 `lookup_symbol` |
| `kernel+vmcore` | 先 `check_kdump_available` + `fetch_debuginfo` → `analyze_vmcore(query="all_stacks")` → `decode_stacktrace` |
| `hardware` | 先 `get_mce_log` + `get_ipmi_sel` 定位错误源 → `get_hardware_inventory` 拿 DIMM/固件信息 → 输出 RAS 建议 |
| `change` | 先 `get_package_history` + `get_boot_history` → `correlate_fault_with_changes(fault_time)` |
| `unknown` | 先 `parse_dmesg` 拿事件 → 路由意图明确后再走具体路径 |

---

## 6. 集成

### 6.1 与 v1 agent 的关系

```
v1: parse_input → extract_events → classify_fault → retrieve
                                       ↓
                  load_sop → generate_hypotheses → verify_hypothesis
                          → self_consistency → bind_claims → generate_report

v2: parse_input → extract_events → detect_taint_and_hw_signals → classify_fault_and_route
                                                                       ↓
                            ┌────── M22 ReAct Loop ──────┐
                            │  (动态工具调用)              │
                            └──────────────────────────────┘
                                                                       ↓
                                                  bind_claims → generate_report → save_audit_trail
```

`load_sop` / `generate_hypotheses` / `verify_hypothesis` / `self_consistency` 四节点被 M22 替代；其余节点保留。

### 6.2 与 M23（eval）的集成

- M22 输出的 `tool_call_history` 落入 `agent_tool_trace` 表（schema 见 [Architecture §6.3](../Architecture.md)）
- M23 的 eval reporter 从这张表 join `eval_results` 算"工具调用效率"指标

### 6.3 与 PgCheckpointer 集成

复用 v1 `agent/checkpointer.py:get_checkpointer()`。M22 每步循环结束调 `checkpointer.save(state)`，断点续跑时 `checkpointer.load(thread_id)` 取回 state 续做。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| ~~LangGraph prebuilt 不支持必要 hook~~ | ✅ spike 已验证（2026-05-22）：prebuilt 需 `BaseChatModel` 适配器，已否决，采用自研 loop（[M22_spike_findings.md](../M22_spike_findings.md)）|
| ReAct 收敛性差（max_iter 频发）| prompt 调优 + 工具集缩减 + 增加 `<insufficient_evidence>` 引导 |
| token 消耗超预算 | 50K 是基于 GPT-4 估算；切换模型需重新校准（T-026）|
| 单工具长时间阻塞 | 30s timeout + 异步 dispatch |
| 工具调用重复（LLM 卡 pattern）| dedup detector：(tool, args) hash 连续重复检测 |
| invalid tool call 反复 | schema 验证 + 错误注入；累计 3 次强制 final_answer |
| checkpoint 数据膨胀 | tool_result 大对象不入 state，存对象存储；state 只保留 summary |

---

## 8. 实施任务对照（[ProjectPlan](../ProjectPlan.md)）

| Task ID | 内容 | PD |
|---------|------|----|
| T-003 | ReAct Loop Engine（含 prebuilt spike + 失败处理）| 10 |
| T-005 | 工具集按路由动态裁剪逻辑 | 2 |
| T-025 | Prompt 模板设计（按路由）| 3 |
| T-026 | Token cost 模型 + budget 校准 | 1 |
| T-012 | `agent_tool_trace` PG 表 + 迁移 | 1 |

合计 17 PD（v2.0 M22 主线）。

---

*参考：[Architecture.md](../Architecture.md) · [PRD.md](../PRD.md) · [ADR-019](../adr/ADR-019-hybrid-react-deterministic.md)*

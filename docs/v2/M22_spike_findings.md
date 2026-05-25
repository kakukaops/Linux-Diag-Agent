# M22 ReAct — Spike Findings

> **Spike 报告**：本文档记录 M22 启动前的可行性验证结论（ADR-019 §7 要求的 "启动前 1 周 spike"）。结论直接决定 M22 主线实现路径。

| 字段 | 值 |
|------|---|
| 日期 | 2026-05-22 |
| 范围 | LangGraph `create_react_agent` 可行性 · provider 工具调用能力 · 后端选型 |
| 关联 | [modules/M22_react_loop_engine.md](modules/M22_react_loop_engine.md) · [adr/ADR-019-hybrid-react-deterministic.md](adr/ADR-019-hybrid-react-deterministic.md) |
| 产出代码 | `agent/react/` · `llm/provider/openai_compat.py`（工具调用补全）|
| 验证 | 150 单元测试通过（含 15 个 spike 新测试）+ 实网 round-trip（OpenRouter，2 轮收敛）|

---

## 1. Spike 要回答的问题

ADR-019 D1：「**优先扩展 LangGraph `create_react_agent` prebuilt**；从头自研为 fallback」。spike 必须验证 prebuilt 路径是否可行，并退化决策。

---

## 2. 发现

### Q1 — `create_react_agent` 能否直接驱动本项目的 LLM？ **不能。**

`langgraph 1.2.0` 的 `create_react_agent(model, tools, ...)` 的 `model` 参数类型是
`str | BaseChatModel | Runnable | Callable[..., BaseChatModel]` —— 必须是 **LangChain `BaseChatModel`**。

本项目的 `LLMProvider`（`llm/provider/base.py`）是自定义 `Protocol`（`chat()` / `chat_stream()` / `health_check()`），**不是** LangChain 模型。要用 prebuilt 有两条路，都不可接受：

- **(a) 直接用 LangChain 原生 chat model（如 `ChatOpenAI`）** → 丢掉 `claude_code` 后端（默认后端，`claude -p` 无对应 LangChain 模型）、PG prompt cache、滑窗限流器（Pro 配额 40 msg/5h 的关键防护）、JSONL 可观测性。
- **(b) 写一个 `BaseChatModel` 适配器内部转调 `provider.chat()`** → 需在 LangChain `BaseMessage` ↔ 本项目 `Message`、工具 schema、`tool_calls` 三处做双向翻译。这是在一个**已经原生使用 OpenAI 工具调用 schema** 的栈之上再叠一层易碎的翻译层，且随 LangChain 版本漂移。

→ **prebuilt 不是"零胶水"路径**，D1 的前提不成立。

### Q2 — 本项目 provider 能否做客户端侧工具调用？

**`openai_compat`：schema 早已定义，但从未接线 —— 发现并修复 3 处缺口。**

| # | 缺口 | 影响 | 修复 |
|---|------|------|------|
| 1 | `_build_params` 丢弃出站消息的 `tool_calls` / `tool_call_id` / `name` | 多轮 ReAct 对话无法回放给模型（助手发起的调用 + 工具结果全丢）| 新增 `_msg_dict()` |
| 2 | `chat_stream` 只读 `delta.content`，忽略 `delta.tool_calls` | 模型返回的工具调用被丢在地上 | 新增 `_accumulate_tool_call()` 按 `index` 累积分片 |
| 3 | `_assemble` 不回填 `ChatResponse.tool_calls` | 即便流里有，`finish_reason` 也对不上 | `_drain_tool_calls()` + `finish_reason` 归一化 |

三处均为**补全**（`ChatChunk.tool_call_delta`、`ChatResponse.tool_calls` 字段本就存在）。OpenAI 把工具调用的 `arguments` JSON 按片流式下发，重组逻辑是核心难点 —— 已用合成分片块单测覆盖（`test_openai_compat_toolcalls.py`，8 例）。

**`claude_code`：结构上不适配客户端侧 ReAct 循环。**

`claude -p` 子进程**本身就是一个自主 agent**：给它 `--allowedTools`，它会跑自己的内部循环并**自行执行**工具（须 MCP 注册），不会把 `tool_calls` 交回给调用方派发。M22 要按 ADR-019 D3/D4/D5 自己掌控**迭代数 / token 预算 / 每步 PG checkpoint / 重复失败检测** —— 与 `claude -p` "自己执行" 的模型冲突。`chat()` 里 "Patch tool_calls from the raw stream" 只是空注释桩。

> 备选「把工具暴露成 MCP server、让 `claude -p` 自驱循环」**已评估并否决**：交出 D3/D4/D5 控制权；且 Pro 限流 40 msg/5h 下一次 15 迭代调查约耗 15 条消息 → 每 5h 仅 ~2.6 次调查,不可行。

### Q3 — 后端选型

**M22 ReAct 的 `chat` role 必须用 `openai_compat` 后端。** 本环境已默认如此（`configs/local.yaml`：`deepseek/deepseek-v4-flash:free` via OpenRouter）。`claude_code` 后端继续服务**确定性节点**（triage 分类、query parse、报告生成 —— 单发、无工具循环）与 v1 兼容。

---

## 3. 决策

| # | 决策 |
|---|------|
| **S1** | **否决 `create_react_agent` prebuilt，走 ADR-019 D1 的 fallback：自研 ReAct 循环。** 理由：(1) prebuilt 仍需 `BaseChatModel` 适配器，非零胶水；(2) 本项目 provider 栈已原生使用 OpenAI 工具调用 schema（`ToolSchema`/`ToolCall`/`finish_reason="tool_calls"`），自研循环直接复用，无翻译层；(3) ADR-019 D3/D4/D5 的预算/失败/checkpoint 控制本就是自定义逻辑，塞进 prebuilt 的 hook 与自研循环工作量相当。 |
| **S2** | M22 ReAct 固定使用 `openai_compat` 后端（`chat` role）。 |
| **S3** | 建议修订 **ADR-019 D1** 与 **M22 §1.2 D1**：prebuilt 路径已 spike 否决，指向本文档。 |

---

## 4. 本次 spike 产出

**`llm/provider/openai_compat.py`** —— 工具调用补全（`_msg_dict` / `_accumulate_tool_call` / `_drain_tool_calls`，`_assemble` 回填）。

**`agent/react/`** —— ReAct 循环原型骨架：
- `tool_registry.py` — `Tool` + `ToolRegistry`（按 route 裁剪工具集 `for_route` / `schemas`）
- `loop.py` — `run_react_loop()`：observe → reason → tool_call → dispatch 主循环 + `MAX_ITER` 上限；终止判定 `diagnosed` / `insufficient_evidence` / `max_iter_reached`

**测试**（15 例，全过）：
- `test_openai_compat_toolcalls.py` — 流式分片重组、并行工具调用、消息回放、`finish_reason` 归一化
- `test_react_loop_spike.py` — fake provider 驱动：工具派发→结果回灌→诊断终止、`MAX_ITER`、工具异常回灌、`insufficient_evidence`、route 裁剪

---

## 5. M22 主线遗留任务（spike 范围外）

`agent/react/loop.py` 中已用 `TODO(M22)` 标注：

| Task | 内容 |
|------|------|
| T-003 | 重复失败检测（同 `(tool, args)` 失败 ≥3 次 → `insufficient_evidence`）；invalid schema 注入 |
| T-026 | token 预算守卫（`TOKEN_BUDGET` → `budget_exhausted`）|
| D5 | 每步 PG checkpoint（复用 `agent/checkpointer.py`）|
| T-005 | route → 工具集动态裁剪接入 `agent/graph.py` 图层 |
| T-025 | per-route system prompt 模板（`agent/react/prompts/*.md`）|
| — | 把 M22 子图接入 `agent/graph.py`，替换 `load_sop`/`generate_hypotheses`/`verify_hypothesis`/`self_consistency` 四节点 |

**实网 round-trip 已完成（2026-05-22）**：OpenRouter `deepseek/deepseek-v4-flash:free`，ReAct 循环 2 轮收敛 —— LLM 发起 `get_meminfo` 工具调用 → 流式 `tool_calls` 被 `_accumulate_tool_call` / `_drain_tool_calls` 正确重组 → 派发 → 结果回灌 → 第 2 轮 LLM 输出最终诊断，且诊断**引用了工具返回的真实数值**（118 MB free / swap 耗尽）。期间 `openai_compat` 的 429 自动重试也透明生效。端到端验证通过。

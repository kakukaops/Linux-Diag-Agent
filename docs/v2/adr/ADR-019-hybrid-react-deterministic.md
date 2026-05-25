# ADR-019 — Hybrid 模式：Triage 确定性 + Investigation ReAct + Report 确定性

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-20 |
| 决策者 | 用户 + Architect |
| 关联模块 | M7（agent）· 新 M22（ReAct Loop Engine；原编号 M13，已重编以避里程碑冲突）|
| 相关 ADR | [ADR-013](../../v1/adr/ADR-013-m5-retrieval-strategy.md) · [ADR-015](../../v1/adr/ADR-015-m7-diagnosis-strategy.md) · [ADR-024](ADR-024-insufficient-evidence-exit.md) |
| 设计基础 | [AgentThink.md §三、§5.2、§5.3](../../AgentThink.md) |

## 上下文

v1 是固定 10 节点流水线，LLM 在每个节点只做文本生成、不能自主调用工具（`llm/provider/base.py` 定义了 `ToolCall`/`ToolSchema` 但从未传给 LLM）。这意味着：

- LLM 不能根据诊断中途的发现决定"我需要查这个进程的内存布局"
- 检索路由（7 路中调哪路、用什么关键词）由规则决定
- SOP steps 只是字符串列表，不是 LLM 可执行的步骤

业界对比（[AgentThink §一](../../AgentThink.md)）：Google SRE Agent、Microsoft Azure SRE Agent、OpenSRE 在**线上事故响应**场景全部采用 ReAct 循环，因为 ReAct 的紧密 perception-action 反馈匹配"故障调查需要根据中间发现决定下一步"的本质。

但 v1 的 pipeline 也不是纯缺点——**报告生成 / 证据绑定阶段的确定性是优点**：同一个故障应该产同一份 RCA，可复现、可审计。

## 决策

**v2 采用三阶段 Hybrid Agent**：

```
┌─ 阶段一 Triage（确定性，3-4 节点）──┐
│  parse_input → extract_events       │
│  → detect_taint_and_hw_signals      │
│  → classify_fault_and_route          │
└─────────────────────────────────────┘
                ↓
┌─ 阶段二 Investigation（ReAct 子图）─┐
│  LLM 自主调用工具：                  │
│   loop:                              │
│     observe state → reason →         │
│     select & call tools →            │
│     until final_answer /             │
│           max_iter / budget /        │
│           insufficient_evidence      │
└─────────────────────────────────────┘
                ↓
┌─ 阶段三 Report（确定性，3 节点）────┐
│  bind_claims → generate_report      │
│  → save_audit_trail                 │
└─────────────────────────────────────┘
```

**核心边界**：

- **确定性阶段**（Triage、Report）：不允许 LLM 自由发挥，逻辑可复现
- **ReAct 阶段**（Investigation）：LLM 可自主调用工具集，但有硬约束（见下）

## ReAct 工程约束（必须实现）

| 约束 | 默认值 | 理由 |
|------|--------|------|
| `MAX_ITER` 最大迭代数 | 15 | 防止 LLM 在工具调用里打转 |
| `TOKEN_BUDGET` 单次诊断 token 上限 | 50K | 成本可控 |
| 单工具超时 | 30s | 防止单工具卡死整个 loop |
| 每步 checkpoint | 必须 | 复用 v1 PG checkpointer，中断可恢复 |
| 工具集动态裁剪 | 按 triage route | 每次最多暴露 ~15 个工具，避免 schema 过长 |
| 同 tool+args 连续 3 次 → 强制 final_answer | 必须 | 检测循环不收敛 |
| **工具异常处理** | **必须**：异常 → `tool result: error: <msg>` 注入 messages → LLM 自主决定（重试/换工具/放弃）。同一 (tool, args) 失败累计 3 次后**强制走 insufficient_evidence** 路径 | 不让单工具崩溃中断整个 loop；也防止 LLM 顽固重试相同失败 |
| **LLM 输出 invalid tool call**（schema 不匹配）| 注入 `tool result: schema_error: expected ...` 给 LLM 自我修正；累计 3 次 invalid → 强制 final_answer | 模型偶发会输出错误格式 |

## 工具集按路由裁剪

triage 输出的 `route ∈ {kernel, kernel+vmcore, hardware, change, unknown}` 决定 Investigation 阶段暴露给 LLM 的工具菜单：

| 路由 | 必加工具 | 可选 |
|------|---------|------|
| `kernel` | 类别 A 检索、D 代码补丁、B 解析器 | C 监控、G 变更 |
| `kernel+vmcore` | 上面全部 + 类别 F vmcore/drgn | — |
| `hardware` | 类别 H 硬件 + 类别 A 中 hw 子集 | G 变更 |
| `change` | 类别 G 变更 + A、D | C 监控 |
| `unknown` | 全工具集 | — |

## 影响

### 优势

| 维度 | 影响 |
|------|------|
| **故障调查能动性** | LLM 可根据中间发现决定下一步（如发现 oom_score=999 → 主动查 cgroup 限制变更） |
| **报告确定性** | Report 阶段不变，同输入产同 RCA，可审计可复现 |
| **工具生态可扩展** | 新工具实现 LLM tool schema 即可挂入，不改 graph 结构 |
| **配额可控** | budget guard + max_iter 让单次诊断成本可预测 |
| **与业界对齐** | ReAct 是 SRE agent 主流范式（Google ADK / Azure SRE Agent / OpenSRE 60+ tools） |

### 代价

| 维度 | 影响 |
|------|------|
| **架构复杂度** | ReAct loop engine 是新模块（M22），需要 ~10 PD（含 LangGraph prebuilt spike + 工具失败处理；详见下方"实施路径"）|
| **Token 消耗增加** | Investigation 阶段会比 v1 多调用 LLM，但 50K budget 可控 |
| **调试难度** | ReAct 轨迹比线性 pipeline 难定位问题，需要 audit trail + trace UI |
| **不确定性** | ReAct 路径不固定，相同输入可能走不同工具序列；用 temperature=0.0 + tool selection 锁死缓解 |

## 实施路径

> **Spike 已完成（2026-05-22）**：下方"优先扩展 prebuilt"的原始设想经 spike 验证**已否决**，最终采用**自研 ReAct loop**。详见 [M22_spike_findings.md](../M22_spike_findings.md)。原始论证保留如下，记录决策演化。

**原始设想**：LangGraph 提供 `create_react_agent` prebuilt，已内置 tool dispatch / message history / checkpointer 集成；预期所有定制需求（budget guard / 路由裁剪 / 失败处理）可通过 hook 实现，省 3-5 PD。

**Spike 结论（否决 prebuilt）**：`create_react_agent` 要求 LangChain `BaseChatModel`，而本项目 `LLMProvider` 是自定义 Protocol。直接用 LangChain 原生 chat model 会丢掉 `claude_code` 后端、PG cache、限流器；写 `BaseChatModel` 适配器则是在已原生支持 OpenAI 工具 schema 的栈上叠一层易碎翻译层。故触发本 ADR 第 2 条预留的 fallback —— **自研 loop**（`agent/react/loop.py`），直接驱动项目 provider 栈，无翻译层。

**工时**：T-003 仍定为 **10 PD**（spike + 自研 loop + 失败处理 + budget guard）。

## 备选方案（已否决）

### 备选 A：纯 ReAct（全部 10 节点都 agent 化）

- **优势**：架构最简单，统一 ReAct loop
- **劣势**：报告阶段无确定性 → 同输入不同 RCA，不可审计；config bind_claims 这种本质是 SQL JOIN 的步骤交给 LLM 是浪费

否决理由：报告阶段的可复现性 / 可审计性是产品硬约束（[PRD §4.2](../PRD.md)）。

### 备选 B：保持 v1 流水线，只在某节点内允许 LLM 调用工具

- **优势**：改动最小
- **劣势**：仍不是真正的 ReAct（不能跨节点根据发现回头查）；hypotheses → verify → consistency 三节点的固定顺序与"先看证据决定调查方向"冲突

否决理由：解决不了 v1 的核心问题（LLM 无自主性）。

### 备选 C：Plan-and-Execute（先规划全步骤再执行）

- **优势**：可预测；适合离线分析
- **劣势**：在线故障调查需要根据中间发现调整，无法预先规划

否决理由：[AgentThink §一](../../AgentThink.md) 业界共识——线上事故响应 ReAct 占主导。

## 验证

- **Spike 验证**：✅ 已完成（2026-05-22，见 [M22_spike_findings.md](../M22_spike_findings.md)）—— `openai_compat` 工具调用补全 + 自研 ReAct loop 原型，15 单元测试 + 实网 round-trip（OpenRouter，2 轮收敛）通过
- **acceptance**：v2.0 验收要求 ReAct 调查循环跑通 5 例真实 incident（[PRD §6.1](../PRD.md)）

## 未来回顾

**M15 v2.0 验收**（[PRD §6.1](../PRD.md)）后立即回顾以下指标：

- ReAct 平均迭代数（目标中位数 ≤ 8）
- max_iter / budget 触发率（目标 < 10%）
- 同输入产同结论的比率（自由度过高的信号，目标 ≥ 80% 在确定性 Report 阶段实现）
- 工具失败 → insufficient_evidence 路径的命中率（避免被频繁 short-circuit）

若指标不达标，在 M16 启动前调整：限制 ReAct 子图的可调用工具数 / 调整 prompt / 引入显式规划步骤。

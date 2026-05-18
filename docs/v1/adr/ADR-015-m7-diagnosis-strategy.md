# ADR-015 — M7 诊断 Agent 核心策略：LangGraph + Reject-Regenerate + 3 SOP

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M7 |
| 相关 ADR | [ADR-001](ADR-001-no-embedding.md) · [ADR-002](ADR-002-no-reranker.md) · [ADR-004](ADR-004-claude-code-provider.md) · [ADR-013](ADR-013-m5-retrieval-strategy.md) |

## 上下文

M7 诊断 Agent 需要决定三个核心策略：

1. **Agent 编排框架**：LangGraph / 自写状态机 / CrewAI / AutoGen
2. **Claim-Evidence Binding 强制级别**：Reject + Regenerate / Mark speculative / 严格 Reject 无上限
3. **v1.0 SOP 覆盖故障域数量**：3 / 5 / 8

每个决策都影响 v1.0 工作量、Pro 5h 配额消耗、与现有 ADR 的一致性。

## 决策

### D1 — Agent 编排框架 = LangGraph

**理由**：
- 与 M1 Architecture 已规划一致（不引入新依赖）
- LangChain 生态成熟，状态机 + tool calling + checkpointing 原生支持
- OpenAI Chat schema（[ADR-003](ADR-003-openai-chat-schema.md)）原生兼容
- 支持 Anthropic / vLLM / 本地模型多 backend（[ADR-004](ADR-004-claude-code-provider.md)）
- OpenTelemetry tracing 集成
- 双层 Agent（Triage + Diagnosis）用两个独立 graph 表达，逻辑清晰
- Self-Consistency K=3 可并行 checkpoint 不冲突

### D2 — Claim-Evidence Binding = Reject + Regenerate ≤ 2 次，超限标 `speculative`

**理由**：
- 平衡 [ADR-001](ADR-001-no-embedding.md) "无幻觉哲学" 与 Pro 5h 配额现实
- 严格 Reject 无上限会导致单个诊断耗尽配额（worst case 10+ regenerate）
- 仅 Mark speculative 让 LLM 思考轨迹保留，但不符合"claim 必须有证据"硬约束
- 2 次重试通常足够（第一次给 invalid 反馈，第二次按提示修复）
- 超限 → speculative 标记 → 不进结论但可被人审；与"零幻觉但保留思考"的中间立场吻合

**实现**：
- draft → parse claims → validate refs → invalid 触发 regenerate → 2 次后仍 invalid → 标 speculative

### D3 — v1.0 SOP 覆盖 = 3 个（OOM / lockup / panic）

**理由**：
- 30 例评测样本（[ADR-010](ADR-010-lkml-2y-bootstrap.md) / [ADR-011](ADR-011-bugzilla-kernel-org-only.md)）主要分布在这 3 类
- 每个 SOP yaml 编写 + fixture 准备工作量 ~1.5 PD
- 5 个 SOP 工作量约 60% 反升（fixture 难找）
- 8 个全集会延期 v1.0 上线
- 留 v1.2 评测后扩展（基于真实样本分布优先级调整）

### D4 — Self-Consistency 默认 K=1，评测显式 K=3，Pro budget < 10 强制 K=1

**理由**：
- K=1 每诊断 ~3-5 messages；Pro 45/5h 可跑 9-15 诊断
- K=3 每诊断 ~9-15 messages；Pro 仅可跑 3-5 诊断/5h
- 评测脚本启用 K=3 是合理（30 例评测可分多日跑）
- 配额吃紧时降级保命

### D5 — 报告输出 = Markdown + JSON 双格式

**理由**：
- Markdown 是用户/工程师默认阅读格式
- JSON 是评测 / 后续 UI / 自动化处理所需
- 两者共享底层数据结构（`DiagnosisReport` dataclass），单份生成双格式渲染

## 影响

### 工作量估算（含 ADR-015 决策）

| 子模块 | 行数估计 |
|--------|---------|
| LangGraph triage + diagnosis graph | ~600 |
| Hypothesis 生成 + 验证 + 收敛 | ~400 |
| SOP yaml × 4（3 + generic）+ registry | ~200 |
| Claim-Evidence Binding（parse + validate + regenerate）| ~400 |
| 报告生成（Markdown 模板 + JSON schema）| ~300 |
| CLI diagnose 子命令 | ~200 |
| 测试 + fixture | ~500 |
| **M7 总计** | **~2600 行** |

加上 M1-M6 ~6400 行，**v1.0 累计 ~9000 行 Python**（不含 M8 评测）。

### Pro 5h 配额下吞吐

| 场景 | Messages / diagnose | Diagnosis / 5h |
|------|-------------------|---------------|
| Triage simple（直答）| 2 | ~22 |
| Diagnosis K=1（无 regenerate）| 4 | ~11 |
| Diagnosis K=1（含 1 次 regenerate）| 5 | ~9 |
| Diagnosis K=1（含 2 次 regenerate）| 6 | ~7 |
| Diagnosis K=3（评测） | 12 | ~3 |

30 例评测（混合场景）真实耗时：**3-5 个 5h 窗口（2-3 天）**。

### 与已有 ADR 一致性

| ADR | 一致性 |
|-----|------|
| [ADR-001](ADR-001-no-embedding.md) 无 embedding | ✓ M7 不引入 embedding |
| [ADR-002](ADR-002-no-reranker.md) 无 cross-encoder reranker | ✓ M7 不直接做 rerank（在 M5 内） |
| [ADR-003](ADR-003-openai-chat-schema.md) OpenAI Chat schema | ✓ LangGraph 兼容 |
| [ADR-004](ADR-004-claude-code-provider.md) claude_code provider | ✓ 通过 M1 抽象层调用 |
| [ADR-008](ADR-008-reuse-codesearch.md) codesearch reuse | ✓ M7 通过 M5 间接用 codesearch |
| [ADR-013](ADR-013-m5-retrieval-strategy.md) M5 always-fire-all | ✓ M7 每个 hypothesis verify 都触发 M5 retrieve |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **D1 自写状态机** | 失去 LangGraph 的 checkpointing / streaming / tool calling 原生支持；维护成本反升 |
| **D1 CrewAI / AutoGen** | 多 agent role 抽象对 2 层架构是过度设计；输出不可预测；不易做 hypothesis 严格收敛 |
| **D2 仅标 speculative，不重生成** | 不符合 ADR-001 "无幻觉" 硬约束；报告 speculative 占比可能 30%+ |
| **D2 严格 Reject 无上限** | Pro 5h 配额下单诊断可能耗 20+ messages，无法做 30 例评测 |
| **D3 5 / 8 SOP** | v1.0 工时反升 50-100%；评测 30 例样本未必每类都有；MVP 求收敛优于求广覆盖 |
| **D4 默认 K=3** | Pro 5h 配额下 3-5 诊断/窗口，评测 30 例需 7+ 天；非必要不开 |
| **D5 仅 Markdown** | 评测 / 后续自动化 / UI 需要 JSON；双格式增量成本极低 |

## v1.1+ Review 触发条件

满足以下任一时，重评估本 ADR：

1. LangGraph 演进与 OpenAI Chat schema 不兼容 → 评估自写或切框架
2. Claim binding regenerate 平均次数 ≥ 1.5 → 加严初始 prompt 或加重试上限
3. 30 例评测 SOP 命中率 < 50% → 紧急扩到 5 SOP
4. 切到本地 vLLM 后配额无限制 → 默认 K=3
5. JSON 输出在评测 / UI 上发现缺关键字段 → schema 演进

## 监控指标

- `m7_agent_framework{name=langgraph}` — 标识当前框架
- `m7_invalid_claims_total{reason=invalid_format|not_in_evidence}` — binding 失败率
- `m7_regenerate_attempts_total{attempt=1|2}` — 重试分布
- `m7_speculative_claims_total` — 最终未解决的 claim 数
- `m7_sop_matched_total{sop_id}` — 各 SOP 命中分布
- `m7_self_consistency_k_distribution` — 实际 K 值分布
- `m7_diagnosis_messages_total{phase}` — Pro 配额消耗分布

## 参考

- M7 模块设计：[M7_diagnosis_agent.md](../modules/M7_diagnosis_agent.md)
- LangGraph 文档：https://langchain-ai.github.io/langgraph/
- [ADR-013 M5 检索策略](ADR-013-m5-retrieval-strategy.md)
- Wang et al., "Self-Consistency Improves Chain-of-Thought Reasoning"（ICLR 2023）— Self-Consistency 算法

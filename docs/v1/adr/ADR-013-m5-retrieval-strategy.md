# ADR-013 — M5 检索策略：全 LLM 查询解析 + Always-fire-all 7 路召回

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M5 |
| 相关 ADR | [ADR-002](ADR-002-no-reranker.md) · [ADR-004](ADR-004-claude-code-provider.md) · [ADR-008](ADR-008-reuse-codesearch.md) |

## 上下文

M5 检索编排层需要决定两个关键策略：

1. **Query 解析**：自然语言 → 结构化字段（kernel_version / fault_domain / subsystem_hint / error_keywords / stack_frames）如何实现？
   - 规则为主 + LLM 兜底（低成本，但可能漏字段）
   - 全 LLM 解析（高质量，每查询消耗 1 message）
   - 纯规则（最便宜，但漏字段）

2. **多源召回触发**：每次 retrieve 是否触发全部 7 路？
   - Hybrid：基于 query 类型选默认 firing pattern，agent 可 override（节省资源）
   - Always-fire-all：无差别触发 7 路（最广覆盖，资源开销大）
   - 保守组合（如 CodeGraph + LKML 两路）：最节省，但召回不全

## 决策

**v1 采用：**

- **D1：全 LLM 查询解析**（每查询 1 Navigator message）
- **D2：Always-fire-all 7 路无差别召回**（code + docs + lkml + bug + syzbot + commit + cve）

## 影响

### Pro 5h 配额预算重算

| 步骤 | Message 数 | 触发频率 |
|------|----------|--------|
| query parse | 1 | 每查询必发 |
| PageIndex select × 4（条件触发）| 4 | 概念类查询或 BM25 不足时 |
| LLM rerank | 1 | 候选 > 10 时（多数查询）|

| 场景 | 总 messages | Pro 5h（45 msg）支持查询数 |
|------|-----------|-----------------------|
| Incident 类（无 PageIndex）| 2/query | ~22 |
| Concept 类（含 PageIndex）| 6/query | ~7 |
| 混合（50/50）| 4/query 平均 | ~11 |

**30 例评测预期耗时**：2-3 天（视 concept 类占比，跨 3-4 个 5h 窗口）。

### 优势

| 维度 | 选择 |
|------|------|
| **query 解析质量** | 全 LLM 解析比规则更鲁棒；复杂 NL（多句、含调用关系、含时间信息）覆盖更全 |
| **召回完整性** | Always-fire 保证不漏召回；评测阶段尤其重要（不希望因路由错误导致漏证据）|
| **架构简洁** | 不维护 query 类型分类器；不维护"何时触发某路"的规则集；M5 路由逻辑极薄 |
| **可观测性** | 每路 candidates 数 + latency 都可独立监控，便于发现哪路退化 |
| **未来 vLLM** | v1.3 切本地后无配额限制，always-fire 完全无成本压力 |

### 代价

| 维度 | 代价 |
|------|------|
| **Pro 配额吞吐** | MVP 阶段查询频次受限（混合 11 queries / 5h）|
| **资源开销** | CodeGraph + PG + Neo4j 每查询都被命中 7 路；CPU/IO 利用率高 |
| **延迟** | 多源并行最慢路 P95 决定 retrieve 总耗时（~800ms vs hybrid ~400ms）|
| **冗余结果** | 简单 concept 类查询（"什么是 PSI"）也会触发 LKML/Bug/syzbot 召回，多数返空 |

### 缓解

| 风险 | 缓解 |
|------|------|
| Pro 配额耗尽 | budget.remaining_messages() < 3 时禁 PageIndex；< 1 时禁 rerank；query parse 永远触发 |
| 评测期长 | 30 例分多日跑；同 NL 重复评测时 M1 PG cache 命中零消耗 |
| 无价值召回浪费 | v1.2 评测后如发现某路常返空（如简单 concept 查询的 syzbot 路），可在 v1.3 加 selective firing；本 ADR 不锁死永久 always-fire |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| 规则为主 + LLM 兜底（D1）| 规则维护成本高，且漏字段问题难发现 |
| 纯规则（D1）| 复杂 NL 处理弱；不符合"LLM 友好"的项目哲学 |
| Hybrid 路由（D2）| 引入 query 类型分类器，新增维护负担；MVP 阶段先求广覆盖再优化 |
| 仅 CodeGraph + LKML 两路（D2）| 丢失 syzbot 主动召回（30 例评测样本主要来源），评测质量直接受损 |

## v1.2+ Review 触发条件

满足以下任一时，重评估本 ADR：

1. Pro 5h 配额成为评测瓶颈（吞吐 < 10 queries/5h 持续 2 周）
2. 某路连续 100 次 retrieve 返回 0 candidates（说明此 query 类型不需该路）
3. 切到本地 vLLM 后，always-fire-all 的资源开销在 GPU 上不可接受
4. 用户报告"简单查询也要等多源召回，体感慢"

满足时：
- D1 可降级到"规则为主 + LLM 兜底"
- D2 可切到 hybrid（基于 query_intent 选默认 routes）

## 监控指标

- `m5_query_parse_messages_total{trace_id}` — 每查询消耗
- `m5_route_candidates{route}` — 每路召回数（如某路 P95 = 0 提示无价值）
- `m5_rerank_triggered_ratio` — rerank 触发比例
- `m5_pageindex_triggered_ratio` — PageIndex 触发比例
- `llm_rate_budget_remaining` — Pro 配额实时
- `m5_retrieve_duration_p95` — 端到端延迟

## 实施约束（2026-05-15 Spike 反馈）

来自 [Spike_Report.md](../spike/Spike_Report.md) §2.2 实证：在 codesearch v1.27.0 上对 `oom_kill_process` 同一 query 测试两种模式：

| `search_code` mode | 结果 |
|--------------------|------|
| `mode='symbol'` | "No code matches found"（漏召回）|
| `mode='literal'` | 12ms 命中 6 处 + **自动附带 SCIP 精确定义**（`mm/oom_kill.c:1092`）|

原因：codesearch 的 literal 模式走 Zoekt 三元组索引 + 在结果中合并 SCIP 命中；symbol 模式仅查 SCIP 索引，对宏展开 / 头文件声明 / EXPORT_SYMBOL 等场景覆盖不全。

### F7：M5 调用 CodeGraph `search_code` 默认 `mode='literal'`

| 约束 | 内容 |
|------|------|
| **默认** | 代码召回路（route = `codegraph_code`）默认使用 `search_code(mode='literal')` |
| **补充** | `mode='symbol'` 仅在 query parser 明确识别为"纯符号定义查询"（如 NL 仅提到一个 C 函数名且无其他上下文）时使用 |
| **首选** | 若 query 已含明确符号名（函数名 / 类型名），**优先**调用 `lookup_symbol(action='definition')`，跳过 `search_code` — 这是 codesearch 系统提示中明示的推荐路径 |

### 后果

- M5 路由代码（`m5/routes/code.py`）中 `search_code` 调用默认参数固定为 `mode='literal'`
- query parser 不强求识别 "符号 vs 文本" 二分类（symbol mode 仅作辅助）
- 监控指标增加 `m5_codegraph_mode_distribution{mode}` 用于事后评估

## 参考

- [M5 检索编排设计文档](../modules/M5_retrieval_orchestration.md)
- [ADR-004 claude_code provider + Pro 订阅限制](ADR-004-claude-code-provider.md)
- [M1 LLM Provider 抽象层](../modules/M1_llm_provider.md)

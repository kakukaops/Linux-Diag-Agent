# ADR-002 — 不使用 Cross-Encoder Reranker，以 LLM 重排替代

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M1, M5 |
| 相关 ADR | ADR-001（无 embedding） |

## 上下文

[ADR-001](ADR-001-no-embedding.md) 锁定全项目无 embedding 后，原 M1 方案中保留的 cross-encoder reranker（bge-reranker-v2-m3）与"PageIndex 哲学扩展到所有数据源"的架构纯净度产生冲突 ‒ PageIndex 官方实现明确是 "vectorless" 且"没有任何 scoring/reranker 组件，完全依靠 LLM tree-walking 推理排序"。

而我们的目标是**提升智能体的诊断准确率**，所以精排不能没有，关键是选哪种精排器：

| 候选 | 准确率提升（vs 不精排） | 与 PageIndex 哲学一致 |
|------|---------------------|---------------------|
| 不精排 | 基线 | ✓ |
| cross-encoder reranker（bge-reranker-v2-m3）| +10-30% NDCG@10 | ✗（独立异构小模型） |
| **LLM 重排（用 Navigator backend 读候选 → 输出 top-k + 理由）** | **+15-35% NDCG@10**，上限更高 | ✓ |
| reranker + LLM 重排串联 | +25-40% | 部分（仍有 reranker） |

## 决策

**v1 全部检索路径采用 LLM 重排（LLM-based listwise reranking），不部署 cross-encoder reranker 服务**。

具体形式：

```
召回阶段：BM25 + Tree-walking + Graph traversal → 返回 ~20-50 个候选
   ↓
精排阶段：Navigator backend（Claude Haiku / Qwen-7B）读候选列表 + query，输出 top-k + 排序理由
   ↓
top-k 给 Agent
```

## 影响

| 模块 | 影响 |
|------|------|
| M1 LLM Provider | 不部署 bge-reranker-v2-m3；不部署 TEI 服务；新增 Navigator 角色（同 backend，更便宜模型） |
| M5 检索 | 精排实现是 LLM 调用而非独立模型推理；精排输出额外包含"每条结果为何相关"的解释（天然 grounding） |
| 准确率 | 内核 jargon 上预期优于 bge-reranker（LLM 有内核领域预训练知识，reranker 没有） |
| 成本 | 每次 query 多 1 次 LLM 调用：claude_code (Pro) 消耗 1 message；本地 Qwen-7B 是 0 |
| 延迟 | claude_code Haiku ~1-2s，本地 Qwen-7B ~0.5-1s（vs reranker ~50-200ms）|
| 可解释性 | ★ 大幅强化（reranker 给不出"为什么"，LLM 重排给得出） |
| 架构纯净度 | ★ 整套架构只有一个排序哲学：LLM reasoning |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| 保留 bge-reranker-v2-m3 | 与 PageIndex 哲学不一致；多一个异构组件维护；准确率上限低于 LLM 重排 |
| 不做精排，直接按召回顺序取 top-k | 内核符号同名/近名度高（如 `alloc_pages` 系列），BM25 顺序质量风险大；用户对准确率要求高 |
| reranker + LLM 重排串联（双层精排）| 复杂度对 v1.0 过高；v1.2 评测后如发现单层 LLM 重排不稳定再考虑 |

## v1.1+ 升级路径

如果 v1.2 评测发现 LLM 重排在某些细分场景准确率不稳定（如长邮件列表），升级方案是**叠加更强 Chat 模型做第二层精排**（Sonnet → Opus），而不是回去用 cross-encoder。架构始终保持"单一 LLM reasoning 排序范式"。

## 参考

- RankGPT (Sun et al., EMNLP 2023)：LLM listwise reranking 优于 cross-encoder
- RankVicuna (Pradeep et al., 2023)：小 LLM 重排即可逼近大模型效果
- Vectify AI PageIndex 官方实现：无 reranker 组件

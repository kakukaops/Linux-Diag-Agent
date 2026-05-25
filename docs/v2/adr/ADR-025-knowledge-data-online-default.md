# ADR-025 — 知识数据架构：从 offline-first 改为 online-default + offline-capable mode

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-21 |
| 决策者 | 用户 + Architect |
| 关联模块 | M3 ingestion · M5 retrieval · M4 graph · configs |
| 相关 ADR | **取代** [ADR-010](../../v1/adr/ADR-010-lkml-2y-bootstrap.md)（LKML 2y bootstrap）· 不影响 [ADR-001](../../v1/adr/ADR-001-no-embedding.md)（无 embedding）· 同源哲学 [ADR-021](ADR-021-observability-adapter.md) |
| 设计基础 | 本会话 2026-05-21 讨论；`link_commit_message=0` 实测（26,221 引用 ∩ 11 命中）|

## 上下文

v1 立项把 "offline-first" 作为原则——所有知识数据 bulk 下载进本地 PG。隐含假设是"离线建图 → 诊断更准"。2026-05-21 的实测推翻了这个假设：

| 证据 | 含义 |
|------|------|
| 本地 BM25 实测 Recall@10 = 6.7% | 离线没买来准确性 |
| LKML bulk 按 180 天窗口，漏掉 85% 被 commit 引用的 message（22,246/26,221）| 离线（现有方式）反而损害完整性 |
| LKML stable 摄入耗时 34.5h | 构建成本极高 |
| CodeGraph 一直是在线服务 | 没人认为它损害准确性——在线服务模式的现成证明 |

**核心澄清**：准确性来自**操作**（检索 / LLM 理解 / 图遍历），不来自**数据的物理位置**。这些操作需要的是 (a) 搜索服务 (b) 取内容的能力 (c) LLM——三者在数据远程时同样满足。LLM 理解永远发生在 agent 侧，吃数据源返回的结果（CodeGraph 模式即如此：CodeGraph = Zoekt + SCIP，LLM 理解在 agent 侧）。

"offline-first" 实际捆了三件**独立**的事，必须拆开单独决策：

| 捆在一起的 | 本 ADR 怎么处理 | 理由 |
|-----------|----------------|------|
| (a) 知识数据本地 bulk | **改**：在线为主 + 智能缓存 | 无准确性依据 |
| (b) LLM 本地（vLLM）| **不动** | 由数据隐私决定（vmcore/sosreport 不发外部 LLM），与数据在线化正交 |
| (c) 可气隙部署 | **保留为模式** | 客户部署现实（OLK 客户多气隙）|

## 决策

**知识数据架构从「offline-first（强制）」改为「online-default, offline-capable（默认在线，可配置离线）」。**

采用**三层模型**：

```
L1 本地计算结构   link 表 / 线程 DAG 元数据。很小。永远本地。= 图谱骨架
                  （Cross-Graph Linker 的产物，纯 ID 关系）
        ↑
L2 本地智能缓存   引用驱动 + 检索驱动累积的节点内容。
                  我们在其上建 BM25 索引、调排序、跑 LLM 摘要 —— 全部高质量、可控
        ↑ 回填
L3 远程发现服务   lore.kernel.org（public-inbox：Xapian 全文搜索 + 线程重建 + /raw）
                  找缓存外的东西；结果回填 L2
```

**部署模式**（同一套缓存制品，区别只是预热时机）：

- `online`（默认）：L2 靠用收敛（read-through cache），L3 补在线发现
- `offline`（气隙）：出厂预热 L2 打包交付；无 L3。损失"发现从未被引用过的新讨论"那部分召回——可通过出厂打包更大语料快照缓解

## 各数据源的归属

| 数据源 | 归属 | 是否改 |
|--------|------|--------|
| OLK commits | 本地（Linker 全扫；本来就是本地 git）| 不变 |
| CVE / Bug / syzbot | 本地 bulk（量小：15K/3.7K/999 行，不痛）| 不变 |
| **LKML** | **L1 link 表本地 + L2 缓存 + L3 lore search** | **改**（核心）|
| 代码 / 文档 | CodeGraph 远程服务 | 不变（本来就是此模式）|

## LKML 的具体落地

| 能力 | v1（被取代）| v2（本 ADR）|
|------|------------|-------------|
| 跨图遍历（commit→讨论）| bulk 按 list+日期摄入 | 定向按 message-id 抓取（`/all/<msgid>/raw`）→ L2 |
| BM25 检索路由（`lkml` 路）| 本地 PG BM25 over 全量 | L2 缓存本地 BM25 + L3 lore live search，结果合并 |
| 懒加载 | 无 | 诊断引用到未缓存 message → 现取 → 回填 L2 |
| 线程上下文 | bulk | `/t.mbox.gz` 按需取整线程 |
| 线程 LLM 摘要 | 摄入时批量预算 | 按需生成（诊断时 LLM 直接读原文往往比陈旧摘要更好）|

## 影响

### 优势

| 维度 | 影响 |
|------|------|
| 构建时间 | LKML 从"几天 bulk"降到"几小时定向抓取 + 缓存靠用收敛" |
| 完整性 | 定向按 id 抓取覆盖全历史全 list，**比 bulk 按日期窗口更完整**（修复 85% 漏抓）|
| 召回 | L3 lore search 搜全量历史归档 > 本地 180 天部分索引 |
| 封 IP 风险 | 在线请求量（每次诊断几次）远低于 bulk（几万~百万次）|
| 准确性 | 不降——操作（检索/LLM 理解/遍历）不依赖数据本地；L2 上我们仍有完整索引/排序控制权 |

### 代价

| 维度 | 影响 |
|------|------|
| 网络依赖 | online 模式诊断时依赖 lore 可达性；缓存层缓解（二次访问本地）|
| 延迟 | 远程取内容 ~100-1000ms + 限速；首次访问慢，缓存后快 |
| 气隙模式召回 | offline 模式失去 L3 在线发现；小幅损失，可靠快照缓解 |
| 评测可复现 | 在线语料会变；评测模式需 pin 语料快照（eval fixture）|
| 实现复杂度 | 新增懒加载 + 缓存回填 + lore search 客户端 |

## 取代 / 影响的 v1 ADR

- **[ADR-010](../../v1/adr/ADR-010-lkml-2y-bootstrap.md)（LKML 2-year bootstrap）—— 被本 ADR 取代**。不再做 2 年 bulk 摄入。
- [ADR-001](../../v1/adr/ADR-001-no-embedding.md)（无 embedding / BM25 / 知识库优先）—— **不受影响，正交**。L2 缓存上仍是 BM25，无向量。
- [ADR-007](../../v1/adr/ADR-007-data-dir-in-repo.md)（data-dir）—— 部分影响：本地数据语义从"全量语料"变"智能缓存"。

## 备选方案（已否决）

### 备选 A：保持 offline-first bulk 全量摄入

否决：实测 34.5h 构建、85% 漏抓、Recall 6.7%——成本高、完整性差、无准确性收益。

### 备选 B：纯在线，不要本地缓存

否决：每次诊断都远程取，延迟不可接受；气隙完全不可用；评测不可复现。缓存是必须的。

### 备选 C：把 LKML 也做成独立 MCP 服务（像 CodeGraph）

否决：lore.kernel.org 本身已是公共服务，没必要自建一个 LKML 索引服务；直接消费 lore + 自维护 L2 缓存更简单。（若未来 lore 不稳定，可重新评估自建。）

## 验证

- v2.1：定向抓取 + lore search 路由上线后，真实案例 Recall@10 ≥ 70%（[PRD §6.2](../PRD.md)）
- `link_commit_message` 从 0 → ~26,000（定向抓取后）
- 气隙模式：cache-only 跑通 5 例（不依赖 L3）

## 未来回顾

- 跟踪 L2 缓存命中率 / lore 远程调用延迟
- 若 lore.kernel.org 可达性/限速成为问题，重新评估备选 C（自建 LKML 服务）
- offline 模式的召回损失是否在可接受范围（vs online）

---

*参考：[V2_v1_modules_supplements.md §2bis](../V2_v1_modules_supplements.md) · [Architecture.md](../Architecture.md) · [ADR-021](ADR-021-observability-adapter.md)*

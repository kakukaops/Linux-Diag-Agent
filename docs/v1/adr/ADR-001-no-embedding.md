# ADR-001 — 全项目不使用 Embedding 检索

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M1, M2, M4, M5 |
| 相关 ADR | ADR-002（无 reranker） |

## 上下文

PRD 与早期设计原则约定"文档部分采用 PageIndex 思路、不做 embedding"。在 M1 LLM Provider 模块讨论中，进一步澄清：用户的哲学是**整个项目都不使用向量化检索**，不只是文档。这扩展自 Vectify AI 的 PageIndex 思想（vectorless RAG）到所有数据源：

- 文档（kernel-doc / man-pages / LWN）：PageIndex 章节树 + LLM 走查（原计划）
- **源码**：扩展为按子系统 / 文件 / 函数树走查 + BM25 关键词 + Code Graph 遍历
- **LKML**：按 mailing list / 时间窗 / thread 树走查 + BM25 + Message-ID DAG
- **Bug Graph**：按 product / component / 状态过滤 + BM25 + Cross-Graph Linking

## 决策

**在 Linux-Diag-Agent v1 全部数据源上不部署 embedding 服务、不构建向量索引**。

- 不部署 BGE-M3 或任何 embedding 模型
- 不部署 ColBERT-v2 或任何 token-level late interaction 模型
- 不在存储层使用 FAISS / Milvus 等向量数据库
- 检索召回完全依赖：**BM25**（Tantivy 或 PostgreSQL `tsvector`）+ **图遍历**（Code Graph / Cross-Graph Linking 的 Neo4j 关系）+ **LLM 树形走查**（PageIndex 思路）

## 影响

| 模块 | 影响 |
|------|------|
| M1 LLM Provider | 取消 BGE-M3 嵌入服务部署；TEI 框架不部署 |
| M2 存储与 Schema | 取消 FAISS 索引；所有结构化数据存 PostgreSQL + Neo4j |
| M4 知识图谱 | 不为源码 / LKML / Bug 生成嵌入；改为强化 BM25 索引 + 图谱关联 + 子树描述生成 |
| M5 检索 | 路 A 改为 "BM25 + Graph + LLM 走查 + LLM 重排"；路 B PageIndex 不变 |
| 准确率 | 词汇特异性查询（错误码、内核符号）不受影响；纯语义模糊查询需 LLM 改写 query 弥补 |
| 部署 | 简化：少一类服务、少一类索引、少一份模型权重 |
| 离线性 | 强化：连嵌入模型权重都不需要部署 |
| 可解释性 | 强化：所有命中是关键词 / 结构 / LLM 推理路径，全部可追溯 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| 路 A（源码/LKML/Bug）保留 ColBERT-v2 稠密召回 | 与"PageIndex 哲学扩展到所有数据源"的架构纯净度冲突；增加一个异构组件 |
| 仅在源码上保留稀疏 embedding（如 SPLADE） | 仍属于向量化检索；不符合用户意图 |
| 用 embedding 但只在路 A 选择性启用 | 实测增益主要来自精排（reranker / LLM-rerank），召回阶段 BM25 + Graph 已经够 |

## 参考

- Vectify AI PageIndex：https://github.com/VectifyAI/PageIndex
- Sourcegraph 早期纯 BM25 + AST 实践
- BM25 在词汇特异性查询上仍优于 10B+ 参数稠密模型（2025 实证）

# ADR-006 — BM25 索引 = PostgreSQL `tsvector` + GIN（v1.0 起步）

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M2, M5 |

## 上下文

[ADR-001](ADR-001-no-embedding.md) 锁定全项目无 embedding 后，召回阶段主要依赖 BM25 + 图遍历 + LLM 走查。BM25 引擎候选：

| 方案 | 性质 |
|------|------|
| A | PostgreSQL `tsvector` + GIN 索引（PG-native，复用现有 PG） |
| B | Tantivy（Rust 嵌入式 BM25 引擎）+ tantivy-py 绑定 |
| C | OpenSearch / Elasticsearch（独立 JVM 服务） |
| D | Meilisearch |

## 决策

**v1.0 采用方案 A（PG tsvector + GIN）起步；v1.1 评测后再决定是否切 Tantivy**。

具体实现：
- 每个含大文本的表都有一个 `*_tsv TSVECTOR GENERATED ALWAYS AS (...) STORED` 列
- 通过 GIN 索引加速 `@@` 查询
- 使用 `ts_rank_cd` 计算相关性分数
- 与 PG 元数据 join 一气呵成（无跨服务同步）

## 影响

| 维度 | 影响 |
|------|------|
| 部署 | 零新增依赖（PG 本就有）|
| 一致性 | tsvector 与原始数据同步是数据库内一致性，零滞后 |
| 查询便捷 | 可与 metadata join：`WHERE kernel_version = ? AND body_tsv @@ ?` |
| ranking 质量 | 弱于专业 BM25 引擎（`ts_rank_cd` 不是真 BM25，无 k1/b 调优空间） |
| 索引膨胀 | GIN 索引在大文本表上膨胀较快；月度 `VACUUM FULL` 缓解 |
| 中文支持 | 仅启用 `english` parser；内核场景基本英文，足够 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由（v1.0 阶段）|
|------|---------------|
| **B Tantivy** | 真 BM25、性能优秀；但 v1.0 阶段需建立独立索引文件 + 与 PG 同步逻辑，复杂度上升；MVP 阶段过度优化 |
| **C OpenSearch / Elasticsearch** | 太重，独立 JVM 服务，运维负担大；对单机 / 私有部署不友好 |
| **D Meilisearch** | BM25 不是默认；社区版功能受限；与 PG 同步问题同 B |

## 升级路径（v1.1 评估）

v1.1 评测时，如发现 top-5 命中率 < 70%（PRD 验收阈值），切 Tantivy：

1. 在 `<repo>/data/bm25/` 建立 Tantivy 索引
2. Ingestion pipeline 末端加一步同步 Tantivy
3. `retrieval/bm25/` 模块切到 Tantivy adapter
4. 业务接口不变（[ADR-003](ADR-003-openai-chat-schema.md) 检索 API 保持稳定）
5. 删除 PG `*_tsv` 列与 GIN 索引（释放 ~15GB）

预期切换成本：~5 PD（嵌入式 Tantivy）

## 监控指标

- `pg_index_size{index=*_fts}` — 索引大小
- `bm25_query_latency_seconds` — 查询延迟
- `bm25_recall_at_10` — 评测召回率（人工抽样）
- 触发切换：召回 < 70% 或 P95 查询 > 1s

## 参考

- PostgreSQL Full Text Search：https://www.postgresql.org/docs/current/textsearch.html
- Tantivy：https://github.com/quickwit-oss/tantivy
- tantivy-py：https://github.com/quickwit-oss/tantivy-py

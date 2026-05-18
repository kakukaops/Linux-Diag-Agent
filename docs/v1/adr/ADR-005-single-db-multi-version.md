# ADR-005 — 双版本（v6.6 + v5.10）= 单库 + `kernel_version` 列  ⚠️ SUPERSEDED

| 字段 | 值 |
|------|---|
| 状态 | **Superseded by [ADR-009](ADR-009-multi-version-via-codesearch-repos.md)（2026-05-14）** |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M2 |
| 取代原因 | [ADR-008](ADR-008-reuse-codesearch.md) 决定复用 codesearch 作为 Code Graph 提供者后，原计划的"单库 + `kernel_version` 列"假设已不成立 — 内核源码/符号/文档全部在 codesearch 内，由其 `repo` 列做隔离。我方 PG 内剩余的版本相关数据极少，无需统一版本列。 |

## 上下文

PRD 锁定 v1 支持 v6.6 + v5.10 LTS 双内核版本并行。存储层需要选一种隔离方式：

| 方案 | 描述 |
|------|------|
| A | 单库 + `kernel_version` 列 + 复合索引 |
| B | 物理 schema 分库（`v6_6` / `v5_10` 两套表） |
| C | PG Declarative Partitioning by version |

## 决策

**采用方案 A：单库 + `kernel_version` 列**。

具体实现：
- 所有与内核版本相关的表（`kernel_function`, `kernel_macro`, `kernel_struct`, `kernel_file`, `link_function_subsystem`, `link_stackframe_function` 等）都含 `kernel_version TEXT NOT NULL` 列
- 复合索引以 `kernel_version` 为前缀：`(kernel_version, name)`, `(kernel_version, file_path, name, line_start)` 等
- `kernel_commit` 不带 `kernel_version`（commit 是全局唯一），用 `affected_versions TEXT[]` 表达涉及哪些版本
- LKML / Bug / Doc 数据本身与版本无关，不需要版本列
- 跨版本同名函数映射通过查询而非额外表（如：`SELECT * FROM kernel_function WHERE name = $1 GROUP BY kernel_version`）

## 影响

| 维度 | 影响 |
|------|------|
| 查询便捷性 | 跨版本查询无需 `UNION`：`WHERE kernel_version IN ('v6.6', 'v5.10')` |
| 迁移成本 | 后续加新 LTS（如 v6.12）仅是多一个 enum 值，无 schema 改动 |
| 索引效率 | `(kernel_version, name)` 复合索引在版本+名称两条件过滤时性能等同分库 |
| 备份恢复 | 单库 dump / restore；不需要协调多个 schema |
| 隔离性 | 弱于物理分库；但通过 PG row-level security（如需）可加额外约束 |
| 表大小 | 单表 ~1-2M 行属于 PG 舒适区，无性能压力 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **B 物理 schema 分库** | 跨版本查询需 UNION，复杂；迁移脚本翻倍；后续加版本要建新 schema 并复制全部 DDL；备份脚本翻倍；维护复杂度高 |
| **C Declarative Partitioning** | 当前 1-2M 行 / 表远未到分区收益临界点（一般 5000 万行起）；引入额外复杂度；分区键设计错可能反而降性能 |

## 后续路径

- 若 v1.2 后单表行数超 5000 万（应该不会）：评估按 `kernel_version` 做 PARTITION BY LIST
- 若 v2 加入 RHEL/SUSE enterprise kernel 分支：评估专属表（带 distro 标识）vs 同一列扩展

## 参考

- PG Partitioning 官方文档：https://www.postgresql.org/docs/current/ddl-partitioning.html
- 行数选择性的实证（5000 万行临界点）：内部估算 + PG 社区常见经验

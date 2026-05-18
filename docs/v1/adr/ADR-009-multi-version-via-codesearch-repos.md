# ADR-009 — 多版本（v6.6 + v5.10）通过 CodeGraph 的"两个独立 repo"模式隔离

| 字段 | 值 |
|------|---|
| 状态 | Accepted（**有 P0 实施期发现待跨项目协调**，见 §实施期发现）|
| 日期 | 2026-05-14（修订 2026-05-15） |
| 决策者 | 用户 + Architect |
| 关联模块 | M2, M3, M4 |
| Supersedes | [ADR-005](ADR-005-single-db-multi-version.md)（标记为 Superseded） |
| 相关 ADR | [ADR-008](ADR-008-reuse-codesearch.md) |

## 上下文

[ADR-008](ADR-008-reuse-codesearch.md) 决定复用 codesearch 作为 Code Graph + Doc Tree 提供者。代码与文档检索均落入 codesearch 域内。

[ADR-005](ADR-005-single-db-multi-version.md) 原方案"单库 + `kernel_version` 列"是在自建 Code Graph 时设计的，已不再适用 — 因为：

- 内核函数 / 宏 / 结构体 / 调用关系 / 文件大纲都在 codesearch 内（其 schema 由 `repo` 列做隔离）
- 我们 PG 中**剩余**与版本相关的数据，主要是 `kernel_commit.affected_versions` 和（如保留的话）oops stack 解析
- 版本隔离的"主战场"已经转移到 codesearch 那一侧

## 决策

**v6.6 与 v5.10 在 codesearch 内以两个独立 repo 登记**：

| repo 名 | 内容 |
|---------|------|
| `olk-kernel-v6.6` | Linux v6.6 LTS 完整 checkout（Zoekt 全文 + SCIP 符号 + Sphinx JSON 文档）|
| `olk-kernel-v5.10` | Linux v5.10 LTS 完整 checkout（同上）|

在 codesearch 的 `workspace.yaml`（或等价配置）中分别声明：

```yaml
repos:
  - name: olk-kernel-v6.6
    path: <codesearch-data>/repos/olk-kernel-v6.6
    doc_backend: sphinx-json
    doc_build_root: <codesearch-data>/kernel-docs-build/v6.6/json
  - name: olk-kernel-v5.10
    path: <codesearch-data>/repos/olk-kernel-v5.10
    doc_backend: sphinx-json
    doc_build_root: <codesearch-data>/kernel-docs-build/v5.10/json
```

### Linux-Diag-Agent 的查询语义

- 凡涉及代码 / 文档 / 符号的查询，必须指定 `repo` 参数（如 `search_code(query=..., repos=["olk-kernel-v6.6"])`）
- 不允许"未指定 repo"的跨版本无差别检索（除非显式需要双版本对照）
- 用户输入或诊断 agent 推导出的"内核版本" → 直接映射到 CodeGraph repo 名（实际由 codesearch 提供）

### 我们 PG 内残余的版本相关字段

| 表 | 字段 | 说明 |
|----|------|------|
| `kernel_commit` | `affected_versions TEXT[]` | 该 commit 影响哪些 LTS（如 `['v6.6']`, `['v5.10', 'mainline']`），不需 row-per-version |
| `link_commit_bug` | — | 与版本解耦 |
| `lkml_*` / `bug` / `cve` / `syzbot_crash` | — | 全部与版本无关，无版本列 |

**没有任何表需要 `kernel_version` 列做行隔离**。版本信息要么在数组里（affected_versions），要么通过 CodeGraph repo 名隐式传达。

## 影响

| 维度 | 影响 |
|------|------|
| **codesearch ingestion** | 增加一个 repo（v5.10），workspace 配置加一行；docker compose 现有 kernel-docs-builder 可直接复用 |
| **codesearch 索引开销** | 翻倍（v6.6 已索引；v5.10 新增）；估算 +6 GB 磁盘 + ~2 小时初次索引 |
| **诊断 agent 路由逻辑** | 必须先确定"用户报的故障在哪个内核版本"，再选 repo；这本身就是诊断的第一步 |
| **跨版本对照** | 显式："这个 oops 在 v6.6 没问题，在 v5.10 是不是已修？"由 agent 串两次 lookup_symbol 完成 |
| **未来加 v6.12 LTS** | 配置加一行；零代码改动 |
| **PG schema 简化** | 不需要复合索引 `(kernel_version, name)`；不需要在 v5/v6 路径上设计 partition；schema 显著简化 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **保留 ADR-005（单库 + kernel_version 列）** | 在 CodeGraph 接管 Code Graph 后失去意义；多余复杂度 |
| **在 codesearch 内用 `repo + version tag` 模式** | codesearch 当前实现以 `repo` 字符串为唯一键，没有"同 repo 多 version"原生支持；用两个独立 repo 更贴合现状 |
| **物理 schema 分库** | 同 ADR-005 拒绝理由 |

## 迁移路径

- 旧 ADR-005 状态置为 "Superseded by ADR-009"
- M2 storage_schema 中所有"双版本设计"段落更新或删除
- KnowledgeGraph_Overview §3 Code Graph 同步更新

## 后续

未来若要加新内核版本（如 v6.12 LTS、RHEL kernel 分支）：

1. 在 codesearch 内新增 repo（`olk-kernel-v6.12`）
2. 索引 + 验证
3. Linux-Diag-Agent 配置文件追加映射：`"v6.12": "olk-kernel-v6.12"`
4. 零代码改动

## 实施期发现（2026-05-15 Spike）

来自 [Spike_Report.md](../spike/Spike_Report.md) §2.1 实证：通过 `list_repos` MCP 调用查询 codesearch v1.27.0 实际状态：

```
olk-kernel    [Zoekt+SCIP, docs:sphinx-json/ready]
```

**当前 codesearch 只有一个 repo `olk-kernel`（单数）**，没有按版本拆分。本 ADR 设计的 `olk-kernel-v6.6` / `olk-kernel-v5.10` 双 repo 命名约定与实际不符。

### 影响范围

- ADR-008 §3.2 / §"关键风险与监控" 中所有 `olk-kernel-v6.6` 字面值
- KnowledgeGraph_Overview §3 / §6 / §8.4 中所有提及双 repo 的地方
- M2 / M3 / M4 / M5 设计文档中假设双 repo 调用的代码示例
- ProjectPlan §7 v1 实施前置 checklist「OLK kernel v6.6/v5.10 已索引」

### 待协调（P0，M0 启动前必须解决）

| 路径 | 说明 | 工作量 |
|------|------|--------|
| **A. codesearch 团队按双 repo 拆分** | codesearch 内将现有 `olk-kernel` 重命名为 `olk-kernel-v6.6`，并新增 `olk-kernel-v5.10` repo（Zoekt + SCIP + Sphinx JSON 索引）| codesearch ≤ 1 周（含 v5.10 SCIP 兼容性验证）|
| **B. 本项目妥协回单 repo + 版本字段** | 修订 ADR-009：保留单 repo `olk-kernel`，重新引入"版本相关字段"（但仅在 CodeGraph 一侧；我方 PG 仍按 ADR-009 现行设计无 kernel_version 列）| 本项目 ≤ 3 天文档修订 |

**推荐路径 A**：与 ADR-008/009 现行设计完全一致，扩展性更好（未来加 v6.12 / v6.13 LTS 时无代码改动）；v5.10 SCIP 兼容性是必须验证的项，越早越好。

### 临时退路

在路径 A 完成前，spike 与早期 M0 实施可以单 repo 形态进行：把所有调用中的 `repos=["olk-kernel-v6.6"]` 暂用 `repos=["olk-kernel"]`。**Linux-Diag-Agent 配置层应抽象一个 "kernel_version → codesearch repo name" 的映射表**（建议位置：`clients/codegraph/repos.py`），所有上层代码都通过此映射，不硬编码 repo 名。一旦 codesearch 完成双 repo 拆分，仅需修改映射表，无业务代码改动。

## 参考

- codesearch repos 配置模式：`/home/mqq/github/codesearch/delivery/workspace.py`
- codesearch OLK Kernel 接入方案 §11：`/home/mqq/github/codesearch/docs/olk-kernel-indexing.md`
- [ADR-008 codesearch 复用决策](ADR-008-reuse-codesearch.md)

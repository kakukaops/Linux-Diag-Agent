# ADR-008 — Code Graph + Doc Tree = 复用 codesearch 作为独立 MCP 服务

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M2, M3, M4, M5, M6 |
| 相关 ADR | [ADR-001 无 embedding](ADR-001-no-embedding.md) · [ADR-002 无 reranker](ADR-002-no-reranker.md) · ADR-009（版本隔离）|
| Supersedes | 蓝图 §4.1 原计划自建 Code Graph 与 §1.4 自建 Doc Tree 部分 |

## 上下文

v1 原计划在 M4 内自建 Code Graph（Tree-sitter + Clang LSP + 双版本 ETL，~1000 行）与 Doc Tree（PageIndex + Sphinx/man 解析，~700 行）。

用户随后告知存在已有项目 `/home/mqq/github/codesearch`，并要求评估其适配性，同时调研业界（含 codebase-memory-mcp、Sourcegraph 上游）。调研结论：

1. **codesearch 已经实现了我们需要的核心能力**：Zoekt + SCIP + PostgreSQL chunks + Sphinx JSON 文档接入 + 9 个 MCP 工具，三阶段全部 PASS（2026-03-28 / 03-30 / 03-30），44 repos 已索引，**OLK Kernel 6.6 已专项接入**（kernel-doc 抽取 + Sphinx JSON backend）。
2. **codesearch 与我们 ADR 完全一致**：embedding/hybrid 已 drop（与 ADR-001 一致）、graph_edges 已 drop（与"显式证据优先"哲学一致）、PageIndex 思路（与 §1.4 一致）、PG tsvector BM25（与 ADR-006 一致）。
3. **codebase-memory-mcp 三大 blocker**：embedding 编译进 binary、多版本隔离无文档、SQLite 后端与我们 PG 架构碰撞 — 不适用。
4. **Sourcegraph 上游不直接接 MCP**：Web 服务架构、AGPL/企业版门槛 — 我们自有 codesearch 等价品更轻量。

## 决策

**在 v1 中复用 codesearch 作为独立 MCP 服务**，承担 Code Graph + Doc Tree 两类知识源；Linux-Diag-Agent 不重新实现这两块，通过 MCP 协议消费 codesearch 暴露的工具。

### 集成模式：独立 MCP 服务（不 fork、不 submodule）

- codesearch 继续独立维护与演进
- Linux-Diag-Agent 通过 MCP stdio/HTTP 调用 CodeGraph 的工具
- 在 Linux-Diag-Agent 的 MCP 工具清单里，CodeGraph 工具与我们自有工具（mcp-lkml / mcp-bugzilla / mcp-syzbot / mcp-crash / mcp-perf 等）并列

### CodeGraph 提供的 MCP 工具（v1 直接消费）

| 工具 | 角色映射 |
|------|---------|
| `browse_docs` | M5 路 B PageIndex 顶层导航 |
| `browse_doc_sections` | M5 路 B 章节树浏览 |
| `read_doc_section` | M5 路 B 章节内容读取 |
| `search_docs` | M5 路 A 文档 BM25 关键词检索 |
| `search_code` | M5 路 A 代码 Zoekt 检索 |
| `lookup_symbol` | M4 Code Graph 符号导航（SCIP） |
| `search_symbol_docs` | 文档↔代码桥接（doc-comment 搜索）|
| `get_outline` | tree-sitter 文件级大纲 |
| `read_file` | Zoekt 索引按路径读文件 |
| `list_repos` | 仓库发现 |

### 边界划分

| 由 codesearch 拥有 | 由 Linux-Diag-Agent 拥有 |
|------------------|----------------------|
| 内核源码全文检索（Zoekt） | LKML 邮件 ingestion + 检索（M3 + 自有 mcp-lkml）|
| 内核 SCIP 符号定义 / 引用 / 实现 | Bugzilla / syzbot ingestion + 检索（M3 + 自有 mcp-bugzilla / mcp-syzbot）|
| kernel-doc / Sphinx JSON 文档检索 | commit 元数据 + Fixes:/Reported-by: 抽取（M3）|
| man-pages（如 codesearch 接入） | Cross-Graph Linker（commit↔bug↔LKML↔CVE，M4）|
| 文件级 tree-sitter 大纲 | 故障域 SOP 与诊断 agent（M7）|
| repo 元数据 | 评测体系（M8）|
| | 主机侧 MCP 工具（kdump/drgn/perf/journal，M6）|

## 影响

### 工作量重新估算

| 子图 | 原计划自研行数 | 调整后 | 备注 |
|------|----|----|------|
| Code Graph | ~1000 | **0** | CodeGraph 接管 |
| Discussion Graph (LKML) | ~1200 | ~1200 | 不变 |
| Bug Graph | ~1300 | ~1300 | 不变 |
| Doc Tree | ~700 | **0** | CodeGraph 接管 |
| Cross-Graph Linker | ~1200 | ~1000 | 略减（不再需要在 PG ↔ Neo4j 同步函数节点） |
| CodeGraph 集成胶水（新增）| 0 | ~300 | MCP client + repo 注册 + 健康检查 |
| **总计** | **~5400** | **~3800** | **节省约 30%** |

### 文档影响

- ADR-005（单库 + kernel_version 列）→ 被 [ADR-009](ADR-009-multi-version-via-codesearch-repos.md) supersede
- ADR-006（PG tsvector BM25）→ 仍适用，但范围缩小到我们自有的 LKML/Bug 数据；代码与文档检索由 codesearch 内部完成
- ADR-007（数据目录）→ 缩小范围：仅放 LKML mbox / Bugzilla / syzbot；内核源码与文档 checkout 在 codesearch 那边
- M2 storage schema → 大改：删除 kernel_function/macro/struct/field/file/doc_node/doc_content 等所有 codesearch 已覆盖的表
- KnowledgeGraph_Overview → §3 Code Graph 与 §6 Doc Tree 章节重写为"复用 codesearch"
- M4 模块（待写）→ 范围缩小到 Cross-Graph Linker + commit metadata + 子系统路由
- M5 模块（待写）→ 范围缩小到"在 CodeGraph MCP 之上做查询编排 + LLM 重排"，不重新实现 BM25/SCIP

### 部署影响

- 部署拓扑增加一个独立 service：codesearch（docker compose 已有定义）
- Linux-Diag-Agent 启动时检测 CodeGraph 服务健康状态（通过 `list_repos` ping）
- 失败模式：CodeGraph 服务不可用时，agent 拒绝处理需要源码/文档检索的查询，明确报错

### 演进影响

- 如果 codesearch 后续添加 commit history 工具、MAINTAINERS 解析、调用链遍历等能力，可平移到我们项目，进一步削减 Cross-Graph Linker 的工作量
- 如果 v2 切换到 Sourcegraph 企业版或别的代码图谱后端，只需写新的 MCP adapter，diag-agent 业务代码不变（同 ADR-003 LLM provider 抽象的思路）

## 实施约束（2026-05-15 Spike 反馈）

来自 [Spike_Report.md](../spike/Spike_Report.md) §3 案例 B 实证：v6.1.74-syzkaller 内核生成的 stack trace 行号（`sch_api.c:792` / `dev.c:5282`）与 v6.6 LTS 同函数实际行号（`sch_api.c:774` / `dev.c:5279`）存在 **~20 行漂移**。

### F6：Stack frame → function 解析禁止依赖行号匹配

| 约束 | 内容 |
|------|------|
| **必须** | M4 stack frame → function 解析必须使用**函数名**作为主键，通过 CodeGraph `lookup_symbol(action='definition')` 落地 |
| **可以** | 把 `frame_offset`（如 `oom_kill_process+0x84`）保留为辅助信息，用于函数内部位置定位（运行时 disassembly 比对，非必需）|
| **禁止** | 任何"用 stack trace 中的 `file:line` 直接匹配 CodeGraph SCIP 行号"的实现 — 跨小版本行号一定漂移 |

### 后果

- ADR-008 §3.2 表中 `lookup_symbol(symbol='oom_kill_process', action='definition', repo='olk-kernel-v6.6')` 是正确用法；任何 `lookup_symbol(file='mm/oom_kill.c', line=996, ...)` 形式的接口**不应被引入**
- M4 `link_stackframe_function` 已在 [ADR-008 §决策] 中决定"运行时不持久化"，本约束进一步限定运行时调用方式
- spike 已验证函数名匹配的可行性：3 案例共 6 个函数名（`qdisc_tree_reduce_backlog` / `net_tx_action` / `oom_badness` / `__ext4_fill_super` 等）全部被 CodeGraph SCIP 精确命中

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **完全自建（原计划）** | 重复 codesearch 已经验证过的工作；OLK Kernel 接入会浪费 ~1700 行自研 + 数周时间 |
| **codebase-memory-mcp** | 多版本隔离没文档；SQLite 与 PG 架构碰撞；C 调用图仅 "Good" 档；宏处理不明；单 vendor 风险 |
| **直接接 Sourcegraph 上游** | Web 服务非 MCP，需二次包装；AGPL/付费门槛；我们已有 codesearch 等价品 |
| **codesearch fork** | 创造维护负担；codesearch 已成熟，没必要 |
| **codesearch submodule** | 升级冲突；强耦合；独立服务更清晰 |

## 关键风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| codesearch 演进破坏 MCP 接口 | 启动时调用 list_repos + smoke test | 锁定 codesearch 版本（git tag），升级前先在测试环境验证 |
| OLK kernel 索引未完成或失败 | `list_repos` 检查 `olk-kernel-v6.6` / `olk-kernel-v5.10` 状态 | ingestion 文档化 + 重跑脚本 |
| codesearch 与 diag-agent 协作出现性能瓶颈 | 每次 MCP 调用记录 latency | 加缓存层；评估升级 codesearch 实例规格 |
| codesearch 无 commit-level 工具，需要时由 diag-agent 自查 git | 我们仍维护 `kernel_commit` 表并自跑 git log | 与 codesearch 共享 git checkout 路径，或独立 clone（v1 二选一）|
| 两个 repo 团队节奏不一致 | 月度 sync | 列入 v1 Project Plan 的固定 stakeholder check-in |

## 参考

- codesearch 当前主仓库：`/home/mqq/github/codesearch`
- codesearch 技术路线：`/home/mqq/github/codesearch/docs/technical-route.md`
- codesearch OLK Kernel 文档接入方案：`/home/mqq/github/codesearch/docs/olk-kernel-indexing.md`
- codesearch v2 设计文档：`/home/mqq/github/codesearch/docs/design-v2.md`
- Sourcegraph 关于代码图谱思路：https://sourcegraph.com/blog/how-cody-understands-your-codebase
- Sourcegraph SCIP 设计：https://sourcegraph.com/blog/announcing-scip
- scip-clang 项目：https://github.com/sourcegraph/scip-clang
- Zoekt 项目：https://github.com/sourcegraph/zoekt

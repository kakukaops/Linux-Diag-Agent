# ADR-017 — Linux-Diag-Agent 内对 codesearch 服务统一称 "CodeGraph"

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-15 |
| 决策者 | 用户 + Architect |
| 关联模块 | M2, M4, M5, M6（所有调用 codesearch 的模块） |
| 相关 ADR | [ADR-008](ADR-008-reuse-codesearch.md) · [ADR-009](ADR-009-multi-version-via-codesearch-repos.md) · [ADR-012](ADR-012-codesearch-http-transport.md) · [ADR-013](ADR-013-m5-retrieval-strategy.md) |

## 上下文

`/home/mqq/github/codesearch` 是一个独立项目（Zoekt + SCIP + PostgreSQL + Sphinx JSON 文档接入的代码 / 文档检索服务），由 [ADR-008](ADR-008-reuse-codesearch.md) 决定在 Linux-Diag-Agent v1 中以 MCP 服务形式复用。

在我方 `KnowledgeGraph_Overview` 的子图叙事里，源码图谱被称为 **Code Graph**（与 `Discussion Graph` / `Bug Graph` / `Doc Tree` 并列）。但前期设计文档中大量出现 `调用 codesearch MCP` / `CodeSearchClient` / `clients/codesearch/` 这类字眼，与子图叙事不一致，也让 codesearch 项目名在我方业务层"喧宾夺主"。

## 决策

**在 Linux-Diag-Agent 项目内部，统一把"复用 codesearch 服务"这一角色称为 `CodeGraph`。** codesearch 项目本身保留原名（跨项目协作时不改名）。

### 三层命名

| 层级 | 名字 | 何时用 |
|------|------|--------|
| **L1 — 外部项目** | `codesearch` | 指 `/home/mqq/github/codesearch` 项目本身、其代码仓、其团队、其 ADR、其 ReadMe |
| **L2 — 本项目角色 / 服务门面** | `CodeGraph` | 指本项目调用的"代码/文档检索服务"角色（与 Discussion Graph / Bug Graph / Doc Tree 并列叙事）|
| **L3 — Wire protocol** | `search_code` / `lookup_symbol` / `browse_docs` / ...（不变） | MCP tool 名，跨语言客户端共享，由 codesearch 项目定义 |

### 命名规范

| Context | 应当用 |
|---------|--------|
| Python 模块目录 | `clients/codegraph/` |
| Python class | `CodeGraphClient` / `CodeGraphHttpClient` |
| Python 异常 | `CodeGraphClientError` / `CodeGraphTimeout` |
| 健康检查 | `CodeGraphHealthCheck` / `check_codegraph_health()` |
| 环境变量 | `CODEGRAPH_URL` / `CODEGRAPH_TIMEOUT` |
| 配置键 | `codegraph.base_url` / `codegraph.timeout_seconds` |
| 监控指标前缀 | `codegraph_*`（如 `codegraph_call_duration_seconds`）|
| 文档中提到角色 | "**CodeGraph** 服务" / "**CodeGraph** MCP" / "**CodeGraph** 路检索" |
| 文档中提到外部项目 | "**codesearch** 项目" / "由 codesearch 实现" / "codesearch 团队" |

### 标准描述句式

文档中首次提到时，用以下任一句式点明双层关系：

- "**CodeGraph 服务**（由 codesearch 项目实现）"
- "调用 **CodeGraph MCP**（底层是 codesearch v1.27.0+）"
- "本项目 `clients/codegraph/` 客户端封装对 codesearch HTTP MCP server 的调用"

### MCP tool 名不改

`search_code` / `lookup_symbol` / `browse_docs` / `read_doc_section` / `get_outline` / `list_repos` / `search_docs` / `read_file` / `browse_repo` / `search_symbol_docs` / `browse_doc_sections` 这 11 个 tool 名是 codesearch 项目暴露的 wire protocol，**保持原样**，不在 CodeGraphClient 中重命名。

## 影响

### 改名清单（一次性，M0 启动前完成）

| 范围 | 文件数 | 估计修改 | 处理 |
|------|--------|---------|------|
| 14 份 v1 docs（PRD / Architecture / ProjectPlan / KG Overview / 8 模块 / 5 ADR） | 14 | ~350 处 | 批量替换（按规约判断保留 vs 改）|
| ADR 文件名 `ADR-008-reuse-codesearch.md` | 1 | 0 | **保留**（决策标签历史价值）|
| `Spike_Report.md` | 1 | 0 | **保留**（spike 实证现场，保留原始命名）|
| 代码（M0 尚未开工）| 0 | 0 | 直接按本 ADR 规范写 |

### 跨项目协调

- 与 codesearch 团队对话时**继续用 codesearch**（不要求他们改名，不要求他们承认 CodeGraph 这个角色名）
- ADR-009 P0 协调项中向 codesearch 团队的请求仍是 "拆 olk-kernel 为 olk-kernel-v6.6 / olk-kernel-v5.10"，与本 ADR 无关

### 调试 / 故障排查

调用链可能出现"**CodeGraphClient** 通过 HTTP MCP 调到 **codesearch** server 上的 `search_code` 工具"这种**三层混用**的描述。M4 客户端代码 module docstring 顶部必须用一段注释明示三层关系（参见 [M4 §4.2](../modules/M4_cross_graph_linker.md)）。

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **保持 codesearch 名**（不改） | 与 KG Overview 子图叙事不一致；外部项目名在业务层喧宾夺主；M0 后改名成本翻倍 |
| **完全替换 codesearch 字眼**（连外部项目名也叫 CodeGraph） | 跨项目协作时困惑；codesearch 项目自身的 ReadMe/ADR/repo 名不归我方管 |
| **用 KernelCode 或 LinuxCode 等领域名** | 失去通用性；后续若 CodeGraph 角色扩展到非内核领域，命名要再改 |
| **沿用 Code Graph 加空格** | Python identifier 不能含空格；连写更紧凑 |

## 实施顺序

1. ★ 本 ADR-017 落地（本文）
2. 14 份 docs 批量改名（M4 / KG Overview / 4 ADR / 8 其他模块 / PRD / Architecture / ProjectPlan）
3. M0 第 1 周写 `clients/codegraph/` 时按本 ADR 规约
4. 后续所有新文档默认遵循

## 参考

- [ADR-008 复用 codesearch 决策](ADR-008-reuse-codesearch.md)
- [KnowledgeGraph_Overview](../KnowledgeGraph_Overview.md) — Code Graph 子图叙事来源
- codesearch 项目仓库：`/home/mqq/github/codesearch`

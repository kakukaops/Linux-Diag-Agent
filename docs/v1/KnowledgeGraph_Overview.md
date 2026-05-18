# 知识图谱构建总览 — Knowledge Graph Overview

| 字段 | 值 |
|------|---|
| 文档类型 | Cross-cutting Reference |
| 范围 | v1 全部知识源（源码 / 邮件 / Bug / 文档）的图谱化思路 |
| 关联模块 | M2（存储） · M3（Ingestion） · M4（Cross-Graph + commit ETL） · M5（检索编排） · M6（MCP） |
| 关联 ADR | [ADR-001 无 embedding](adr/ADR-001-no-embedding.md) · [ADR-008 复用 codesearch](adr/ADR-008-reuse-codesearch.md) · [ADR-009 多版本通过 codesearch repos](adr/ADR-009-multi-version-via-codesearch-repos.md) |
| 最后更新 | 2026-05-15（v2.1：吸收 [Spike_Report](spike/Spike_Report.md) F2-F7 反馈；2026-05-14 v2 因 [ADR-008](adr/ADR-008-reuse-codesearch.md) 复用 codesearch 重写） |

---

## 1. 设计哲学

Linux-Diag-Agent 的核心差异化壁垒是**把内核生态的公开知识（源码 + 邮件 + Bug + 文档）构建成一张可推理的多层关联图**。整体由 **4 张子图 + 1 个 Cross-Graph Linker** 组成。

**关键架构决策**（[ADR-008](adr/ADR-008-reuse-codesearch.md)）：**Code Graph 与 Doc Tree 两块复用已有项目 [`codesearch`](https://github.com/mqq/codesearch)**（独立 MCP 服务，已在 OLK Kernel 6.6 跑通）；Linux-Diag-Agent 自建 LKML / Bug Graph + Cross-Graph Linker。

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────┐
│ Code Graph      │  │ Discussion      │  │ Bug Graph       │  │ Doc Tree       │
│ (源码图谱)       │  │ Graph (邮件)    │  │ (缺陷/崩溃)      │  │ (文档章节树)    │
│                 │  │                 │  │                 │  │                │
│ ★ 复用          │  │ Message-ID DAG  │  │ bug + CVE       │  │ ★ 复用         │
│ CodeGraph       │  │ + 线程摘要      │  │ + syzbot crash   │  │ CodeGraph      │
│ (Zoekt + SCIP)  │  │ + patch 系列    │  │ + stack 签名    │  │ (PageIndex     │
│                 │  │ + Reviewed-by  │  │ + 修复 commit    │  │  / Sphinx JSON)│
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘  └────────┬───────┘
         │                    │                    │                    │
         └────────────────────┴────────┬───────────┴────────────────────┘
                                       ▼
                          ┌────────────────────────┐
                          │ Cross-Graph Linker     │
                          │ Fixes:/Reported-by:/   │
                          │ Message-ID/CVE/        │
                          │ commit↔bug↔message     │
                          │ + 运行时 CodeGraph    │
                          │   lookup_symbol 调用   │
                          └────────────────────────┘
                                       ▼
              我方 PG + Neo4j (LKML/Bug/commit + 关系)
                    +
              codesearch PG (chunks + scip_symbols)
```

## 2. 共同原则

| 原则 | 适用 |
|------|------|
| 全项目**无 embedding**（[ADR-001](adr/ADR-001-no-embedding.md)）| 召回靠 BM25 + 图遍历 + LLM 树形走查 |
| **结构化优先**：能用自然键、外键、tag、正则的，就不用 LLM 推断 | 减少幻觉，所有关联可审计 |
| **Code Graph + Doc Tree 通过 MCP 复用 codesearch**（[ADR-008](adr/ADR-008-reuse-codesearch.md)）| 避免重复造轮子；codesearch 已 production-validated |
| 双版本（v6.6 + v5.10）= codesearch 内两个独立 repo（[ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md)）| 我方 PG 内无 `kernel_version` 列 |
| **我方 PG 存详情、Neo4j 只存图谱拓扑** | 各取所长 |
| 所有 ingest 任务**增量 + 断点续传** | 每周同步成本可控 |
| 所有链接都带 `confidence` 与 `source` 字段 | 调试与对账可追溯 |

---

## 3. 子图一：Code Graph — 复用 codesearch ★

### 3.1 构建思路

**完全由 CodeGraph 承担**。Linux-Diag-Agent 仅作为 MCP 消费者。

codesearch 已实现：

| 层 | 在 codesearch 内的实现 |
|----|-----------------------|
| 文件全文 + symbol 文本搜索 | **Zoekt** trigram 索引（v6.6 + v5.10 两个 repo）|
| 精确符号定义 / 引用 / 实现 | **SCIP**（scip-clang 基于 LLVM 21）|
| 文件级大纲 | tree-sitter（按需解析）|
| 跨 repo 符号查询 | 在 codesearch 的 PG `scip_symbols` 表 |
| commit 元数据（基础部分）| CodeGraph 不深度处理 commit metadata，留给我方 |

### 3.2 通过 CodeGraph MCP 工具获取代码图谱信息

| 调用 | 返回 |
|------|------|
| `search_code(query, repos=['olk-kernel-v6.6'], mode='symbol')` | Zoekt 命中的文件:行 列表 |
| `lookup_symbol(symbol='oom_kill_process', action='definition', repo='olk-kernel-v6.6')` | SCIP 精确定义（file:line + 签名 + doc-comment） |
| `lookup_symbol(symbol='oom_kill_process', action='references')` | 所有调用点 |
| `get_outline(repo='olk-kernel-v6.6', path='mm/oom_kill.c')` | 文件级函数/结构体大纲 |
| `read_file(repo='olk-kernel-v6.6', path='mm/oom_kill.c', line_start=100, line_end=200)` | 源码 |

### 3.3 我方在 Code Graph 上的工作（保留少量）

| 工作 | 实现位置 | 估计行数 |
|------|---------|---------|
| CodeGraph MCP 客户端封装 | `clients/codegraph/client.py` | ~150 |
| 内核版本 → CodeGraph repo 名映射 | `clients/codegraph/repos.py` | ~50 |
| CodeGraph 健康检查 + 启动时校验 | `clients/codegraph/healthcheck.py` | ~100 |
| commit 元数据 ETL（git log + Fixes: 抽取）| `ingest/kernel_commit/` | ~300 |
| 子系统推断（从 file_path 推 subsystem）| `lib/subsystem_inference.py` | ~100 |
| **小计** | | **~700 行** |

### 3.4 与原计划对比

| 项 | 原计划 | 复用 codesearch 后 |
|----|--------|-----------|
| 自研行数 | ~1000 | ~700 |
| 双版本隔离实现 | PG kernel_version 列 | CodeGraph 两 repo |
| 索引底座 | Bootlin Elixir + Tree-sitter + libclang | codesearch 已含全部 |
| 启动时间 | 自建 v6.6 索引 ~15h | 接入 CodeGraph ~10 分钟 |

### 3.5 关键风险

| 风险 | 应对 |
|------|------|
| codesearch repo 名约定可能变 | 启动时通过 `list_repos` 检查 |
| SCIP 索引未完成或失败 | ingestion 文档化；失败时降级（仅 Zoekt 文本搜索） |
| 跨版本符号差异处理（v6.6 没有 v5.10 的某函数） | agent 业务层判断；CodeGraph 各 repo 独立返回 |

---

## 4. 子图二：Discussion Graph — LKML 邮件讨论

### 4.1 构建思路

**我方自建**，与原计划一致。lore.kernel.org 5 年邮件（2020-12 至今，估约 7M 邮件）重建为：

| 层 | 内容 | PG 表 |
|----|------|-------|
| 单封邮件 | 元数据 + body + tsvector 全文索引 | `lkml_message` |
| 线程 DAG | Message-ID + In-Reply-To 反向树 | Neo4j `(:Message)-[:IN_REPLY_TO]->()` |
| Patch 系列 | `[PATCH vN]` + Fixes: tag + landed_commit | `lkml_patch` |
| 评审信号 | `Reviewed-by:` / `Tested-by:` / `Acked-by:` / `NACK` | `lkml_review` |
| 长线程摘要 | LLM "设计动机 + 争议点 + 结论" | `lkml_thread.summary` |

### 4.2 处理流程

```
lore.kernel.org REST API + mbox 下载（按 list + 月份切分）
   ↓
mbox 解析（mailbox + email Python stdlib）→ 单封邮件
   ↓
入 PG lkml_message + tsvector
   ↓
基于 In-Reply-To / References 构建线程 DAG → PG lkml_thread + Neo4j 关系
   ↓
[PATCH vN] / Fixes: / Reported-by: trailer 抽取 → PG lkml_patch
   ↓
Reviewed-by/Tested-by/Acked-by 抽取 → PG lkml_review
   ↓
对长线程（> 30 封）调用 M1 Navigator LLM → 三段式摘要
```

### 4.3 开源依赖

| 组件 | License | 用途 |
|------|---------|------|
| **public-inbox**（lei CLI） | AGPLv3 | 官方 mailing list 工具链 |
| **`mailbox` + `email`**（Python stdlib） | PSF | mbox 解析、RFC 5322 邮件解析 |
| **`requests` / `httpx`** | Apache 2.0 / BSD | lore REST API 调用 |
| **Patchwork API**（kworkflow 复用） | GPLv2 | patch series metadata |

### 4.4 自研工作量

约 **1200 行**，与原计划相同。

---

## 5. 子图三：Bug Graph — 缺陷与崩溃

### 5.1 构建思路

**我方自建**，与原计划一致。统一多源 bug 数据 + 关联 CVE + syzbot 特殊字段 + stack 签名相似匹配。

数据源（v1，[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)）：
- **bugzilla.kernel.org**（v1 仅此源）
- syzbot.kernel.org
- Zenodo `linux-kernel-bugs` 数据集（120 万 commits + 9 万 bug-fix 对，一次性导入）
- NVD JSON Feed（CVE 元数据）— 通过 commit hash 关联补强 CVE-kernel 链路

⚠️ Red Hat BZ / Debian BTS / Launchpad 延后到 v1.1+ 评估（见 [ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)）。

### 5.2 关键变化（vs 原计划）

| 项 | 原计划 | 现在 |
|----|--------|------|
| stack frame → function 解析 | 自建 `link_stackframe_function` 表 | **运行时调用 CodeGraph `lookup_symbol`，不持久化** |
| 函数与子系统映射 | 自建 `link_function_subsystem` 表（MAINTAINERS 解析）| **v1.0 用 CodeGraph file_path 推断；MAINTAINERS 解析延后** |

### 5.3 开源依赖

| 组件 | License | 用途 |
|------|---------|------|
| **`python-bugzilla`** | GPLv2 | Bugzilla REST API |
| **`requests` / `httpx`** | Apache 2.0 | syzbot HTML + NVD API |
| **NVD JSON Feed** | 公共领域 | CVE 数据 |
| **Zenodo 数据集** | 数据集 license | 一次性下载 |

### 5.4 自研工作量

约 **1300 行**，与原计划相同。

---

## 6. 子图四：Doc Tree — 复用 codesearch ★

### 6.1 构建思路

**完全由 CodeGraph 承担**。Linux-Diag-Agent 仅作为 MCP 消费者。

codesearch 已实现：
- kernel-doc 通过 **Sphinx JSON backend** 接入（不再走 `.rst → Markdown → chunks`）
- 三层导航：
  1. `toctree` 站点级目录树（用于 `browse_docs`）
  2. 页面级 `toc` 章节树（用于 `browse_doc_sections`）
  3. anchor / kernel-doc API 块级定位（用于 `read_doc_section`）
- 独立 `kernel-docs-builder` Docker 容器构建 Sphinx 产物

### 6.2 通过 CodeGraph MCP 工具获取文档信息

| 调用 | 返回 |
|------|------|
| `browse_docs(repo='olk-kernel-v6.6')` | 顶层文档目录树 |
| `browse_doc_sections(repo='olk-kernel-v6.6', source_path='admin-guide/mm/concepts.rst')` | 单文档章节树 |
| `read_doc_section(repo='olk-kernel-v6.6', source_path='admin-guide/mm/concepts.rst', section='Fragmentation')` | 节文 |
| `search_docs(query='OOM killer', repo='olk-kernel-v6.6')` | BM25 文档命中 |
| `search_symbol_docs(query='oom_kill_process')` | 通过 doc-comment 桥接到 SCIP 符号 |

### 6.3 我方在 Doc Tree 上的工作

**几乎为零**。只需在诊断 agent 中正确路由查询到 CodeGraph 的文档工具。

| 工作 | 估计行数 |
|------|---------|
| 路由决策（什么时候查文档 vs 查源码 vs 查 LKML）| ~100（在 M5/M7 内）|
| **小计** | **~100 行**（含在其他模块行数内）|

### 6.4 与原计划对比

| 项 | 原计划 | 复用 codesearch 后 |
|----|--------|-----------|
| 自研行数 | ~700 | ~0（含在 M5 路由内）|
| PageIndex 实现 | 自建 tree builder + describer + navigator | codesearch 已实现 |
| Sphinx 接入 | 自建 | codesearch 独立 kernel-docs-builder 已有 |

---

## 7. Cross-Graph Linker — 核心壁垒（不变）

### 7.1 思路

把四个子图焊成一张可多跳推理的网。**所有关联都有明确证据**，无相似度匹配：

| 关系 | 链接方式 | 证据来源 | 实现位置 |
|------|---------|---------|---------|
| `commit ↔ bug` | `Fixes:` tag、`Closes: bug#N`、`Bugzilla:` URL、Zenodo 数据集 | 高置信 | **我方 PG `link_commit_bug`** |
| `commit ↔ LKML thread` | Message-ID 引用、`Link:` trailer、patch landed_commit 反查 | 高置信 | **我方 PG `link_commit_message`** |
| `bug ↔ CVE` | NVD 引用 + Bugzilla "External Bug References" | 高置信 | **我方 PG `bug.cve_ids` + `cve` 表** |
| `commit ↔ function` | git diff 提及的文件 → CodeGraph `lookup_symbol` 找符号 | 中置信 | **运行时**（不持久化） |
| `stack frame ↔ function` | 函数名 + offset → CodeGraph `lookup_symbol` | 中置信 | **运行时**（不持久化） |
| `function ↔ subsystem` | file_path 路径推断（如 `mm/oom_kill.c` → `mm`）| 高置信 | **运行时** + commit/bug 列 |

**关键变化**：原计划自建 `link_function_subsystem` 和 `link_stackframe_function` 表，现在改为**运行时通过 CodeGraph 解析 + file_path 推断**，不持久化。

### 7.2 处理流程

```
所有子图 ingestion 完成后：
   ↓
扫描 kernel_commit.body → 抽 Fixes/Reported-by/Closes/Link trailer
   ↓
扫描 lkml_message.body → 抽 Bugzilla URL / syzbot 引用
   ↓
扫描 bug.description → 抽 commit hash / CVE / Message-ID
   ↓
归并所有候选边 → 去重 + confidence 评估 → 写 PG link_* 表
   ↓
同步到 Neo4j 关系（双写一致性）
   ↓
（可选）周度对账：PG link 表行数 vs Neo4j 关系数
```

### 7.3 开源依赖

| 组件 | License | 用途 |
|------|---------|------|
| **正则 / glob** | stdlib | 模式匹配 |
| **`addr2line` / `faddr2line`** | GPL | 如需地址精确解析 |

### 7.4 自研工作量

| 工作项 | 行数估计 |
|--------|---------|
| Trailer 全集抽取（Fixes/Reported-by/Closes/Link/Cc: stable） | ~200 |
| patch landed_commit 反查算法 | ~250 |
| ~~MAINTAINERS 路径模式解释器~~ → 延后到 v1.1 | （延后） |
| NVD ↔ Bugzilla CVE 桥接 | ~150 |
| PG `link_*` ↔ Neo4j 关系双写一致性 | ~300 |
| 周度对账脚本 | ~100 |
| **小计** | **~1000 行**（vs 原计划 1200，因延后 MAINTAINERS）|

### 7.5 为什么这是真正的壁垒

- 单纯的 codesearch / PageIndex / BM25 都是通用技术
- Cross-Graph Linker 需要**内核领域知识**：
  - `Fixes:` tag 的非标准写法
  - patch 与 commit hash 的关系（一个 patch 可能多次 backport）
  - syzbot 的 ID 编号规则
  - NVD ↔ Bugzilla 跨源 CVE 引用模式
- 这部分做对了，整个 agent 才能像内核工程师那样"从 oops 一直追到原始讨论"
- **复用 codesearch 不削弱这个壁垒**，反而把"代码符号"作为标准输入接口，让 Linker 更专注做关联

### 7.6 Spike 反馈（2026-05-15）

[Spike_Report](spike/Spike_Report.md) §3 用 3 个真实案例（CVE-2024-35892 / mm OOM regression / CVE-2024-40998）人工走 KG 端到端链路。**3/3 走通**，但揭示 4 个 v1.0 设计假设需补强：

| ID | 发现 | 落地章节 |
|----|------|---------|
| F3 | Fixes 链是**嵌套多层**的（实证三层：fix → introduce-1 → introduce-2）| [M4 §3.7](modules/M4_cross_graph_linker.md#37-多层嵌套-fixes-链支持f3) |
| F4 | `Fixes:` trailer 用 short SHA（7-12 位），需 `git rev-parse / PG LIKE / GitHub API` 三级兜底解析为 full SHA | [M4 §3.1](modules/M4_cross_graph_linker.md) |
| F5 | **部分 fix commit 没有 `Fixes:` trailer**（并发竞态 / 设计性 bug），需"反向关联"路径：stack-trace 函数名 + bug.stack_frames 集合包含 | [M4 §3.8](modules/M4_cross_graph_linker.md#38-无-fixes-trailer-修复-commit-间接关联f5) |
| F6 | Stack frame 行号跨小版本漂移 ~20 行（v6.1 vs v6.6）；M4 stack-frame 解析**禁止依赖行号**，必须用函数名 + offset | [ADR-008 实施约束](adr/ADR-008-reuse-codesearch.md#实施约束2026-05-15-spike-反馈) |

另有 3 项跨章节反馈：
- **F2**（codesearch 实际 repo 名 `olk-kernel` 单数 vs ADR-009 设计 `olk-kernel-v6.6`）→ 见 [ADR-009 实施期发现](adr/ADR-009-multi-version-via-codesearch-repos.md#实施期发现2026-05-15-spike)，P0 待协调
- **F7**（M5 `search_code` 默认 `mode='literal'`）→ 见 [ADR-013 实施约束](adr/ADR-013-m5-retrieval-strategy.md#实施约束2026-05-15-spike-反馈)
- **F1**（kernel.org 全域 Anubis 反爬虫）→ 印证 [ADR-010](adr/ADR-010-lkml-2y-bootstrap.md) public-inbox 路径选择正确，无需调整

### 7.7 commit 来源：OLK 双仓库（[ADR-018](adr/ADR-018-commit-source-olk-kernel.md)）

诊断目标是 OLK（openEuler）内核，故 commit 图谱来源**不是 kernel.org 官方仓**，而是：

- **主仓库 = OLK 内核 git**（`OLK-6.6` / `OLK-5.10` 分支），与 CodeGraph 索引 `olk-kernel` **同源** —— 保证代码图谱与 commit 图谱一致
- **辅仓库 = kernel.org mainline** —— 上游知识源，让 OLK backport commit 链回 LKML / bug / CVE 生态

OLK commit 分两类，桥接方式不同：

| 类型 | 桥接方式 | 能否链到 kernel.org 生态 |
|------|---------|------------------------|
| backport（`mainline`/`stable inclusion`）| commit message 内嵌 `commit <mainline-sha>` 锚点 → mainline commit | ✓ |
| openEuler 原生（`openEuler`/`hulk inclusion`）| 无锚点，标「无上游讨论」+ Gitee 指针 | ✗ |

v1.0 只 ingest kernel.org 讨论 / bug 生态；openEuler 自有生态（kernel@openeuler.org / Gitee issues）留 v1.1+。诊断报告对 commit 证据分级（mainline-backed 高 / openEuler-原生 中）。详见 [M4 §3.9](modules/M4_cross_graph_linker.md)。

---

## 8. 全景汇总

### 8.1 开源 vs 自研（修订）

| 子图 | 主要开源依赖 / 复用 | 自研 Python 行数 | 关键性 |
|------|------|---------------|------|
| Code Graph | ★ **复用 codesearch（Zoekt + SCIP）** | **~700**（vs 原 1000）| ★ 中（接入 + commit ETL）|
| Discussion Graph | public-inbox + Python stdlib | ~1200 | ★ 重 |
| Bug Graph | python-bugzilla + NVD + Zenodo | ~1300 | ★ 中 |
| Doc Tree | ★ **复用 codesearch（chunks + Sphinx JSON）** | **~0**（vs 原 700）| ★ 轻 |
| **Cross-Graph Linker** | 仅正则 + 我方 ETL | ~1000（vs 原 1200，MAINTAINERS 延后） | ★★★ **核心壁垒** |
| CodeGraph 集成胶水 | — | **+300** | 接入工作 |
| **v1 总计** | | **~3800 行**（vs 原 ~5400，**节省 30%**） | |

### 8.2 三条主线（修订）

| 主线 | 阶段 | 模块 |
|------|------|------|
| Ingestion 主线 | v1.0 (M0-M3) | M3 — LKML / Bug / syzbot / commit metadata（**不再 ingest 内核源码与文档，由 CodeGraph 处理**） |
| Cross-Graph Linker 主线 | v1.0 起 → v1.1 加深 | M4 — Fixes 抽取 + commit-bug-message 关联 + Neo4j 同步 |
| 检索 / Agent 主线 | v1.0 起 → v1.2 完善 | M5 编排 CodeGraph MCP + 我方检索 + LLM 重排；M7 诊断 agent |

### 8.3 模块映射（修订）

| 子图 / 组件 | 实施模块 | 状态 |
|------------|---------|-----|
| Code Graph（codesearch 复用 + 集成胶水）| **M4** | 设计待写 |
| Doc Tree（codesearch 复用）| 不单独建模块 | 由 M5 路由消费 |
| LKML ingestion + 解析 | **M3** | 设计待写 |
| Bug Graph ingestion | **M3** | 设计待写 |
| Commit metadata ETL + Fixes: 抽取 | **M3 + M4** | 设计待写 |
| Cross-Graph Linker | **M4** | 设计待写 |
| BM25（我方数据）+ 路由（CodeGraph vs 我方）| **M5** | 设计待写 |
| LLM 调用 | **M1**（已完成）| [M1](modules/M1_llm_provider.md) |
| PG / Neo4j / 文件存储 | **M2**（已完成）| [M2](modules/M2_storage_schema.md) |

### 8.4 与外部数据源 + CodeGraph 的契约（修订）

| 数据源 | 协议 | 拉取方 | 频率 |
|--------|-----|--------|------|
| **CodeGraph MCP 服务** | MCP stdio/HTTP | Linux-Diag-Agent 启动时连接 | 持续 |
| OLK 内核 git（**代码索引**用）| git fetch | **codesearch 维护**（其 ingestion 流程）| 由 codesearch ops 控制 |
| OLK 内核 git + mainline 辅仓（**commit metadata** 用，[ADR-018](adr/ADR-018-commit-source-olk-kernel.md)）| git fetch | **我方** M3 ingestion | 周度增量 |
| lore.kernel.org | mbox + REST API | **我方** M3 ingestion | 周度增量 |
| Bugzilla (kernel.org + Red Hat) | REST API | **我方** M3 | 周度 |
| syzbot | HTML/API | **我方** M3 | 周度 |
| Zenodo dataset | HTTPS download | **我方** M3 | 季度 |
| NVD | JSON Feed | **我方** M3 | 周度 |
| kernel-doc（Sphinx JSON） | git fetch + sphinx-build | **codesearch 维护**（独立 kernel-docs-builder 容器）| 周度 |
| man-pages（如 codesearch 接入）| git fetch | **codesearch 维护** | 周度 |

---

## 9. 已知限制（v1 范围内不解决）

| 限制 | 原因 | 后续 |
|------|------|------|
| 仅 OLK Kernel 6.6 + 5.10 | codesearch 当前覆盖 | v2 加 v6.12 LTS 等 |
| LKML 只回溯 2020-12 起 5 年 | 全量 25 年抓取耗时 | 按需扩展 |
| MAINTAINERS 解析延后 | v1.0 用 file_path 推断够用 | v1.1 加 |
| 不索引 RHEL/SUSE enterprise kernel | 闭源/受限 | 企业版评估 |
| 跨版本函数 rename 追踪粗糙 | codesearch 也未深度支持 | v2 |
| Cross-Graph 链接置信度不区分等级 | v1 用二值 (有/无)| v1.2 引入 weighted edges |

---

## 10. 后续文档索引

- [PRD.md](PRD.md) — 产品需求
- [Architecture.md](Architecture.md) — 系统架构
- [ProjectPlan.md](ProjectPlan.md) — 项目计划
- [spike/Spike_Report.md](spike/Spike_Report.md) — **2026-05-15 KG 可行性 Spike 报告**（含 F1-F7 反馈）
- [M1 LLM Provider 抽象层](modules/M1_llm_provider.md)
- [M2 存储与 Schema](modules/M2_storage_schema.md)
- [M3 Ingestion 层](modules/M3_ingestion.md)
- [M4 Cross-Graph Linker + CodeGraph 集成](modules/M4_cross_graph_linker.md)
- [M5 检索编排层](modules/M5_retrieval_orchestration.md)
- [M6 主机 MCP 工具集](modules/M6_host_mcp_tools.md)
- [M7 诊断 Agent](modules/M7_diagnosis_agent.md)
- [M8 评测与可观测性](modules/M8_evaluation_observability.md)
- ADR 索引：[adr/](adr/)
  - [ADR-001 无 embedding](adr/ADR-001-no-embedding.md)
  - [ADR-002 无 reranker](adr/ADR-002-no-reranker.md)
  - [ADR-003 OpenAI Chat schema](adr/ADR-003-openai-chat-schema.md)
  - [ADR-004 claude_code provider](adr/ADR-004-claude-code-provider.md)
  - [ADR-005 单库多版本](adr/ADR-005-single-db-multi-version.md) ⚠️ **Superseded**
  - [ADR-006 PG tsvector BM25](adr/ADR-006-pg-tsvector-bm25.md)
  - [ADR-007 数据目录在 repo 内](adr/ADR-007-data-dir-in-repo.md)
  - [ADR-008 复用 codesearch](adr/ADR-008-reuse-codesearch.md) ★
  - [ADR-009 多版本通过 codesearch repos](adr/ADR-009-multi-version-via-codesearch-repos.md) ★
  - [ADR-010 LKML 2 年 bootstrap](adr/ADR-010-lkml-2y-bootstrap.md)
  - [ADR-011 Bugzilla 仅 kernel.org](adr/ADR-011-bugzilla-kernel-org-only.md)
  - [ADR-012 CodeGraph MCP HTTP 传输](adr/ADR-012-codesearch-http-transport.md) ★
  - [ADR-013 M5 检索策略：全 LLM 解析 + always-fire-all](adr/ADR-013-m5-retrieval-strategy.md)
  - [ADR-014 M6 主机工具 v1.0 范围](adr/ADR-014-m6-scope.md)
  - [ADR-015 M7 诊断 Agent 核心策略](adr/ADR-015-m7-diagnosis-strategy.md) ★
  - [ADR-016 M8 v1.0 最小化范围](adr/ADR-016-m8-minimal-observability.md)
  - [ADR-017 CodeGraph 命名规约](adr/ADR-017-codegraph-naming.md) ★
  - [ADR-018 Commit 来源 = OLK 内核仓 + 双生态桥接](adr/ADR-018-commit-source-olk-kernel.md) ★

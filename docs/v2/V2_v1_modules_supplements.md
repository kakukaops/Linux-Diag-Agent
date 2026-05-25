# V2 对 v1 模块的增量改动 + 新 ingester + 上线策略

| 字段 | 值 |
|------|---|
| 文档类型 | Supplement to v1 modules + new ingester + rollout |
| 版本 | v2 |
| 状态 | Design Draft（2026-05-20）|
| 关联文档 | [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [ProjectPlan.md](ProjectPlan.md) · [adr/](adr/) · [../v1/modules/](../v1/modules/) |
| 最后更新 | 2026-05-20 |

> v2 module 详设（M9-M12, M22-M23）覆盖了**新**模块；本文档覆盖**对 v1 模块（M2/M4/M5/M6/M7）的增量改动**、新增 ingester（gitee/atomgit issue）、以及 v2 上线/灰度策略。三者之前散落在 PRD / Architecture / ADR 里，缺一份明确文档；本文档统一整理。

---

## 1. 对 v1 模块的增量改动

### 1.1 M7 — 诊断 Agent（最大改造）

**v1 现状**：`agent/graph.py` 10 节点固定流水线。

**v2 改动**：

| 改动 | 涉及节点 | 任务 ID |
|------|---------|--------|
| 新增 `detect_taint_and_hw_signals` 节点 | triage | T-001 |
| `classify_fault` 重构为 `classify_fault_and_route` | triage | T-002 |
| 删除 `generate_hypotheses` / `verify_hypothesis` / `self_consistency` 三节点 | diagnosis | — |
| 替换为 **M22 ReAct Loop**子图 | diagnosis | T-003（M22）|
| `bind_claims` 之后加 `assess_evidence_strength` 子节点 | diagnosis | T-010 |
| `generate_report` 拆分为四出口（diagnosed / insufficient_evidence / hardware_suspected / inconclusive）| report | T-010 |
| 新增 `save_audit_trail` 节点（工具调用轨迹落库）| report | T-011 |

详细的节点/state 设计见 [Architecture §2](Architecture.md) 和 [M22 module 详设](modules/M22_react_loop_engine.md)。

**ADR 依据**：[ADR-019](adr/ADR-019-hybrid-react-deterministic.md) Hybrid 模式、[ADR-023](adr/ADR-023-hardware-first-sop-routing.md) Hardware-first 路由、[ADR-024](adr/ADR-024-insufficient-evidence-exit.md) Insufficient-evidence 出口。

**新文件 / 改动文件**：

```
agent/triage/nodes.py          # 改:加 detect_taint_and_hw_signals + 重构 classify_fault
agent/sop/definitions/hardware.yaml  # 新建（hardware SOP，[M11](modules/M11_hardware_layer.md)）
agent/diagnosis/nodes.py       # 改:删 hypotheses/verify/consistency 三节点
agent/diagnosis/evidence_score.py    # 新建（assess_evidence_strength）
agent/report/insufficient_evidence.py # 新建（不确定性报告渲染）
agent/report/hardware_report.py      # 新建（硬件路径报告渲染）
agent/graph.py                # 改:用 M22 ReAct 子图替换 hypotheses 三节点
agent/react/                   # 新目录,见 [M22](modules/M22_react_loop_engine.md)
```

---

### 1.2 M5 — 检索编排（质量提升 + 工具化）

**v1 现状**：7 路 BM25 召回 + LLM 重排，`retrieve()` 接受 `RetrievalQuery`。

**v2 改动**：

| 改动 | 任务 ID |
|------|--------|
| `retrieve` 节点启用 LLM query parsing（撤销硬编码 `use_llm=False`）| T-006 |
| 7 路检索函数封装为 LLM tool schema（M22 ReAct 可调用）| T-004 |
| BM25 同义词/复合词别名表 + tsquery 显式展开 | T-122 |
| 关键词来源改 `LLM 抽取的 error_keywords` 替代 `question.split()[:10]`| T-006 配套 |

**详细设计**：

- 同义词表位置：`retrieval/synonyms.py`，与 `tsquery` builder 同模块
- 同义词初始内容：`{softlockup: [soft_lockup, lockup], rcu_stall: [rcu, stall], ...}`（约 30 条核心术语）
- 工具 schema 注册：通过 M22 的 [tool registry](#3-工具注册与发现协议-s-3) 接入

**改动文件**：

```
retrieval/query_parser.py      # 改:开放 use_llm 参数,默认 True
retrieval/engine.py            # 改:LLM query parsing 默认启用,带 budget guard
retrieval/synonyms.py          # 新建（同义词表 + tsquery 展开）
retrieval/recall/*.py          # 改:接同义词展开
retrieval/tools.py             # 新建:7 路 search_* 工具的 LLM tool schema 声明
```

---

### 1.3 M4 — Cross-Graph Linker（加 3 个新查询工具）

**v1 现状**：`graph/linker.py` 维护 `link_commit_bug / link_commit_message / link_commit_cve`。

**v2 改动**：

| 工具 | 任务 ID | 实现要点 |
|------|--------|---------|
| `check_backport_status(upstream_sha, olk_version)` | T-007 | SQL 反查 `kernel_commit.upstream_commit`；返回 `{backported, olk_commit_hash, inclusion_type, ...}` |
| `get_regression_fixes(commit_hash)` | T-008 | SQL `kernel_commit.body ~ 'Fixes: <commit_hash>'`；用现有 `kernel_commit_trailer_parser` |
| `get_commit_diff(commit_hash)` | T-009 | `git show <hash>` wrapper（OLK 本地 repo）|
| Linker 扩展支持 gitee/atomgit URL 模式 | T-119 | 见 §2 gitee/atomgit ingester |

**改动文件**：

```
graph/queries.py               # 新建（3 个查询工具的 SQL + LLM tool schema）
graph/linker.py                # 改:加 gitee/atomgit URL pattern
graph/git_diff.py              # 新建（get_commit_diff 实现）
```

---

### 1.4 M6 — 主机 MCP 工具（补全）

**v1 现状**：`mcp-dmesg-journal` + `mcp-sosreport`，功能基础。

**v2 改动**：

| 改动 | 任务 ID |
|------|--------|
| `parse_journal` 工具（封装 `journalctl` 查询）| T-107 |
| `read_live_dmesg` 加 since_ts / level 过滤参数 | T-108 |
| `extract_call_trace` 独立暴露为工具（v1 已有内部解析器，包装即可）| T-109 |
| PID → container 映射工具（cgroup v2 + podman/docker/k8s 标签）| T-120 |

**改动文件**：

```
mcp_servers/dmesg_journal/
  server.py                    # 改:加过滤参数 + parse_journal 工具 + extract_call_trace 工具
mcp_servers/container_mapper/  # 新建（PID→container 映射,独立 MCP server）
```

`container_mapper` 独立 MCP server 的理由：cgroup v2 路径解析、podman/docker socket 访问需要不同权限，与 dmesg/journal 隔离。

---

### 1.5 M2 — Storage Schema（迁移 + 一张新表）

**v2 改动**：

| 改动 | 任务 ID | 内容 |
|------|--------|------|
| `agent_tool_trace` 表 + Alembic 迁移 | T-012 | schema 见 [Architecture §6.3](Architecture.md) |
| `bug.source` 取值扩展（含 `gitee` / `atomgit`）| T-118 | 仅注释 / enum 扩展，列结构不变 |
| `system_baseline` 表 | M10 内 | M10 详设 §4 给出 schema |
| `eval_cases.v2_fields` JSONB 列 | M23 内 | M23 详设 §3.1 |
| `eval_human_judgments` 表 | M23 内 | M23 详设 §3.1 补充（见 [M-5 修复](#m-5-eval_human_judgments-table-schema)）|

迁移文件：`storage/pg/migrations/versions/0004_*.py` 起步（v1 当前 0003）。

---

## 2. 新增 ingester：gitee/atomgit issue（M-6 修复）

承自 [ADR-022](adr/ADR-022-gitee-atomgit-issue-ingest.md)。补充模块详设。

### 2.1 目标

补齐 `link_commit_bug = 0` 的数据缺口：OLK commits 63K+ 引用 gitee.com、7.9K+ 引用 atomgit.com 的 issues，但 v1 `bug` 表仅装 kernel.org bugzilla。

### 2.2 路径与文件结构

继承 `ingest/base.py:BaseIngester` 模式（同 lkml/bugzilla/syzbot/nvd）：

```
ingest/gitee_issue/
  __init__.py
  __main__.py                  # python -m ingest.gitee_issue incremental
  ingester.py                  # GiteeIssueIngester(BaseIngester)
  api_client.py                # gitee.com / atomgit.com REST API client
  parser.py                    # issue JSON → ParsedIssue
```

### 2.3 ingester 协议

| 项 | 值 |
|----|---|
| `source_name` | `gitee_issue`（一个 ingester 处理两个站点，通过 backend 字段区分）|
| 限速 | 1.5s/req（同 lkml/bugzilla 基线）|
| Checkpoint keys | `last_change_time_per_backend = {"gitee": ISO, "atomgit": ISO}` |
| 入库目标 | `bug` 表，`source ∈ {gitee, atomgit}` |

### 2.4 API 调用

**gitee API**：

```
GET https://gitee.com/api/v5/repos/openeuler/kernel/issues
  ?state=all&since={last_change_time}&page={N}&per_page=100
Headers: Authorization: token {GITEE_TOKEN}  # 提升限速;无凭据也可但配额低
```

**atomgit API**：

```
GET https://atomgit.com/api/v3/repos/openeuler/kernel/issues
  ?state=all&since={last_change_time}&page={N}&per_page=100
```

（atomgit API 路径以实际文档为准，T-116 spike 1 天验证）

### 2.5 入库 schema 映射

| API 字段 | `bug` 表列 |
|---------|-----------|
| `number` | `external_id` |
| `title` | `title` |
| `body` | `description`（含 BM25 索引） |
| `state` | `status` |
| `created_at` | `created_at` |
| `updated_at` | `updated_at` |
| `closed_at` | `closed_at` |
| `labels` | 解析 `category=*` 等标签 → `component` / `subsystem` |
| —（固定）| `source = 'gitee' \| 'atomgit'` |

### 2.6 Linker 扩展

`graph/linker.py` 加 URL 模式：

```python
_GITEE_URL_RE = re.compile(r"gitee\.com/openeuler/kernel/issues/(\w+)")
_ATOMGIT_URL_RE = re.compile(r"atomgit\.com/openeuler/kernel/issues/(\w+)")
```

匹配后从 `bug` 表查对应 `source + external_id`，写 `link_commit_bug` 关联。

### 2.7 PD 估算

| Task | 内容 | PD |
|------|------|----|
| T-116 | gitee/atomgit issue API client（含两站点 API spike）| 4 |
| T-117 | ingester（同 BaseIngester 模式 + Quarantine）| 3 |
| T-118 | `bug.source` 扩展 + 数据迁移 | 1 |
| T-119 | Cross-Graph Linker URL 模式扩展 | 2 |

合计 10 PD（v2.1 M18 主线）。

### 2.8 风险

- gitee/atomgit API 限速 / 封 IP → 1.5s/req + token 凭据
- API 文档不全（atomgit）→ T-116 含 1 天 spike 对齐
- 重复 issue ID（gitee/atomgit 各自编号空间）→ `source + external_id` 联合唯一约束

---

## 2bis. LKML 知识数据架构（[ADR-025](adr/ADR-025-knowledge-data-online-default.md) 三层模型）

### 2bis.1 背景：为什么按 list+日期窗口 bulk 摄入是错的

2026-05-21 实测（v1 收尾阶段）：LKML stable list 摄入完成、59,715 条入库、Linker 重跑后，**`link_commit_message` 仍为 0**。

```
commit body 引用的去重 message-id 总数:  26,221
其中在 lkml_message 中存在的:                   11   ← 万分之四
带日期前缀且在 180 天窗口内:                     91   ← 任何 180 天摄入的命中上限
窗口外（太旧，1-3 年前的原始 patch 讨论）:    22,246（85%）
```

两个原因叠加：① **时间窗错位**——OLK backport commit 的 `Link:` 指向 1-3 年前的原始讨论；② **list 错位**——commit 的 lore URL 大量是 `/r/`（27,950）`/all/`（1,092）这类 list 无关寻址。**按 list+日期 bulk 摄入对 `link_commit_message` 几乎无效，且耗时 34.5h**——这是 [ADR-025](adr/ADR-025-knowledge-data-online-default.md) 修正 offline-first 的实测依据。

### 2bis.2 三层模型总览

[ADR-025](adr/ADR-025-knowledge-data-online-default.md) 把 LKML 从「bulk 摄入」改为三层：

```
L1 本地计算结构   link_commit_message / lkml_thread DAG 元数据。很小。永远本地。
        ↑
L2 本地智能缓存   lkml_message 表 = 缓存（引用驱动 + 检索驱动累积）。
                  本地 BM25 索引、排序、LLM 摘要都在这层。
        ↑ 回填
L3 远程发现服务   lore.kernel.org（Xapian 搜索 + /raw + /t.mbox.gz）
```

`lkml_message` 表语义从「全量语料」变「智能缓存」。

### 2bis.3 L1 — 定向抓取（commit 引用的 message-id）

把 commit 实际引用的 26,221 个 message-id 精确抓回来，喂 L2 缓存初值：

```
1. 从 kernel_commit.body 正则提取所有 lore message-id（去重 ~26K）
2. 逐个 GET lore.kernel.org/all/<msgid>/raw（/all/ 全 list 全历史，按 id 寻址）
3. 复用 ingest/lkml/parser.py 解析 + 入库 lkml_message
4. Linker 重跑 → link_commit_message 0 → ~26,000
```

入口：`python -m ingest.lkml backfill_referenced`

| 项 | 值 |
|----|---|
| message-id 来源 | `SELECT DISTINCT regexp_matches(body, lore-url-re) FROM kernel_commit` |
| 抓取端点 | `lore.kernel.org/all/<msgid>/raw` |
| 限速 | 1.5s/req（`_REQUEST_DELAY_S`）|
| 容错 | 复用 fetcher.py 的 `TransportError` retry；404 → skip + quarantine |
| 增量 | checkpoint 存「已抓 message-id 集合」；下次只抓新引用 |
| 耗时 | ~26K × 1.5s ≈ 11h 一次性 |

### 2bis.4 L2 — 懒加载缓存（read-through cache）

诊断时引用到一条**未缓存**的 message → 现取 → 回填 `lkml_message`：

```
诊断需要 message X → 查 lkml_message 缓存
  命中 → 直接用（本地，~1ms）
  未命中 → GET /all/X/raw → 解析 → 写入 lkml_message → 用
线程上下文 → GET /t.mbox.gz 取整线程
```

缓存只增不减；图谱靠"用"收敛到完整。新增 `ingest/lkml/lazy.py`，复用 `parser.py` / `thread_builder.py`。

**线程摘要同样 read-through**：长线程（≥30 封）首次被读时按需做 LLM 三段摘要，写入 `lkml_thread.summary_*` 缓存，后续命中即复用；短线程不摘要，直接读原文（更新鲜、在故障语境里）。`lazy.get_thread_summary()` 是该读穿函数。摘要**不在摄入时批量预算**——`backfill_referenced` 保持纯抓取，不烧 LLM；旧的批量摘要仅保留在 `incremental()` 的 offline 快照预热路径里。触发点是 agent 读线程的工具（M22 era）。

`lkml` 检索路由从「纯本地 PG BM25」改为**混合**：

```
retrieve(lkml route):
  ① 本地 L2 缓存 BM25（body_tsv，对已缓存子集，排序可控）
  ② online 模式额外：lore.kernel.org/all/?q=<keywords>&x=A（Xapian 搜全量历史）
  ③ 合并两源结果；②发现的新 message 回填 L2 缓存
  offline 模式：只跑 ①
```

lore search 客户端复用 `fetcher.py` 的 HTTP + Atom 解析（同端点，`q=` 从日期换关键词）。一次诊断打一次 lore search，远低于限速。

### 2bis.6 online / offline 双模式

| 模式 | L2 | L3 | 适用 |
|------|----|----|------|
| `online`（默认）| 靠用收敛 | 启用 lore search | 有外网的客户 |
| `offline`（气隙）| 出厂预热打包 | 禁用 | 气隙机房客户 |

由 `configs/` 的 `knowledge.mode` 控制。offline 模式损失"在线发现新讨论"那部分召回，可靠出厂更大快照缓解。

### 2bis.7 PD 估算

| Task | 内容 | PD | 层 |
|------|------|----|----|
| T-125 | message-id 提取 SQL + 去重 | 0.5 | L1 |
| T-126 | `backfill_referenced` 子命令 | 2 | L1 |
| T-127 | checkpoint + 增量逻辑 | 0.5 | L1 |
| T-128 | lore search 客户端（`retrieval/recall/lore_search.py`，复用 fetcher）| 3 | L3 |
| T-129 | LKML 懒加载 + 缓存回填（`ingest/lkml/lazy.py`）| 3 | L2 |
| T-130 | `recall/lkml.py` 混合化（本地缓存 BM25 + lore search 合并）| 3 | L2+L3 |
| T-131 | weekly_sync 改造 + summarizer 懒化 + `knowledge.mode` config + 迁移 | 3 | 配套 |

合计 **15 PD**（v2.1 M17/M18）。`incremental` bulk 逻辑保留但降级为 offline 模式快照预热专用。

### 2bis.8 风险

- 26K 次定向抓取请求 → 严守 1.5s/req；分批可断点续；监控 429
- 部分 message-id 已从 lore 归档移除（404）→ skip 不阻断
- commit body 提取的 message-id 有噪声 → 格式校验（`@` 存在、长度合理）
- online 模式依赖 lore 可达性 → 缓存层缓解；不可达时降级到 cache-only
- 评测可复现 → 评测模式 pin 语料快照（eval fixture）

---

## 3. 工具注册与发现协议（S-3 修复）

### 3.1 问题

每个 module 都说"M22 ReAct Loop 会注册这些工具"，但没有任何一处规定**工具去哪里注册、注册的协议是什么**。两种工具形态（库 vs MCP HTTP）如何被统一调用？

### 3.2 决策

**单一 ToolRegistry，两种 transport 抽象统一**：

```python
# agent/react/tool_registry.py

class ToolTransport(Enum):
    PYTHON = "python"        # 直接 Python 函数调用（M5 检索、M12 监控适配器）
    MCP_HTTP = "mcp_http"    # 远程 MCP server（M6 dmesg、M9 vmcore、M11 hw、M10 change）

@dataclass
class RegisteredTool:
    name: str
    schema: dict             # OpenAI function calling JSON schema
    transport: ToolTransport
    invoker: Callable        # PYTHON: 函数指针；MCP_HTTP: lambda args: http_call(url, args)
    category: str            # A/B/C/D/E/F/G/H（[AgentThink 类别](../AgentThink.md)）
    available_in_routes: set[str]  # {"kernel", "kernel+vmcore", ...}

class ToolRegistry:
    _registry: dict[str, RegisteredTool] = {}

    @classmethod
    def register(cls, tool: RegisteredTool) -> None: ...

    @classmethod
    def for_route(cls, route: str) -> list[RegisteredTool]:
        return [t for t in cls._registry.values() if route in t.available_in_routes]
```

### 3.3 注册时机

模块在 import 时**自注册**（类似 SQLAlchemy declarative pattern）：

```python
# 例:retrieval/tools.py
from agent.react.tool_registry import ToolRegistry, RegisteredTool, ToolTransport
from retrieval.engine import retrieve

def _search_commits(**kwargs):
    return retrieve(...)

ToolRegistry.register(RegisteredTool(
    name="search_commits",
    schema={...},
    transport=ToolTransport.PYTHON,
    invoker=_search_commits,
    category="A",
    available_in_routes={"kernel", "kernel+vmcore", "change", "unknown"},
))
```

```python
# 例:mcp_servers/crash_forensics/client.py（agent 侧调 M9 MCP server）
ToolRegistry.register(RegisteredTool(
    name="analyze_vmcore",
    schema={...},
    transport=ToolTransport.MCP_HTTP,
    invoker=lambda **args: mcp_call("http://localhost:8090/analyze_vmcore", args),
    category="F",
    available_in_routes={"kernel+vmcore", "unknown"},
))
```

### 3.4 启动期发现

`agent/graph.py` 启动时显式 import 触发所有 module 的 register：

```python
def _init_tool_registry():
    import retrieval.tools          # noqa: F401 - registers L2 retrieval
    import graph.queries             # noqa: F401 - registers L2 graph queries
    import mcp_servers.dmesg_journal.client  # noqa: F401 - registers M6
    import mcp_servers.crash_forensics.client # noqa: F401 - registers M9
    import mcp_servers.hardware.client       # noqa: F401 - registers M11
    import mcp_servers.change_correlator.client # noqa: F401 - registers M10
    import clients.observability.tools        # noqa: F401 - registers M12
```

启动失败的 import 通过 try/except 记 warning（容忍部分 backend 缺失），但 logic-required tool 缺失会硬失败。

### 3.5 dispatcher（统一调用）

M22 ReAct loop 不关心 transport：

```python
async def dispatch_tool(tool_call: ToolCall, timeout_s: int = 30) -> dict:
    tool = ToolRegistry._registry[tool_call.name]
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(tool.invoker, **tool_call.args),
            timeout=timeout_s,
        )
    except TimeoutError:
        return {"error": "tool_timeout", "tool": tool_call.name}
    except Exception as exc:
        return {"error": str(exc), "tool": tool_call.name}
```

### 3.6 修订 M22 module 详设

M22 [§3](modules/M22_react_loop_engine.md) 的 `ROUTE_TO_TOOLS` 改为**从 ToolRegistry 动态查询**：

```python
def get_tools_for_route(route: str) -> list[dict]:
    return [t.schema for t in ToolRegistry.for_route(route)]
```

不再硬编码工具名列表（[M22 §3](modules/M22_react_loop_engine.md) 当前的 `ROUTE_TO_TOOLS` dict 在实施时改为派生自 Registry）。

---

## 4. v1 → v2 上线策略（L-10 修复）

### 4.1 目标

v2 上线**不能破坏 v1 用户**，且要能验证"v2 真比 v1 好"。

### 4.2 三阶段灰度

```
阶段 1: 影子模式（v2.0 acceptance 后 2 周）
   - 用户请求仍走 v1 流水线返回报告
   - 同请求异步触发 v2 ReAct（影子，不返回给用户）
   - 后台对比 v1/v2 报告差异 + agent_tool_trace 落库
   - 评估指标:Recall@10、报告一致性、token 成本

阶段 2: opt-in（v2.0 acceptance 后 4-8 周）
   - CLI 加 --v2 / --v1 flag（默认 v1）
   - SRE 主动选 v2 试用
   - 用户反馈 + 评测周报观察是否退化
   - Recall@10 周报 ≥ v1 后 + 无重大回归 → 进入阶段 3

阶段 3: 默认 v2，v1 fallback（v2.0 acceptance 后 ≥8 周）
   - 默认走 v2 ReAct pipeline
   - CLI 仍保留 --v1 flag 作为应急 fallback
   - v1 保留 ≥ 6 个月,期间持续观察
   - v2.2 验收后评估是否下线 v1
```

### 4.3 CLI 兼容性

```bash
# v1 兼容（默认行为不变）
diag-agent diagnose --upload /var/log/dmesg

# v2 显式选用
diag-agent diagnose --upload /var/log/dmesg --v2

# 阶段 3 后:
diag-agent diagnose --upload /var/log/dmesg          # 默认 v2
diag-agent diagnose --upload /var/log/dmesg --v1     # 应急 fallback
```

### 4.4 报告 JSON schema 兼容

v2 报告 schema 是 v1 的**超集**（[Architecture §6.4](Architecture.md)）：

- 所有 v1 字段保留（向后兼容）
- v2 新字段加在顶层（`verdict`、`route`、`tool_call_trace`、`iterations` 等）
- 旧消费方（v1 schema 解析器）忽略未知字段即可

### 4.5 影子模式实现

```python
# agent/shadow.py
def diagnose_with_shadow(raw_input):
    v1_result = v1_diagnose(raw_input)
    asyncio.create_task(_shadow_v2(raw_input, v1_result))  # 异步,不阻塞
    return v1_result  # 用户看到 v1 报告

async def _shadow_v2(raw_input, v1_result):
    v2_result = await v2_diagnose(raw_input)
    save_comparison(v1_result, v2_result)  # 落 shadow_comparison 表
```

### 4.6 任务对照

[T-027](ProjectPlan.md) **v1→v2 兼容性测试套件 + 灰度上线策略**（3 PD）扩展到本节定义的 4 阶段流程。

---

## 5. v1 ancestry 对照表（L-9 修复）

明确 v2 各任务对应 v1 哪个模块的改造：

| v2 任务 | v1 模块 | 关系 |
|---------|--------|------|
| T-001 detect_taint_and_hw_signals | M7 triage | **新节点** |
| T-002 classify_fault_and_route | M7 triage | **重构**（原 classify_fault） |
| T-003 ReAct Loop | M7 diagnosis | **替换**（删 hypotheses 三节点）→ M22 module |
| T-004 检索 tool schema 封装 | M5 + M6 | **包装**（不改 v1 函数语义） |
| T-006 retrieve LLM parse | M5 | **撤销硬编码**（开 use_llm=True） |
| T-007/008/009 L2 查询工具 | M4 | **新增**（不动 v1 linker 主干） |
| T-010 insufficient_evidence 出口 | M7 diagnosis + report | **新增分支** |
| T-011 报告 JSON schema 扩展 | M7 report | **超集扩展** |
| T-012 agent_tool_trace 表 | M2 | **新表** |
| T-013–T-018 类别 F | — | **新 M9** |
| T-019–T-020 类别 H | — | **新 M11** |
| T-021 数据集 | M8 eval | **扩展数据集** → M23 |
| T-101-T-106 类别 G | — | **新 M10** |
| T-107-T-109 类别 B 补全 | M6 | **新工具** |
| T-110-T-115 类别 C | — | **新 M12** |
| T-116-T-119 gitee ingester | M3 | **新 ingester** |
| T-120 PID→container | M6 | **新 MCP server** |
| T-121 weekly_sync Linker 自动重跑 | M3 scripts | **shell 集成** |
| T-122 BM25 同义词 | M5 | **新 module 内文件** |
| T-201-T-203 多 backend | M12 扩展 | **新 adapter** |
| T-204-T-209 eval 反馈环 | M8 eval | **新 M23** |

---

## 6. 与其他 v2 文档的关系

| 本文档解决 | 其他文档涉及 |
|-----------|-------------|
| §1 v1 模块改动 | PRD F-1–F-15 / Architecture §1.4 / ProjectPlan §2.2-2.4 |
| §2 gitee/atomgit ingester | ADR-022 / ProjectPlan T-116-T-119 |
| §3 工具注册协议 | Architecture §6 / M22 §3 |
| §4 上线策略 | PRD §5.1 / ProjectPlan T-027 |
| §5 v1 ancestry 对照 | 所有 v2 文档 |

---

*参考：[../v1/modules/](../v1/modules/) · [Architecture.md](Architecture.md) · [PRD.md](PRD.md) · [ProjectPlan.md](ProjectPlan.md)*

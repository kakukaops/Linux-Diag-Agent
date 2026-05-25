# Linux-Diag-Agent v1 — 项目现状

| 字段 | 值 |
|------|---|
| 最后更新 | 2026-05-21 |
| 状态 | 知识库摄入收尾（LKML stable 完成，linux-mm 补跑中）；核心功能全部可用 |

---

## 1. 当前运行状态

| 组件 | 状态 | 说明 |
|------|------|------|
| PostgreSQL + Neo4j | ✅ 运行中 | docker compose up -d；schema 已迁移至 0003 |
| kernel_commit ingester | ✅ 完成 | 1,299,549 commits；hash 污染已修复（见技术债务） |
| NVD CVE ingester | ✅ 完成 | 15,502 行；2,622 CVE 含 fix_commits |
| Bugzilla ingester | ✅ 完成 | 3,700 条 |
| syzbot ingester | ✅ 完成 | 999 条；scraper URL/selector 已修复 |
| LKML ingester（stable）| ✅ 完成 | 59,715 条入库（stable list 180d，34.5h）|
| LKML ingester（linux-mm）| ⏳ 补跑中 | pid 40859；首次因 RemoteProtocolError 失败，fetcher 修复后重跑 |
| Cross-Graph Linker | ✅ 已重跑（2026-05-21）| link_commit_cve 33→401，upstream_bridged +500 |
| Neo4j rebuild | ⏸ 待执行 | linux-mm 补跑 + 定向抓取后再跑 |
| CodeGraph MCP | ✅ 已接入 | olk-kernel (6.6)；修复了 health check / SSE 解析 / repos 参数 |
| v1.0 验收评测 | ✅ 已执行 | 5/5 案例产出报告；syzbot 路由现已激活 |

**⚠️ link_commit_message = 0（2026-05-21 实测确认为数据覆盖缺口，非 bug）**：
commit 引用的 26,221 个 lore message-id 中只有 11 个在 lkml_message 内——OLK backport commit 的 `Link:` trailer 指向 1-3 年前、跨多个 list 的原始 patch 讨论，而我们按"stable list + 180 天"摄入覆盖不到。**按 list+日期 bulk 摄入解决不了**；真正解法是定向抓取 commit 引用的 message-id（`lore.kernel.org/all/<msgid>/raw`），已列入 v2（[docs/v2/V2_v1_modules_supplements.md §2bis](../v2/V2_v1_modules_supplements.md)，T-125/126/127）。

---

## 2. 近期修复（2026-05-18 ~ 19）

| 问题 | 修复位置 | 影响 |
|------|---------|------|
| kernel_commit hash 污染（120 万行）| `git_wrapper.py _parse_record()` | 跨图链接 / commit 路由完全失效 |
| syzbot URL 格式变更 | `scraper.py list_crashes()` + `fetch_crash_detail()` | 入库 0 行；改为 `extid=` 参数 |
| syzbot CSS selector 变更 | `scraper.py list_crashes()` | `table.list` → `table.list_table` |
| syzbot 标题爬取错误 | `scraper.py fetch_crash_detail()` | `<h1>` 固定为 "syzbot"；改为 `<title>` |
| CodeGraph health check 误报 | `client.py health_check()` | `/health` 返回 404；改为 MCP initialize 探活 |
| CodeGraph SSE 响应解析失败 | `client.py _call()` | 缺少 `Accept: text/event-stream`；新增 `_parse_sse_response()` |
| CodeGraph `repo` 参数格式错误 | `client.py search_code()` | `repo` 单值 → `repos` 数组 |
| CodeGraph repo 名称配置错误 | `configs/default.yaml` | `olk-kernel-v6.6` → `olk-kernel`（实际索引名） |
| reranker 空响应 / 额外文本 | `reranker.py _llm_rerank()` | 正则提取 `[...]` 数组；空响应提前拦截 |
| 检索跨路由分数不可比 | `engine.py _normalize_by_route()` | commit 分数 4-8 vs lkml 分数 13-25；max 归一化后公平竞争 |
| reranker 发送 100 条给 LLM 导致截断 | `reranker.py _RERANK_INPUT_CAP=20` | 仅取 BM25 top-20 发给 LLM，避免 score length mismatch |
| LangGraph TypedDict 节点 state 丢失 | `agent/*/nodes.py` | 全部节点签名改为 `def node(state: dict) -> dict` |
| `.provider` → `.backend` 属性错误 | 4 个文件 | `LLMRoleConfig` 字段名不一致 |

---

## 3. 技术债务

| 债务 | 影响 | 估算 | 优先级 |
|------|------|------|--------|
| `link_commit_bug` SQL 使用 raw text() 而非 ORM | 难以测试 | 2 PD | 低 |
| syzbot scraper 无 HTTP 集成测试 | 覆盖缺失 | 1 PD | 低 |
| 评测数据集只有 5 例，且为合成问题 | Recall@10 无实测基准 | 3 PD | 中 |
| gitee/atomgit issue 无入库路径 | `link_commit_bug` 始终为 0（OLK 主要引用 gitee issues）| 3 PD | 中 |

**已解决（本轮）**：
- ~~PgCheckpointer 未挂入~~ → `PostgresSaver` 已集成，每次诊断写入 12 条 checkpoint 记录
- ~~`weekly_sync.sh` 无 lock file~~ → flock 已存在（行 33-43）

**已知数据缺口**：
- `link_commit_bug = 0`：OLK commits 的 `bugzilla:` trailer 指向 gitee/atomgit issues，不在 `bug` 表；
  指向 bugzilla.kernel.org 的 1,625 条 commit 对应的 bug 因 Bugzilla 日期窗口过旧未入库。
  v1.1 增加 gitee issue ingester 后可修复。
- `link_commit_message = 0`：LKML 仍在摄入；完成后重跑 Linker 可补充。

---

## 4. ADR 回顾

| ADR | 决策 | 结论 |
|-----|------|------|
| ADR-001 | 无向量嵌入，纯 BM25 | **正确**：离线 MVP 阶段降低复杂度；PG tsvector 已够用 |
| ADR-004 | claude_code 作为 subprocess adapter | **正确**：无需 API key；`claude -p --output-format stream-json` 稳定 |
| ADR-009 | CodeGraph MCP HTTP | **可用**：但 API 细节（SSE、repos 数组）与文档不符，需维护 |
| ADR-018 | OLK inclusion header 二值规则 | **正确**：mainline/stable → backport；其余 → native；开放集自动兼容 |
| ADR-016 | v1.0 极简 obs（文件日志替代 Prometheus）| **正确**：节省开发量；grep JSONL 足够调试 |

**值得改进**：

| 问题 | 改进方向 |
|------|---------|
| 7 路并行检索无优先级 | v1.1+ 可加 route_weight 按故障类型动态调权 |
| Self-Consistency K=3 全量调用 | 可加 complexity classifier 先判断是否需要 K=3 |
| Neo4j 数据图尚未真实加载 | 评测后决定是否迁移至纯 PG |
| code 路由仅对符号名有效 | `keywords` 提取需优化为偏向函数名/符号名 |

---

## 5. 下一步

### 5.1 立即

1. **等待 LKML 完成** → 运行 Cross-Graph Linker（`python -c "from graph.linker import run_linker, link_nvd_commits; ..."`)
2. **Neo4j rebuild**（`graph/neo4j_rebuild.py`）

### 5.2 中期

1. **补充评测数据集到 30 例**：需真实告警案例，填充 `expected_commit_hashes`
2. **Recall@10 基准测试**：完整知识库下重跑评测
3. **vLLM 部署**（GPU 就绪后，WBS 10.1-10.2）

---

## 6. 性能目标（待实测）

| 指标 | 目标 | 当前估算 |
|------|------|---------|
| M5 retrieve P95（无 PageIndex）| ≤ 1s | ~0.3s（纯 BM25） |
| M7 diagnose P95（K=1） | ≤ 60s | ~200s（OpenRouter 限流下） |
| LKML 周度增量同步 | ≤ 30min | 未实测（首次 180d 约 2-4h） |
| BM25 Recall@10 | ≥ 70% | **合成案例 6.7%（不可用作基准）；见备注** |
| 根因正确率 (A=1) | ≥ 50% | 未实测（需人工评审） |

**Recall@10 备注**：合成问题（"How to diagnose OOM?"）产生 74K+ BM25 候选，预期 commit 排名 >10 不被召回。真实 dmesg 包含精确函数名/地址，OR 候选集收窄，召回率预计显著提升。6.7% 为方法论缺陷，非系统 bug。真实案例评测待补充。

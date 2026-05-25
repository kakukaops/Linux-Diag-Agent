# M5 — 检索编排层 设计文档

> **⚠️ 设计规格文档**：本文档为实施前的原始设计规格（定稿于 2026-05-15）。实际实现以代码为准，两者可能存在偏差。如需了解当前实现状态，请阅读对应目录下的 `CLAUDE.md` 和源代码。


| 字段 | 值 |
|------|---|
| 模块编号 | M5 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M1](M1_llm_provider.md) · [M2](M2_storage_schema.md) · [M3](M3_ingestion.md) · [M4](M4_cross_graph_linker.md) · [Architecture](../Architecture.md) |
| 关联 ADR | [ADR-002](../adr/ADR-002-no-reranker.md) · [ADR-004](../adr/ADR-004-claude-code-provider.md) · [ADR-008](../adr/ADR-008-reuse-codesearch.md) · [ADR-013](../adr/ADR-013-m5-retrieval-strategy.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

### 1.1 目标

向 M7 诊断 Agent 提供**统一的证据收集接口**：输入查询（自然语言 + 结构化提示）→ 输出**经 LLM 重排的 top-k Evidence 列表**，每条带规范引用（供 Claim-Evidence Binding）。

M5 是**编排层**，不引入新检索算法；底层调用 CodeGraph MCP（M4 client）+ 我方 PG/Neo4j。

### 1.2 在范围

| 子能力 | 形态 |
|--------|------|
| 查询解析（NL → 结构化提示）| **全 LLM 解析**（[ADR-013](../adr/ADR-013-m5-retrieval-strategy.md)） |
| 多源召回（7 路 always-fire-all） | **并行无差别触发**（[ADR-013](../adr/ADR-013-m5-retrieval-strategy.md)）|
| PageIndex 走查 | **两层并存**：低阶 passthrough + 高阶封装 |
| LLM 重排 | Navigator backend，触发条件保守 |
| 结果归一化（Candidate → Evidence）| Schema 适配 |
| 缓存 | 依赖 M1 LLM 缓存 + CodeGraph 自身 |
| 降级链 | CodeGraph 不可达 / 配额耗尽 / 召回为空 |

### 1.3 不在范围

| 项 | 处理位置 |
|----|---------|
| 直接全文索引 | CodeGraph（代码 / 文档）+ M2 PG tsvector（LKML/Bug）|
| 符号定义查询 | CodeGraph SCIP |
| Hypothesis 生成 / 诊断推理 | M7 |
| PageIndex 节点描述生成 | CodeGraph |

## 2. 关键决策摘要

| # | 决策 | 出处 |
|---|------|------|
| D1 | Query 解析 = **全 LLM 解析**（不走规则）| [ADR-013](../adr/ADR-013-m5-retrieval-strategy.md) |
| D2 | 多源召回 = **Always-fire-all 7 路**（不基于 query 类型筛选）| [ADR-013](../adr/ADR-013-m5-retrieval-strategy.md) |
| D3 | PageIndex 走查 = **两层并存**（高阶封装 + 低阶 passthrough）| 本文 §6 |
| D4 | Evidence 缓存 = **不单独维护**，靠 M1 LLM 缓存 + CodeGraph 自身 | 本文 §8 |
| D5 | LLM 重排触发 = 候选 > 10 且未撞 Pro 配额阈值 | 本文 §7 |

## 3. 输入输出接口

### 3.1 Query 对象（M7 → M5）

```python
@dataclass
class RetrievalQuery:
    # 必填
    natural_language: str               # 用户原始问题
    
    # 可选：M7 已识别的结构化提示（M5 仍会全 LLM 解析做最终决策）
    kernel_version: str | None = None   # 'v6.6' | 'v5.10' | None
    fault_domain: str | None = None     # 'oom' | 'soft_lockup' | 'deadlock' | ...
    subsystem_hint: str | None = None   # 'mm' | 'fs' | 'drivers/net' | ...
    stack_frames: list[str] | None = None
    error_keywords: list[str] | None = None
    time_window: tuple[datetime, datetime] | None = None
    
    # 检索控制
    routes: set[Route] | None = None    # None = always-fire-all 7 路
    top_k: int = 10
    enable_rerank: bool = True
    
    # 元信息
    trace_id: str
    requested_by: str                   # 'triage' | 'diagnosis' | 'eval'
```

### 3.2 Evidence 对象（M5 → M7）

```python
@dataclass
class Evidence:
    source: Literal['code', 'doc', 'lkml', 'bug', 'syzbot', 'commit', 'cve']
    ref: str                            # 规范引用，见 §3.3
    snippet: str                        # 显示文本（截断 ~500 字）
    score: float                        # 0-1
    cite: dict                          # Claim-Evidence Binding 用
    route: str                          # 哪条路径召回
    rerank_reason: str | None = None    # LLM 重排理由
    metadata: dict
```

### 3.3 规范引用（`ref`）

| source | ref 格式 | 示例 |
|--------|---------|------|
| code | `<repo>:<file>:<line_start>-<line_end>` | `olk-kernel-v6.6:mm/oom_kill.c:142-180` |
| doc | `<repo>:<source_path>#<anchor>` | `olk-kernel-v6.6:admin-guide/mm/concepts.rst#fragmentation` |
| lkml | `<message-id>` | `<20240218.143@kernel.org>` |
| bug | `<source>:<source_id>` | `bugzilla.kernel.org:217543` |
| syzbot | `syzbot:<crash_id>` | `syzbot:abc123def456` |
| commit | `<short_hash>` | `abc1234567` |
| cve | `<cve_id>` | `CVE-2024-12345` |

## 4. Query 解析（全 LLM）

### 4.1 LLM 解析 prompt

```python
QUERY_PARSE_PROMPT = """\
You are a Linux kernel diagnosis query parser. Given a user query (natural language),
extract structured fields for downstream retrieval.

User query:
---
{natural_language}
---

Already provided hints (do NOT override unless query contradicts):
- kernel_version: {hint_version}
- fault_domain: {hint_fault}
- subsystem_hint: {hint_subsystem}

Return JSON with the following fields (use null when unknown):
{{
  "kernel_version": "v6.6" | "v5.10" | null,
  "fault_domain": "oom" | "soft_lockup" | "deadlock" | "panic" | 
                  "io_hang" | "network" | "perf" | "sched" | null,
  "subsystem_hint": "mm" | "fs" | "net" | "drivers/net" | ... | null,
  "error_keywords": ["...", "..."],   # 1-5 关键 token: 错误码、API 名、函数名
  "stack_top_functions": ["func1", ...] | null,   # 如 NL 中提到
  "time_window_iso": ["start", "end"] | null,
  "query_intent": "concept" | "incident" | "symbol_lookup" | "history" | "fix_status"
}}
"""

async def parse_query(q: RetrievalQuery) -> ParsedQuery:
    response = await navigator_llm.chat(
        messages=[
            {"role": "system", "content": "Parse kernel diagnosis queries to JSON."},
            {"role": "user", "content": QUERY_PARSE_PROMPT.format(
                natural_language=q.natural_language,
                hint_version=q.kernel_version or 'null',
                hint_fault=q.fault_domain or 'null',
                hint_subsystem=q.subsystem_hint or 'null',
            )}
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        metadata={"trace_id": q.trace_id, "stage": "query_parse"}
    )
    
    parsed = json.loads(response.content)
    
    # 合并：M7 hints 优先（如非空），LLM 输出补全
    return ParsedQuery(
        natural_language=q.natural_language,
        kernel_version=q.kernel_version or parsed.get('kernel_version'),
        fault_domain=q.fault_domain or parsed.get('fault_domain'),
        subsystem_hint=q.subsystem_hint or parsed.get('subsystem_hint'),
        error_keywords=q.error_keywords or parsed.get('error_keywords', []),
        stack_frames=q.stack_frames or parsed.get('stack_top_functions'),
        time_window=q.time_window or _parse_time_window(parsed.get('time_window_iso')),
        query_intent=parsed.get('query_intent', 'concept'),
    )
```

### 4.2 成本核算

| 项 | 数值 |
|----|------|
| Query parse 调用 | 1 Navigator message / query |
| Input token | ~200（system + user prompt + NL）|
| Output token | ~150（结构化 JSON）|
| 缓存命中（temperature=0）| 同 NL 重复查询 → M1 PG cache 命中，0 调用 |

### 4.3 失败 fallback

LLM 解析失败时（JSON 解析错 / 超时）：
- 降级到 minimal ParsedQuery：仅含 `natural_language` + `error_keywords` = split(NL by space)
- 记 metric `query_parse_fallback_total`，告警阈值 > 5% 调用率

## 5. 多源召回（Always-fire-all 7 路）

### 5.1 路由全集

每次 retrieve 默认触发全部 7 路（除非 M7 显式 `q.routes` 限制）：

| Route | 后端 | 召回工具 |
|-------|------|---------|
| `code` | CodeGraph | `lookup_symbol` + `search_code` |
| `docs` | CodeGraph | `search_docs` + PageIndex 走查（条件触发）|
| `lkml` | 我方 PG | `lkml_message.body_tsv` + `lkml_thread.summary_tsv` |
| `bug` | 我方 PG | `bug.body_tsv` |
| `syzbot` | 我方 PG | `syzbot_crash.report_tsv` + stack 签名相似 |
| `commit` | 我方 PG + Neo4j | `kernel_commit.body_tsv` + 关系遍历 |
| `cve` | 我方 PG | `cve` 表 + `bug.cve_ids` 反查 |

### 5.2 并行执行

```python
async def recall(parsed: ParsedQuery) -> list[Candidate]:
    """所有 7 路并行召回。"""
    
    tasks = {
        'code': _recall_code(parsed),
        'docs': _recall_docs(parsed),
        'lkml': _recall_lkml(parsed),
        'bug': _recall_bug(parsed),
        'syzbot': _recall_syzbot(parsed),
        'commit': _recall_commit(parsed),
        'cve': _recall_cve(parsed),
    }
    
    # 各路独立超时（30s）
    results = await asyncio.gather(
        *[asyncio.wait_for(t, timeout=30) for t in tasks.values()],
        return_exceptions=True
    )
    
    candidates = []
    for route, r in zip(tasks.keys(), results):
        if isinstance(r, Exception):
            log.warning(f"Route {route} failed: {r}")
            metrics.recall_route_failed.labels(route=route).inc()
            continue
        candidates.extend(r)
        metrics.recall_route_candidates.labels(route=route).observe(len(r))
    
    return candidates
```

### 5.3 各路实现要点

#### code

```python
async def _recall_code(p: ParsedQuery) -> list[Candidate]:
    repos = resolve_repos([p.kernel_version] if p.kernel_version else ['v6.6', 'v5.10'])
    cands = []
    
    # 路 a: 已知函数名 → lookup_symbol 精确
    for func in (p.stack_frames or [])[:3]:
        for repo in repos:
            hits = await codegraph.lookup_symbol(symbol=func, action='definition', repo=repo)
            cands.extend(_wrap_code(hits, route='code/lookup'))
    
    # 路 b: error_keywords → search_code symbol mode
    for kw in (p.error_keywords or [])[:5]:
        hits = await codegraph.search_code(query=kw, repos=repos, mode='symbol', limit=10)
        cands.extend(_wrap_code(hits, route='code/search'))
    
    return cands
```

#### docs

```python
async def _recall_docs(p: ParsedQuery) -> list[Candidate]:
    repos = resolve_repos([p.kernel_version] if p.kernel_version else ['v6.6', 'v5.10'])
    cands = []
    
    # 路 a: BM25
    for kw in (p.error_keywords or [])[:3]:
        for repo in repos:
            hits = await codegraph.search_docs(query=kw, repo=repo, limit=5)
            cands.extend(_wrap_doc(hits, route='docs/bm25'))
    
    # 路 b: PageIndex 走查（条件触发，见 §6.2）
    if _should_trigger_pageindex(p, len(cands)):
        pi_hits = await pageindex_traverse(p, repos)
        cands.extend(_wrap_doc(pi_hits, route='docs/pageindex'))
    
    return cands
```

#### lkml / bug / syzbot

```python
async def _recall_lkml(p: ParsedQuery) -> list[Candidate]:
    # tsvector BM25 + 时间窗 + 子系统过滤
    rows = await pg.query(...)  # 见前
    return [_wrap_lkml(r) for r in rows]
```

#### commit（含 Neo4j 关系）

```python
async def _recall_commit(p: ParsedQuery) -> list[Candidate]:
    cands = []
    
    # 路 a: PG body tsvector
    rows = await pg.query("""
        SELECT hash, subject, commit_date, subsystem,
               ts_rank_cd(body_tsv, plainto_tsquery('english', %s)) AS rank
        FROM kernel_commit
        WHERE body_tsv @@ plainto_tsquery('english', %s)
        ORDER BY rank DESC LIMIT 20
    """, (' '.join(p.error_keywords or []),) * 2)
    cands.extend([_wrap_commit(r, route='commit/bm25') for r in rows])
    
    # 路 b: Neo4j 关系遍历（subsystem 已知时）
    if p.subsystem_hint:
        rows = await neo4j.run("""
            MATCH (s:Subsystem {name: $sub})<-[:BELONGS_TO]-(c:Commit)
            OPTIONAL MATCH (c)-[:FIXES]->(b:Bug)
            WHERE c.date > date('2024-01-01')
            RETURN c, b
            ORDER BY c.date DESC LIMIT 20
        """, sub=p.subsystem_hint)
        cands.extend([_wrap_commit_with_bug(r, route='commit/neo4j') for r in rows])
    
    return cands
```

### 5.4 资源开销（v1.0 估算）

| Route | 平均 latency | 平均候选数 |
|-------|------------|----------|
| code | 200-500ms | 5-15 |
| docs | 200-800ms（含可能的 PageIndex 走查 3-5s）| 3-10 |
| lkml | 50-150ms | 10-30 |
| bug | 50-100ms | 5-20 |
| syzbot | 50-100ms | 0-10 |
| commit | 100-300ms（含 Neo4j）| 10-25 |
| cve | 30-80ms | 0-5 |
| **并行 P95（无 PageIndex）** | **~800ms** | **总 ~50-100** |
| **并行 P95（含 PageIndex）** | **~5s** | **总 ~60-120** |

## 6. PageIndex 走查（两层并存）

### 6.1 低阶 passthrough（M7 自驱）

M5 直接 expose 三个 CodeGraph 工具给 M7：

```python
# M5 公开接口（M7 可直接调，绕过 retrieve()）
async def browse_docs(repo: str | None = None) -> str:
    return await codegraph.browse_docs(repo=repo)

async def browse_doc_sections(repo: str, source_path: str) -> str:
    return await codegraph.browse_doc_sections(repo=repo, source_path=source_path)

async def read_doc_section(repo: str, source_path: str, section: str) -> str:
    return await codegraph.read_doc_section(
        repo=repo, source_path=source_path, section=section
    )
```

M7 可自由组合（与 codesearch "LLM 不承担主检索职责"的哲学一致）。

### 6.2 高阶封装（M5 内置）

```python
async def pageindex_traverse(
    query: ParsedQuery, repos: list[str], max_depth: int = 3
) -> list[Candidate]:
    """LLM 驱动的多步走查，封装到 Always-fire 的 docs 路径里。"""
    
    candidates = []
    
    for repo in repos:
        # Step 1: browse_docs
        toc = await codegraph.browse_docs(repo=repo)
        
        # Step 2: LLM 选 top-3 相关文档
        selected_docs = await _llm_select(
            query=query.natural_language,
            options=toc,
            kind='doc',
            top_n=3
        )
        
        for doc_path in selected_docs:
            sections = await codegraph.browse_doc_sections(repo=repo, source_path=doc_path)
            selected_sections = await _llm_select(
                query=query.natural_language,
                options=sections,
                kind='section',
                top_n=2
            )
            
            for section in selected_sections:
                content = await codegraph.read_doc_section(
                    repo=repo, source_path=doc_path, section=section
                )
                candidates.append(Candidate(
                    source='doc',
                    ref=f"{repo}:{doc_path}#{section}",
                    snippet=content[:500],
                    score=0.8,
                    route='docs/pageindex',
                    metadata={'depth': 3}
                ))
    
    return candidates
```

### 6.3 触发条件

```python
def _should_trigger_pageindex(p: ParsedQuery, current_doc_candidates: int) -> bool:
    """避免每次都跑 PageIndex（成本高）。"""
    
    # 概念问题（"什么是 X"）一定触发
    if p.query_intent == 'concept':
        return True
    
    # 当前 BM25 候选不足
    if current_doc_candidates < 3:
        return True
    
    # 配额吃紧时不触发
    if rate_budget.remaining_messages() < 5:
        return False
    
    return False
```

### 6.4 PageIndex 走查的 LLM 成本

| 步骤 | LLM 调用 |
|------|--------|
| select_docs | 1 message |
| select_sections（每个 doc）| 3 messages（top-3 docs）|
| **总（每次走查）** | **~4 messages** |

## 7. LLM 重排

### 7.1 触发条件

```python
def should_rerank(candidates: list[Candidate], query: ParsedQuery) -> bool:
    if not query.enable_rerank:
        return False
    if len(candidates) <= query.top_k:
        return False
    if rate_budget.remaining_messages() < 3:
        return False  # 配额紧时不 rerank
    return len(candidates) > 10
```

### 7.2 实现

```python
RERANK_PROMPT = """\
You are a Linux kernel diagnosis expert. Rank the top {top_k} most relevant
candidates for this query, with brief justification.

Query: {nl}
Kernel version: {ver}
Fault domain: {fault}

Candidates:
{candidates}

Return JSON: {{"top_k": [{{"rank": 1, "ref": "...", "reason": "..."}}, ...]}}
"""

async def llm_rerank(
    candidates: list[Candidate], query: ParsedQuery, top_k: int
) -> list[Evidence]:
    # 限制 input 大小（避免长上下文费用）
    formatted = _format_candidates(candidates[:30])
    
    response = await navigator_llm.chat(
        messages=[
            {"role": "system", "content": "Kernel diagnosis expert."},
            {"role": "user", "content": RERANK_PROMPT.format(
                top_k=top_k, nl=query.natural_language,
                ver=query.kernel_version or 'multiple',
                fault=query.fault_domain or 'unknown',
                candidates=formatted
            )}
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    
    parsed = json.loads(response.content)
    return [_to_evidence(candidates, item) for item in parsed['top_k']]
```

## 8. 缓存策略

**不引入独立 evidence 缓存**。命中靠：
- M1 PG `llm_response_cache`（query_parse + pageindex select + rerank 三个 LLM 调用都缓存）
- CodeGraph 自身缓存层
- 同 NL query 重复时，三个 LLM 调用全命中 PG cache，几乎零成本

如 v1.2 评测显示需要，再加 M5 evidence-level cache。

## 9. Pro 5h 配额预算重算 ★

基于 D1（全 LLM 解析）+ D2（always-fire-all）：

### 9.1 单次 retrieve 的 LLM message 消耗

| 步骤 | Message 数 | 备注 |
|------|----------|------|
| query parse | 1 | 必发 |
| PageIndex select_docs | 1 | 仅条件触发（concept 类问题或 BM25 不足）|
| PageIndex select_sections × 3 | 3 | 同上 |
| LLM rerank | 1 | 候选 > 10 时触发 |
| **平均（无 PageIndex）** | **~2** | 大多数 incident 类查询 |
| **平均（有 PageIndex）** | **~6** | concept 类查询或 BM25 召回不足 |

### 9.2 Pro 配额下吞吐

| 场景 | 每查询 messages | 5h 窗口可跑 |
|------|---------------|-----------|
| 无 PageIndex（多数 incident 类）| 2 | **~22 queries** |
| 有 PageIndex（concept 类）| 6 | **~7 queries** |
| 混合（评测 30 例假设 50% concept）| ~4 平均 | **~11 queries / 5h** |

### 9.3 30 例评测时间表

| 场景 | 总 messages | 跨 5h 窗口数 | 真实耗时 |
|------|-----------|-----------|--------|
| 全 incident（最理想）| 60 | 2 | 2 × 5h = **10h** |
| 混合（50% concept）| 120 | 3 | 3 × 5h = **15h（2 天）**|
| 全 concept（最坏）| 180 | 4 | 4 × 5h = **20h（3 天）**|

★ **结论**：30 例评测真实耗时取决于 concept-class 占比，预期 **2-3 天**（vs 之前估计的 7 天偏松，但仍未撞硬墙）。

### 9.4 缓解策略

- 同 NL 重复评测：M1 PG cache 命中，零消耗
- 评测脚本可选 `--bypass-cache` 测真实生产 latency
- 配额紧时禁用 PageIndex 走查（保 query parse + rerank 必发）
- 长期：v1.3 切到本地 vLLM 后配额无限制

## 10. Fallback 链

| 失败场景 | Fallback |
|---------|---------|
| CodeGraph HTTP 不可达 | healthcheck 重连；超 1 分钟进入"无代码/无文档模式"，仅返 LKML/Bug/syzbot/commit/cve |
| CodeGraph 某 repo 缺索引 | 自动改用其他 repo（v6.6 缺则只查 v5.10）|
| LKML BM25 返回空 | 降级 fuzzy match（pg_trgm similarity > 0.5）|
| Neo4j 不可达 | 切到 PG link_* 表（recursive CTE，慢但可用）|
| Navigator LLM rate-limited | query parse fallback（split keywords）；PageIndex 跳过；rerank 跳过 |
| PageIndex 走查超时（> 10s）| 取已收集到的 candidates，warning 标记 |
| 某路超时（> 30s）| 该路返回空，其他路继续 |

## 11. 文件结构

```
retrieval/
├── __init__.py
├── api.py                           # retrieve(query) -> list[Evidence]
├── query_parser.py                  # 全 LLM 解析
├── recall/
│   ├── __init__.py
│   ├── code.py                      # CodeGraph search_code/lookup_symbol
│   ├── docs.py                      # CodeGraph search_docs + PageIndex
│   ├── lkml.py                      # PG tsvector
│   ├── bug.py                       # PG tsvector
│   ├── syzbot.py                    # PG + stack 签名
│   ├── commit.py                    # PG + Neo4j
│   └── cve.py                       # PG
├── pageindex.py                     # 高阶走查封装
├── rerank.py                        # LLM listwise rerank
├── normalizer.py                    # Candidate → Evidence
├── budget.py                        # Pro 5h 配额追踪
└── tests/
```

## 12. 测试策略

| 层 | 测什么 |
|----|--------|
| 单元 | query_parse JSON schema、Candidate → Evidence、ref 格式 |
| 集成 | mock CodeGraph + 真实 PG，跑 30 例查询，校验 Recall@10 |
| 性能 | 多源并行召回 P95 < 1s（无 PageIndex），含 PageIndex P95 < 5s |
| Pro 配额 | 跑 30 例后核对 `llm_subscription_messages_used` 是否符合预算 |
| 评测 | 30 例公开案例 Recall@10 ≥ 70%（v1.0 验收）|

## 13. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| 每查询 LLM 解析 + rerank 配额开销大 | `llm_rate_budget_remaining` | 评测分多日；budget < 3 时禁 PageIndex/rerank |
| 多源召回延迟 P95 高 | 各路 latency | 异步并行 + 单路超时 30s |
| query parse LLM 输出非法 JSON | parse fallback 率 | response_format=json_object + fallback |
| PageIndex 走查质量差 | 评测命中率 | prompt 微调 + few-shot |
| CodeGraph 工具行为变化 | smoke test 失败 | 锁版本 + 跨项目协调 |
| Always-fire 7 路浪费资源 | route candidate count metrics | v1.2 后评估是否切 hybrid（[ADR-013](../adr/ADR-013-m5-retrieval-strategy.md) 留 review 触发条件）|

## 14. v1.0 → v1.3 演进

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | 7 路 always-fire + 全 LLM parse + PageIndex 两层 + rerank 触发条件 |
| v1.1 | PageIndex 走查 prompt 微调；评测反馈调整 always-fire 是否切 hybrid |
| v1.2 | 评测 30 例完成；如配额成为瓶颈，引入 selective firing |
| v1.3 | 切本地 vLLM 后配额无限，always-fire 完全可行；评估 query rewrite LLM 微调 |

## 15. 与外部组件的契约

- **M1 Provider**：query_parse / PageIndex select / rerank 都走 Navigator backend；遵守 Pro 5h budget
- **M2 存储**：读 lkml_message / bug / syzbot_crash / kernel_commit / cve / link_* 表
- **M4 Cross-Graph Linker**：通过 M4 提供的 CodeGraph HTTP client；通过 Neo4j 关系查询使用 link 数据
- **M7 诊断 Agent**：调 `retrieve(query)` 取证据；同时也可绕过用 M5 expose 的低阶 passthrough 工具

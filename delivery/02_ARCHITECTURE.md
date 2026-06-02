# 02 · Architecture

> 系统总图 + 数据流 + 模块边界。配合 `10_MODULE_SPECS/M*.md` 看具体契约。

---

## 1. 全局数据流

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          外部数据源（M3 ingest）                          │
│                                                                          │
│   lore.kernel.org   bugzilla   gitee     atomgit    syzbot    NVD        │
│   (LKML mail)       REST       issue     issue      HTML      REST       │
│   OLK kernel git    linux-stable git                                     │
└────────────┬─────────────┬─────────────┬─────────────┬───────────────────┘
             │             │             │             │
             ▼             ▼             ▼             ▼
     ┌─────────────────────────────────────────────────────────────────┐
     │              M2 · PostgreSQL 16（主存储，16 张表）                │
     │                                                                  │
     │   内容表（5）      关系表（7）        系统表（4）                  │
     │   ─────────       ─────────         ────────                    │
     │   kernel_commit   link_commit_*     llm_response_cache          │
     │   lkml_message    link_syzbot_*     ingest_runs                 │
     │   bug             (commit↔bug)      dmesg_event                 │
     │   cve             (commit↔message)  eval_cases / eval_results   │
     │   syzbot_crash    (commit↔cve)                                  │
     │                   (commit↔symbol)                                │
     │                   (commit↔fixes)                                 │
     │                   (commit↔revert)                                │
     │                   (syzbot↔commit)                                │
     │                                                                  │
     │   + body_tsv GIN 索引 + short_hash B-tree 索引                   │
     └────────────────┬─────────────────────────┬─────────────────────┘
                      │                         │
                      │ (M4 派生 7 张 link 表)   │ (M5 查询)
                      │                         │
                      ▼                         ▼
              ┌──────────────┐         ┌────────────────────────┐
              │  Neo4j 拓扑视图│         │  M5 · 7-route BM25     │
              │  (周度 rebuild)│         │  并行检索 + LLM rerank  │
              │  (只读、可选)  │         └───────────┬────────────┘
              └──────────────┘                     │
                                                    │ (evidence pool)
                                                    ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │                  M7 · Diagnosis Agent（LangGraph 拓扑）                │
   │                                                                       │
   │   parse_input → extract_events → detect_taint_and_hw_signals →        │
   │   classify_fault_and_route → retrieve(M5) →                           │
   │   react_investigation(ReAct loop) → bind_claims → generate_report     │
   │                                                                       │
   │   ReAct loop 调用 32 个工具（M5 retrieval 函数 + M6 parser + 内部 KG  │
   │   查询函数），最多 15 iter / 250K token / REPEAT_LIMIT=3             │
   └──────────────────────────────┬───────────────────────────────────────┘
                                  │
                                  ▼
              ┌─────────────────────────────────────┐
              │ M9 · Web UI (FastAPI + SSE)         │
              │ stream_diagnose() → 5 阶段实时滚动   │
              │ 中/英双语 + SRE 风格 markdown 报告   │
              └─────────────────────────────────────┘
                                  ▲
                                  │
                          ┌──────────────────┐
                          │ M1 · LLM Provider│ ← 所有 LLM 调用经此抽象
                          │ (3 backend)      │   chat 角色 + navigator 角色
                          └──────────────────┘
```

## 2. 9 个模块的角色

| 模块 | 角色 | 输入 | 输出 |
|---|---|---|---|
| **M1** LLM Provider | 抽象 LLM 调用 + 角色 + 限流 + cache | ChatRequest | ChatResponse |
| **M2** Storage Schema | PG 16 表 + Neo4j 视图 | DDL | engine 单例 |
| **M3** Ingestion | 6 个数据源 → PG 表 | HTTP / git | 行 + ingest_runs |
| **M4** Cross-Graph Linker | 5 内容表 → 7 link 表 | 内容表 | 关系表 + neo4j rebuild |
| **M5** Retrieval | 关键词 → 证据池 | RetrievalQuery | RetrievalResult |
| **M6** MCP Host Tools | raw dmesg/sosreport → 结构化事件 | text/path | KernelEvent[] / SystemSummary |
| **M7** Diagnosis Agent | 故障描述 → 诊断报告 | raw_input | state (含 report_md/json) |
| **M8** Evaluation | case set → KPI summary | cases.json | summary.json + per-case .json |
| **M9** Web UI | M7 包装成产品交付 | HTTP request | SSE stream |

## 3. 关键架构决策（详见 `03_ADR_collection.md`）

| 维度 | 选择 | ADR |
|---|---|---|
| 索引引擎 | PostgreSQL `tsvector` + GIN | ADR-006 |
| 语义检索 | BM25 + LLM rerank（**不**用 vector embedding） | ADR-001 |
| LLM 抽象 | OpenAI Chat Completion schema 兼容 | ADR-003 |
| Agent 框架 | LangGraph + 自实现 ReAct loop（**不**用 `create_react_agent`） | ADR-019 |
| Commit 源 | OLK kernel.git（**不**用 kernel.org mainline 作主源） | ADR-018 |
| BM25 召回 | 7 路 always-fire-all（**不**做选择性路由） | ADR-013 |
| Bug 源 | kernel.org Bugzilla + gitee + atomgit issues（**不**含 RedHat） | ADR-011 + ADR-022 |
| 硬件路由 | hardware 信号优先于内核分类（ADR-023） | ADR-023 |
| 不足判定 | 显式 `<insufficient_evidence>` 出口（不强行编造答案） | ADR-024 |
| KG 数据策略 | 离线优先；KG silent 时 fallback 到 first-principles | ADR-025 |

## 4. 三个核心信号路径

### 4.1 用户视角："dmesg 进来 → 报告出去"

```
raw_input (dmesg)
    │
    ▼
parse_input (M7 §①)
    │
    ▼
extract_events (M7 §②)
    │ → 把 OOM/panic/oops/... 抽出来
    ▼
classify_fault_and_route (M7 §④)
    │ → fault_kind + diagnostic_route + sop_name
    ▼
retrieve (M5)
    │ → 7 路 BM25 召回 evidence pool
    ▼
react_investigation (M7 §⑥)
    │ → ReAct loop: Phase 0-5 调研，调 0-15 次工具
    │ → LLM emit <final_answer>...</final_answer>
    ▼
bind_claims (M7 §⑦)
    │ → 把 final_answer 拆 claim，校 evidence_refs 真实性
    ▼
generate_report (M7 §⑧)
    │ → render_md + render_json
    ▼
报告（含中/英 SRE-style markdown + 完整 JSON）
```

### 4.2 KG 维护视角："新数据来了 → 关系更新"

```
ingester (M3) tick (cron weekly)
    │
    ▼
content tables (kernel_commit / lkml_message / bug / cve / syzbot_crash)
    │ ← UPSERT；ingest_runs 写 success
    ▼
run_linker (M4)
    │ → trailer 扫 commit → link_commit_bug / link_commit_message
    │ → upstream stub 补齐
    │ → link_nvd_commits / link_syzbot_commits (独立 entrypoint)
    │ → link_commit_symbol / fixes / revert (独立 backfill)
    ▼
neo4j_rebuild (M4) — optional
    │ → 全量 dump 到 Neo4j 拓扑视图
    ▼
新的 evidence 可被 M5 检索到
```

### 4.3 LLM 调用视角："Agent 想调用工具时发生了什么"

```
agent code calls provider.chat(ChatRequest)
    │
    ▼
M1 BaseProvider
    │
    ├─① endpoint-level sliding window limiter (M1 §3)
    │
    ├─② PG cache lookup (key = sha256(model + messages))
    │  └ hit → 直接返回 cached ChatResponse
    │
    ├─③ backend dispatch (claude_code / openai_compat / vllm)
    │
    ├─④ retry on transient errors (429, 5xx, timeout)
    │
    ├─⑤ PG cache write
    │
    └─⑥ JSONL audit log → data/logs/<ts>.jsonl
    │
    ▼
ChatResponse → 调用方
```

## 5. 进程拓扑

| 进程 | 用途 | 是否常驻 |
|---|---|---|
| `uvicorn web.server:app` | M9 web UI + M7 agent + M1/M5 | 常驻（生产部署） |
| `codegraph` MCP server | M5 code / docs 路 + M7 lookup_symbol / get_function_source 等 | 常驻（独立 daemon，:8080） |
| `postgres` | M2 主存储 | 常驻 |
| `neo4j` (可选) | M4 拓扑视图 | 可选常驻 |
| `python -m ingest.*` | M3 ingest 增量 | cron（weekly） |
| `python -m graph.linker` | M4 link 表派生 | cron（紧跟 ingester） |
| `python -m eval.runner_v2` | M8 评测 | 手动 / CI |

详见 `70_OPERATIONAL.md`。

## 6. 性能特征

| 操作 | 典型耗时 | 备注 |
|---|---|---|
| 7-route BM25 retrieve | 500 ms - 1.5 s | 7 路并行；最慢路是 CodeGraph |
| 单次 LLM 调用（DeepSeek） | 3-15 s | 中文 prompt 慢一些 |
| 单次 diagnose() 总耗时 | 60-300 s | 取决于 fault 类型 |
| 一轮完整 ingest（增量） | 5-15 min | weekly |
| 一轮完整 ingest（首次全量） | 4-12 h | 主要是 LKML 大列表 |
| 一轮 link 派生 | 30 s - 2 min | NVD + syzbot 各自独立 |
| 22 case 全量 eval | 30-50 min | 取决于 concurrency |

## 7. 依赖图

```
                  M9 Web UI
                     │
                     ▼
                  M7 Agent ─────────────────┐
                     │                       │
        ┌───────┬────┴────┬──────┐          │
        ▼       ▼         ▼      ▼          ▼
       M1     M5 Retrieval  M6   M4         M8 Eval
        │       │          MCP  Linker        │
        │       │                │             │
        │       ▼                ▼             │
        │      M2 ───────────────┴─────────────┘
        │      Storage
        │       │
        │       ▼
        ▼      M3 Ingestion
       LLM      │
       APIs    External APIs
                (lore / bugzilla / NVD / git ...)
```

**关键依赖规则**：
- M5/M6/M7/M8 都依赖 M1 + M2
- M7 ReAct loop 是中枢，调用 M5 + M6 + 自身工具
- M3 → M4 严格顺序（M4 必须在 M3 后跑）
- M9 是 M7 的薄包装层

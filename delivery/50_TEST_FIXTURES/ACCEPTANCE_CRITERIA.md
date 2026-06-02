# 50 · Acceptance Criteria

> 重建版本是否"达标"的验收标准。每个模块单独一节，含**输入** / **预期** / **怎么跑** / **通过门槛**。
>
> `run_acceptance.py` 实现了 § A-G 中可自动化的项目；§ H 的端到端跑实际诊断需要 LLM + DB，本地手动跑。

---

## A · M2 Storage Schema

**输入**：跑完 migration 链（参考 `04_DATA_MODEL.md` 全部 DDL）。

**预期**：
1. 20 张表全部存在
2. 5 张内容表的 `body_tsv` 列都是 `GENERATED ALWAYS ... STORED`
3. 5 张内容表都有 GIN(body_tsv) 索引
4. `kernel_commit.short_hash` 是 generated stored 列 + B-tree 索引
5. 7 张关系表都有复合 unique constraint

**跑**：

```bash
psql $DSN -f delivery/50_TEST_FIXTURES/sql_check_schema.sql
```

（脚本见 `run_acceptance.py --module M2` 嵌入的 SQL）

**通过**：所有 5 段 SQL 都返回期望计数。

---

## B · M1 LLM Provider

**输入**：1 个 mock provider（不调真实 LLM，模拟 ChatRequest → ChatResponse）。

**预期**：
1. `chat()` 默认 `temperature=0.0`
2. `chat()` 默认 `max_tokens` 上限 ≤ 4096（不会变成无界）
3. httpx 客户端用 `trust_env=False`（不走主机 proxy）
4. OpenAI SDK `max_retries=0`（应用层管重试）
5. 同 ChatRequest 第二次调用应命中 PG cache（或等价缓存层）

**跑**：

```bash
python delivery/50_TEST_FIXTURES/run_acceptance.py --module M1
```

**通过**：5 个 assertion 全 PASS。

---

## C · M3 Ingestion（要求接 PG + 外网）

**输入**：本地 PG empty + 凭据（gitee/atomgit token、DeepSeek key 可选）。

**预期**：

| Ingester | 期望最小行数（首次 30 天 lookback） |
|---|---|
| NVD | ≥ 100 |
| Bugzilla | ≥ 50 |
| syzbot | ≥ 30 |
| lkml linux-mm | ≥ 1000 |
| gitee | ≥ 100 |
| kernel_commit OLK-6.6 | ≥ 5000 |

幂等：同 ingester 立即重跑 `rows_inserted = 0` + `ingest_runs` 多 1 行 status='success'。

**跑**（按需）：

```bash
python -m ingest.nvd incremental
python -m ingest.bugzilla incremental
python -m ingest.syzbot incremental
# 或: ./scripts/weekly_sync.sh
```

**通过**：每个 ingester 至少能 ingest 出非零行；`ingest_runs` 表追加 success 行；checkpoint JSONB 已保存。

---

## D · M4 Cross-Graph Linker（要求 M3 已完成）

**输入**：M3 ingest 完成后的 PG。

**预期**：

```python
from graph.linker import run_linker, link_nvd_commits, link_syzbot_commits

report = run_linker(engine)
nvd_links = link_nvd_commits(engine)
syzbot_links = link_syzbot_commits(engine)
```

| 关系表 | 期望最小行数 |
|---|---|
| `link_commit_bug` | ≥ 1000 |
| `link_commit_message` | ≥ 5000 |
| `link_commit_cve` | ≥ 1000 |
| `link_syzbot_commit` | ≥ 500 |

**通过**：4 张关系表行数 ≥ 上述门槛 + Neo4j 对账误差 ≤ 5 行（如启用 Neo4j）。

---

## E · M5 Retrieval

**输入**：

```python
from retrieval.schema import RetrievalQuery, RouteTag
from retrieval.engine import retrieve

query = RetrievalQuery(
    raw_question="OOM kill in memcg /app/java",
    kernel_version="OLK-6.6",
    keywords=["oom_kill_process", "mem_cgroup_out_of_memory", "CONSTRAINT_MEMCG"],
    routes=[RouteTag(r) for r in ("commit","lkml","bug","syzbot","cve","code","docs")],
    limit_per_route=10,
)
result = retrieve(query)
```

**预期**：
1. `result.items` 至少 8 条
2. `commit` 路至少 1 条非空 commit_hash
3. `lkml` 路至少 3 条
4. **不**包含任何 `subject LIKE '[stub upstream%'` 行（stub filter 必须生效）
5. 如果 `cve_ids=['CVE-2024-26926']` 显式传入，CVE 路第 1 条 score = 1.0 且 `cve_id` 精确匹配

**跑**：

```bash
python delivery/50_TEST_FIXTURES/run_acceptance.py --module M5
```

**通过**：5 项 assertion 全 PASS。

---

## F · M6 dmesg/sosreport parser

**输入**：见 `M6_host_mcp_tools.md § 7` 三段测试样本。

**预期**：
1. 3 种 OOM 头都识别为 `EventKind.oom`
2. `extract_call_trace` 在带 `[timestamp]` 前缀的栈输入下抽出 ≥ 5 个 frame
3. `parse_sosreport(<sample.tar.xz>)` 返回 SystemSummary 含 hostname / kernel_version / olk_version_tag

**跑**：

```bash
python delivery/50_TEST_FIXTURES/run_acceptance.py --module M6
```

**通过**：所有 assertion PASS。

---

## G · 30 Tool Contracts

**预期**：
1. 注册表 `build_registry()` 返回 32 个工具
2. 每个工具的 `parameters` 是合法 JSON Schema
3. `route=kernel` 路由暴露 26 个工具
4. `route=hardware` 路由暴露 6 个 `_HW` 工具（4 hw_*）+ `_ALL` 公用工具
5. 每个工具支持 `**kwargs` 容错（额外 keys 不抛错）

**跑**：

```bash
python delivery/50_TEST_FIXTURES/run_acceptance.py --module tool_contracts
```

**通过**：5 项 assertion PASS。

---

## H · End-to-end 诊断（需 LLM + 完整 DB）

**输入**：3 种 case file:

| 用例 | 用途 | 通过门槛 |
|---|---|---|
| `cases_smoke.json` | 1 个产品级 smoke | verdict ∈ {diagnosed, insufficient_evidence}，report_md ≥ 1000 字符 |
| `cases_coverage.json` | 1 个 KG 全覆盖 | route=kernel 工具调用 ≥ 12 个，含 Phase 0-4 全部 |
| `cases_v2.json` (22 cases) | 完整回归 | avg_phase_coverage ≥ 0.80, route_accuracy ≥ 0.85 |

**跑**：

```bash
# smoke 单条
python -m eval.runner_v2 \
    --dataset delivery/50_TEST_FIXTURES/cases_smoke.json \
    --output /tmp/smoke_acceptance/ \
    --no-judge

# 完整回归（耗时 30-50 min，需 LLM API）
python -m eval.runner_v2 \
    --dataset delivery/50_TEST_FIXTURES/cases_v2.json \
    --output /tmp/full_acceptance/ \
    --concurrency 2
```

**通过**：

- smoke：`summary.json::verdicts.diagnosed + insufficient_evidence == 1`，无 error
- 完整：`summary.json::process_compliance.avg_phase_coverage ≥ 0.80` 且 `route_accuracy ≥ 0.85`

---

## I · M7 Behavioral Contracts（agent 思考行为）

**输入**：跑 § H 任一 case 的 `react_final_answer` 文本。

**预期**：
1. 答案含 `### 候选假设` / `### Candidate hypotheses`（按 lang）—— 强制假设枚举
2. 答案含 `### 自我批驳` / `### Critique`
3. 答案含 `### 证据来源` / `### Evidence trace`
4. 答案中引用的所有 `[0-9a-f]{12,40}` SHA 都能在 `react_tool_trace` 任一 entry 的 `result_preview` 找到
5. 答案不含 `DSML` / `<tool_calls>` / `<invoke ` 等 LLM 幻觉 token

**跑**：

```bash
python delivery/50_TEST_FIXTURES/run_acceptance.py \
    --module behavioral_contracts \
    --result-json /tmp/smoke_acceptance/smoke-product-001.json
```

**通过**：5 项 assertion PASS。

---

## J · M9 Web UI 手测 checklist

启动：

```bash
python -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

7 项 checklist：

1. ✓ 打开 `http://127.0.0.1:8000/` — 默认中文界面，右上角 `EN` 切换按钮
2. ✓ 点 "填入示例" — textarea 加载 cases_smoke 内容
3. ✓ 点 "诊断" — 5 阶段卡片依次滚动出现
4. ✓ 切换语言 `EN` — 整页重新渲染英文（包括已显示的 stage）
5. ✓ Stage 3 深入调研区显示每步 LLM 调用 + 工具调用，spinner 在每步 header
6. ✓ Stage 4 核对证据显示每条 claim + 引用证据 + 判定原因
7. ✓ Stage 5 报告含双语 SRE 结构（## 根本原因 / ## 修复建议 / ## 置信度）

**通过**：7 项全 √。

---

## 总验收

重建版本 SHIP 的最低条件：

| 等级 | 条件 |
|---|---|
| **最低** | § A + § B + § E + § F + § G + § H smoke 全 PASS |
| **正式** | 上面所有 + § C + § D + § H 完整回归（22 case）≥ 0.80 phase_coverage |
| **完美** | 上面所有 + § I + § J 全 PASS |

> AI 重建时建议自上而下：先做 M2/M1 → 再 M3/M4 数据填充 → 再 M5/M6 → 最后 M7/M9 端到端。每完成一级跑对应 acceptance。

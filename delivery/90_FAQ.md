# 90 · FAQ

> 精选自原仓库 `docs/FAQ.md`，按"重建者最常问"重新排序。

---

## Q1 · 我能不能不用 PostgreSQL 改用其它 DB？

可以，但需要重做：
- 全文索引（PG `tsvector` + GIN）→ 你的等价物（MySQL FULLTEXT / SQLite FTS5 / Elasticsearch / ...）
- jsonb 列 → JSON 等价
- generated stored 列 → trigger 维护

**不建议**：换 NoSQL（MongoDB / DynamoDB）—— 7 张 link 表的 join 频繁，关系型更顺手。

**建议**：MySQL 8 + FULLTEXT 是次优方案，但 ts_rank 行为不同要重新调；ClickHouse 不适合（join 性能差）；DuckDB 单机够用但不适合长驻 daemon。

---

## Q2 · 不用 LangGraph 行吗？

可以。LangGraph 在本项目里用到的只是：
- 9 节点串行 DAG
- state dict 透传（不是 TypedDict，见 `60_GOTCHAS.md` §5.1）
- 可选的 PG checkpointer（`langgraph.checkpoint.postgres`）

等价物：
- 手写 `state = step_N(state)` 链
- Temporal / Argo Workflows（重）
- Prefect（中等重）

ReAct loop 是**自己实现**的（M7 § 5），不依赖 LangChain。所以即使不用 LangGraph，ReAct 部分照搬即可。

---

## Q3 · LLM 必须用 DeepSeek 吗？

不。M1 抽象支持任何 **OpenAI Chat Completions schema 兼容**的 vendor：
- DeepSeek 官方（当前默认；最便宜）
- OpenRouter（聚合）
- OpenAI 官方
- Anthropic（通过 `anthropic_provider` 或经 OpenAI-compat 网关）
- 自部署 vLLM / ollama / TGI

**注意**：
- `tool_choice="none"` 时不同 vendor 行为有差异（DeepSeek 会输出 DSML 幻觉，OpenAI / Claude 不会）—— 你的 hallucination retry 逻辑要保留
- function calling 支持参差不齐（vLLM 老版本不支持）—— 测时关注 `resp.tool_calls`

---

## Q4 · 我能直接用 Claude / Cursor 重建吗？

可以。具体路径：

1. 把 `delivery/` 整目录拖给 Claude / Cursor / Aider
2. 给提示："请按 `README.md` 的'重建策略'章节，从 M2 开始实现，每完成一个模块跑 `50_TEST_FIXTURES/run_acceptance.py --module M<N>` 验证"
3. 让 AI 自底向上写代码
4. 每完成一级跑 acceptance，FAIL 时贴 error 给 AI 修

**预估时间**：用 Claude Sonnet 4.6 / Opus 4.7，全栈走完 8-15 小时（实际 token 不到 5M）。

**关键提醒**：
- 让 AI **先读 `60_GOTCHAS.md` 全文**（特别是 PG / API quirks / DSML 三章）
- prompt 文件**整个拷贝**，不要让 AI 重写
- case JSON 不要让 AI 改

---

## Q5 · 为什么不做跨 distro 支持？

详细论证：

**前提**：跨 distro 意味着 Ubuntu kernel / RHEL kernel / Android kernel 也都 ingest。

**实测**：以 mainline 上游 LTS 6.6 为基准，Ubuntu 24.04 / openEuler 24.03 SP / RHEL 9.4 / AOSP 7.x **有 70%+ commit 重叠**——都从同一个上游 backport。

**问题**：
1. 重复存储 70% commits（4× 数据量增长，PG 行数从 1.5M → 6M+）
2. 跨 distro `affected_versions` 字段维护爆炸（OLK-6.6 / Ubuntu-24.04 / RHEL-9.4 / ... 6+ 组合）
3. 每加一个 distro 增加 1-2 个 ingester，limits 限流共享出问题
4. 多 distro 的 inclusion_type 含义不同（Ubuntu 用 `cve-`、RHEL 用 `RHEL-`、OLK 用 `mainline/stable`）

**vs 收益**：上游 commit 在我们 DB 已通过 OLK + linux-stable 拿到了；distro 特有 patch 占比 < 10%。

**结论**：拒绝跨 distro。这是 ADR 之外的额外决策。

---

## Q6 · 答案不准确怎么办？

先看 KPI：

| 现象 | 看哪 |
|---|---|
| process compliance 低（< 0.7） | agent 没按 Phase 0-6 调研；查 ReAct trace 看跳了哪 phase |
| process compliance OK 但 recall@10 低 | KG 数据缺；M3 ingest 覆盖不够 |
| 答案在 evidence 池里**有**正确 commit，但 agent 没选 | LLM rerank / bind_claims 阈值太严；调 `_OVERLAP_THRESHOLD` |
| 答案有正确 SHA 但描述错 | LLM intrinsic knowledge 漂移；多跑几次取多数（N-fold） |
| 直接 `<insufficient_evidence>` 但应该有答案 | `force_finalize` 后没救回；优化 fallback path |

**记住**：M8 主指标是流程合规度（确定性），答案对错是副指标（LLM-noisy）。**不要为提高副指标改 prompt**——历史教训见 ADR-process-compliance。

---

## Q7 · 数据规模能扩到多少？

当前 1.5M commits / 115K LKML / 8K bugs / 16K CVEs。

**PG 单机理论上限**：
- `kernel_commit` 10M+ 行没问题（GIN 索引 ~5 GB，查询 < 100ms）
- `lkml_message` 500K+ 行也没问题
- `link_*` 表 10M 边 OK

**实际瓶颈**：
- LLM rerank token 成本（每次 ~3K-15K token）
- CodeGraph 索引体积（OLK kernel 全索引 ~3 GB）
- Neo4j rebuild 时间（线性 with link 表行数，10M 边大约 30 min）

**伸缩策略**：
- > 5M commits：PG 加 read replica，retrieval 走 replica
- > 100 QPS：web UI 横向扩；LLM 调用做异步队列
- > 单机 PG 容量：partition `kernel_commit` by year，但**还没真做过**

---

## Q8 · 单机部署最少要多少资源？

| 资源 | 单机 dev | 生产 |
|---|---|---|
| RAM | 8 GB | 32 GB（PG buffer + CodeGraph 索引）|
| 磁盘 | 30 GB | 100 GB（含 OLK git clone + PG 数据）|
| CPU | 4 核 | 8+ 核 |
| GPU | **不需要** | 不需要（无 embedding / 无 local LLM） |
| 网络 | 出口能到 DeepSeek API | 同 |

完整 ingest 跑过一次后，**纯诊断**单实例只需要 4 GB / 4 核就够。GPU 不必要。

---

## Q9 · 怎么验证我重建的系统等价于原版？

按 `50_TEST_FIXTURES/ACCEPTANCE_CRITERIA.md` 跑 § A-J：

```bash
# 完整 22 case 评测（耗时 30-50 min + LLM 成本 ~$1-3）
python -m eval.runner_v2 \
    --dataset delivery/50_TEST_FIXTURES/cases_v2.json \
    --output /tmp/full_validation/

# 对比预期门槛：
#   avg_phase_coverage ≥ 0.80
#   route_accuracy ≥ 0.85
#   fault_kind_accuracy ≥ 0.70
```

如果你的版本能达到上述门槛 + § A/B/E/F/G/I 的单元 assertion 全 PASS，**等价性达标**。

---

## Q10 · 中文 / 英文用户分别看到什么？

- UI 静态文字（标题 / 按钮 / 字段 label）按 `lang` 切换
- Agent 答案按 `lang` 用对应 prompt 文件（kernel.md / kernel.zh.md）
- 报告 markdown 头部由 `render_md(state, lang)` 生成中英标题
- **代码标识符**（函数名 / commit hash / CVE-ID / 错误码 / `memcg` / `OOM` / `KASAN` 等）始终**保留英文原文**

详见 `40_BEHAVIORAL_CONTRACTS.md`。

---

## Q11 · 我怎么自己加一个新 LLM 工具？

按 `30_TOOL_CONTRACTS.md` 模板：

1. 写一个函数 `def _my_tool(*, x, y, **_) -> str`
2. 包装成 `Tool` 对象：
   ```python
   MY_TOOL = Tool(
       name="my_tool",
       description="What this does + when to use it.",
       parameters={"type": "object", "properties": {"x": {"type": "string"}, ...},
                   "required": ["x"]},
       fn=_my_tool,
       routes=_K,                    # 决定哪些路由暴露
   )
   ```
3. 加到对应分类列表（如 `ALL_RETRIEVAL_TOOLS`）
4. 写一个 acceptance：在 `run_acceptance.py` 加 case
5. ReAct agent 自动能调（system_prompt 不变，工具描述由 schema 自动渲染）

---

## Q12 · 怎么跨服务调用本系统？

当前 web UI 是 SSE；要 REST/JSON-RPC 调用：

写一个 thin client 调 `/api/diagnose-stream`，收齐所有事件后取 `event.type='report'` 的 payload：

```python
import requests
import json

def diagnose_via_http(raw_input: str, lang: str = "zh") -> dict:
    resp = requests.post(
        "http://localhost:8000/api/diagnose-stream",
        json={"raw_input": raw_input, "lang": lang},
        stream=True, timeout=600,
    )
    events = []
    for line in resp.iter_lines():
        if not line or not line.startswith(b"data:"):
            continue
        ev = json.loads(line[len(b"data:"):].strip())
        events.append(ev)
        if ev["type"] == "report":
            return ev          # 完整 report_md 在 ev["report_md"]
    return None
```

如果要 OpenAPI / gRPC，自己包一层。当前**故意不内置**——SSE 已足够。

---

## Q13 · 系统出错怎么定位？

日志位置见 `70_OPERATIONAL.md § 7`。常见错误：

| 错误 | 定位 |
|---|---|
| `connection refused localhost:5432` | PG 没起 |
| `connection refused localhost:8080` | CodeGraph MCP 没起 |
| `psycopg.errors.UndefinedColumn: column "id" does not exist` | migration 没全跑 / 表 schema 漂移 |
| `openai.APIStatusError: 402` | DeepSeek 余额耗尽 / `max_tokens` 没 cap |
| `httpx.ConnectError ... ssl ...` | proxy 干扰；M1 必须 `trust_env=False` |
| `OSError: [Errno 36] File name too long` | `parse_input` path-shape guard 缺；M7 § 4.1 修 |
| `lookup_symbol` 总返回 not_found | CodeGraph repo_mapping 配置错；查 `configs/default.yaml::codegraph.repo_mapping` |
| `find_similar_crashes` 命中无关 case | stack_signature 算法变了没重签 4 张表；跑 `scripts/resign_signatures.py` |

---

## Q14 · 我能换 prompt 风格吗？

可以但慎重。`20_PROMPT_ASSETS/*.md` 是经过 22 case + 5 轮 prompt engineering 调出来的——改任何一段都可能：
- 让 agent 跳过 Phase 0 / Phase 4
- 让答案丢失候选假设
- 触发 DSML 幻觉的几率上升

如果你要 fork prompt：
1. 在 case 级别打 process_compliance 基线
2. 改完跑同样的 22 case
3. 对比 phase_coverage / kg_path_coverage
4. **明显退化 = 改坏了**

历史教训：Run 19-20 加 `_validate_final_answer` retry 把答案推退化到 `<insufficient_evidence>`；已回滚。改 prompt 是高风险动作。

---

> 还有其他问题？查 `60_GOTCHAS.md` 是否提及 → 没提及就属于"重建过程中可能出的新坑"，把它加进 `60_GOTCHAS.md` § 7"编辑约定"格式贡献回这个交付包。

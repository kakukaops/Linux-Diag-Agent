# 60 · 跨模块已知陷阱

> 模块独有陷阱写在 `10_MODULE_SPECS/M*.md` 各自 § 6。本文件汇总**两个以上模块都会踩**的陷阱——重写时早看早省事。
>
> _本文件持续扩充。AI agent 在实现任何一个模块前应**先通读一遍**，做交叉对照。_

---

## §1 · PostgreSQL / psycopg3

### #1.1 命名参数 + 类型转换：用 `CAST`，不要 `::`

```sql
-- ✗ psycopg3 解析器把 `::` 当成 placeholder 边界，报 syntax error
WHERE meta = :meta::jsonb

-- ✓
WHERE meta = CAST(:meta AS jsonb)
```

涉及模块：M3 (ingestion 写 jsonb 字段) / M5 (retrieval 没 jsonb 参数所以无关) / M7 (state 持久化)

### #1.2 `references` 是 PG 保留字

`cve.references` 是 NVD 引用列表。任何 SQL 必须双引号包裹：

```sql
SELECT "references" FROM cve WHERE cve_id = :id
```

涉及模块：M3 NVD ingester / M5 cve 路 recall

### #1.3 `ingest_runs` 表只在 run 结束时写

不要在 ingester 开始时插入"pending"行——只在 success / failure 完成时插入。约定见 `agent/CLAUDE.md` 实现层注释；该表是审计日志，不是任务调度器。

涉及模块：M3 全部 ingester

### #1.4 全文索引必须走预生成 `body_tsv`

```sql
-- ✓
WHERE body_tsv @@ to_tsquery('english', :q)

-- ✗ 全表扫描
WHERE to_tsvector('english', body) @@ to_tsquery('english', :q)
```

`body_tsv` 是 GENERATED STORED 列 + GIN 索引（migration 0001）。涉及模块：M2 / M5。

---

## §2 · 外部 API quirks

### #2.1 NVD CVE API（services.nvd.nist.gov/rest/json/cves/2.0）

- **正确**：用 `keywordSearch=linux kernel` 过滤
- **错误**：`virtualMatchString` + CPE → 返回 404
- **最大日期窗**：120 天 → 实现要切成 ≤ 119 天滑窗
- **限流**：无 API key 时 6.0 s/req（`_DELAY_S = 6.0`）—— 不要降低
- **field 提取**：`fix_commits` 从 `references` 里抓 GitHub / kernel.org commit URL（regex 见 M3）

涉及模块：M3 NVD ingester

### #2.2 lore.kernel.org

- **错误**：`?x=mbox&since=` 返回 HTML，不是 mbox
- **正确**：Atom feed `/<list>/?q=d:YYYYMMDD..&x=A&o=N`（200 entries/page）+ 单消息 `/<list>/<msg-id>/raw`
- **限流**：1.5 s/req（`_REQUEST_DELAY_S = 1.5`）—— 全部请求共享，包括分页拉 Atom + 拉 raw
- **大列表（linux-mm 30 天）**：1-2 h 才完整入库

涉及模块：M3 lkml fetcher

### #2.3 Bugzilla REST API（bugzilla.kernel.org/rest/bug）

- **日期过滤字段名**：`last_change_time`（不是 `changed_after`）
- **日期格式**：`YYYY-MM-DD` 字符串（不是 ISO datetime）
- **限流**：1.5 s/req

涉及模块：M3 bugzilla ingester

### #2.4 syzbot HTML scraper

- CSS selector 用 `list_table`（2026 syzbot 改版后）
- URL 参数：`extid=<id>`（不是 `id=`）
- 限流：0.5 s/req（用户已批准从默认 1.5 s 降低）

涉及模块：M3 syzbot scraper

---

## §3 · git 子进程

### #3.1 OLK kernel git log 含非 UTF-8 字节

某些 commit message 含 latin-1 / cp1252 字节，`subprocess` 默认 `text=True` 会抛 `UnicodeDecodeError`。

```python
# ✓
result = subprocess.run(["git", "log", ...], capture_output=True)
text = result.stdout.decode("utf-8", errors="replace")

# ✗
result = subprocess.run(["git", "log", ...], capture_output=True, text=True)
```

涉及模块：M3 kernel_commit ingester / M4 linker（间接，linker 读 commit body）

### #3.2 OLK inclusion 类型解析

```
tag ∈ {mainline, stable}  → backport（有 upstream SHA）
其他任何值                → openEuler-native（没 upstream）
NULL / 解析失败            → 当作 native 处理
```

**不要枚举 native 类型**——是开放集合（40+ 种：hulk / driver / urma / 厂商名 / ...）。只判 `mainline` / `stable` 二元。

涉及模块：M3 kernel_commit / M4 linker / M5 retrieval

---

## §4 · LLM provider quirks

### #4.1 DeepSeek 在 `tool_choice="none"` 时会幻觉 tool_call 语法

DeepSeek-V3（2026 起）在被强制不调工具但还想调工具时，会在 content 里输出形如：

```
<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="search_commits">
...
```

——这是 DeepSeek 内部 token 串泄露，**不是合法 OpenAI tool_calls**。

**对策**（agent/react/loop.py）：
- 在 force_finalize reminder 里**明确禁止**任何 tool-call syntax
- 检测到 `DSML` / `<tool_calls>` / `<invoke ` 出现时，给一次"hard reset retry"机会，要求 LLM 仅用 `<final_answer>` / `<insufficient_evidence>` 标签作答

涉及模块：M1 LLM Provider（不直接处理）/ M7 ReAct loop（强制 retry 逻辑）

### #4.2 OpenAI-compat 客户端**不要**走主机 HTTPS_PROXY

WSL / 公司网络环境常设 `HTTPS_PROXY=http://gateway:8080`。LLM 端点（DeepSeek / OpenRouter / OpenAI / vLLM）**直连可达**，走代理只增延迟 + 偶发 5xx。

**对策**：构造 httpx client 时强制 `trust_env=False`：

```python
import httpx, openai
http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout, connect=10.0))
client = openai.OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)
```

涉及模块：M1 LLM Provider

### #4.3 `max_tokens` 必须显式设上限

OpenRouter / 部分 vendor 给"未设 max_tokens"的请求按模型完整输出预算预留信用（如 65K），余额不足时报 402。

**对策**：默认 cap 在 4K（ReAct 单步从不需要更多）：

```python
params["max_tokens"] = req.max_tokens or 4_000
```

涉及模块：M1 LLM Provider

---

## §5 · Agent / Diagnosis

### #5.1 LangGraph 1.2+ 节点签名只能用 `dict`，不能 `TypedDict`

用 TypedDict 时 LangGraph 内部 merge 行为会丢 state 字段。统一用 plain `dict[str, Any]`，返回 `{**state, ...}` 显式合并。

涉及模块：M7 全部 LangGraph 节点

### #5.2 dmesg parser 必须先剥 `[timestamp]` 前缀

```
[12345.681001]  dump_stack_lvl+0x4d/0x6c
```

生产 dmesg **总是**带行号前缀。任何按行匹配（OOM head / call trace frame）必须先 `re.sub(r"^\[\s*[\d.]+\]\s*", "", line)`。

涉及模块：M6 (dmesg_journal extractor) / M7 ReAct 工具 `extract_call_trace`

### #5.3 `stack_signature` 必须过滤通用诊断帧

`dump_stack_lvl` / `dump_header` / `show_stack` / `__warn` / 异常入口（`exc_page_fault` 等）/ panic / printk / watchdog — 这些帧出现在任何崩溃栈里，没有 distinguishing power。把它们哈希进 signature 会让 OOM 和 JFFS hungtask 算出相同签名。

完整 `_INTERNAL_FRAMES` 集合见 `kg/CLAUDE.md` § signature。

涉及模块：M4 (`link_event_signature` 计算) / M3 (`syzbot_crash.stack_signature` ingest) / M5 (find_similar_crashes)

---

## §6 · 评测 / 数据完整性

### #6.1 `link_commit_bug` 长期为 0 不是 bug，是数据缺口

OLK commits 的 `bugzilla:` trailer 三种形态：
- `bugzilla: https://gitee.com/openeuler/kernel/issues/XXXXX`（~63 K 条）→ gitee issue，需 gitee ingester（ADR-022 已实现）
- `bugzilla: https://atomgit.com/openeuler/kernel/issues/NNNN`（~7.9 K 条）→ atomgit issue，同上
- `Link: https://bugzilla.kernel.org/show_bug.cgi?id=NNNNN`（~1.6 K 条）→ bug 表有但按 last_change_time 过滤过老的未入库

链接代码本身正确，数据没入库导致写入 0 行。

涉及模块：M3 / M4

### #6.2 Backfill 增量循环陷阱

增量 backfill（如 link_commit_symbol）从 `NOT EXISTS (link_table)` 选候选时，如果 extract 函数返回空，**必须写一个 sentinel 行**（如 `link_type='!sentinel'` 或单行占位）。否则下次增量还选到这条记录，extract 还是空，**永远循环**。

涉及模块：M4 linker backfill / M3 symbol backfill

---

## §7 · 编辑约定

每条 gotcha 必须含：
1. **错误形态**（怎么看出来）
2. **正确做法**（怎么避免 / 修）
3. **涉及模块**（让 AI 读完知道在哪查）

新发现的 gotcha 增量加进对应 §，不要起新文件。

# 30 · ReAct Tool Contracts

> 32 个 ReAct 工具的**完整契约**——每个工具：用途 / 输入 JSON Schema / 输出格式 / 路由暴露 / 行为契约 / 已知陷阱 / 重建注意。
>
> AI agent 重建时要让 LangChain `Tool` / OpenAI `function calling` schema 与本文件**严格匹配**——LLM 的工具选择依赖 description，名称匹配差一字就调不通。

## Route 暴露表

工具按 `routes: frozenset[str]` 字段决定哪些路由能看到它。5 个路由集合：

```python
_ALL = frozenset({"kernel", "kernel+vmcore", "hardware", "change", "unknown"})
_K   = frozenset({"kernel", "kernel+vmcore", "unknown"})        # kernel 主路由
_KC  = frozenset({"kernel", "kernel+vmcore", "change", "unknown"})  # kernel + change
_KH  = frozenset({"kernel", "kernel+vmcore", "hardware", "unknown"})  # kernel + hardware
_KV  = frozenset({"kernel+vmcore"})                              # vmcore 专属
_HW  = frozenset({"hardware"})                                   # 硬件专属
```

### 工具索引（按类别 + 路由）

| # | 工具 | 路由 | 主要用途 |
|---|---|---|---|
| **A. 检索（PG / CodeGraph BM25）** | | | |
| 1 | `search_commits` | _ALL | OLK kernel commit BM25 |
| 2 | `search_lkml` | _ALL | LKML mail BM25 + lore live |
| 3 | `search_bugs` | _ALL | Bug DB BM25 |
| 4 | `search_syzbot` | _ALL | syzbot crash BM25 |
| 5 | `search_cve` | _ALL | CVE 精确 ID + BM25 |
| 6 | `search_code` | _K | CodeGraph kernel source BM25（**AND 语义**）|
| 7 | `lookup_symbol` | _K | CodeGraph SCIP 精确符号定义 |
| **B. 日志解析（MCP）** | | | |
| 8 | `parse_dmesg` | _ALL | 抽 dmesg 事件 |
| 9 | `parse_sosreport` | _ALL | 解 sosreport 归档 |
| 10 | `extract_call_trace` | _ALL | 从 dmesg 抽栈帧 |
| 11 | `parse_taint_flags` | _ALL | 解析 taint 字母含义 |
| **C. Commit / Code Detail** | | | |
| 12 | `get_commit_detail` | _KC | 读 commit 元数据 + body |
| 13 | `get_commit_diff` | _KC | 读实际 diff |
| 14 | `check_backport_status` | _K | upstream SHA 在 OLK-X.Y 是否已 backport |
| 15 | `get_function_source` | _K | 读函数源码（SCIP 精确） |
| 16 | `get_call_graph` | _K | SCIP callers / callees |
| 17 | `expand_query_from_symbol` | _K | 从符号源码抽候选关键词 |
| 18 | `decode_stacktrace` | _K（M9 暂未启用） | 把 raw addresses 解码为函数名 |
| 19 | `fetch_debuginfo` | _K（M9 暂未启用） | 拉 debuginfo 包 |
| **D. KG 派生关系查询** | | | |
| 20 | `find_commits_touching_symbol` | _K | symbol → commits（link_commit_symbol 反查） |
| 21 | `find_similar_crashes` | _K | 栈签名 → syzbot/bug/lkml/dmesg_event 同栈 |
| 22 | `find_syzbot_fixed_by_commit` | _K | 该 commit 修过哪些 syzbot |
| 23 | `browse_subsystem_fixes` | _K | mm/net/fs 子系统最近 fix |
| 24 | `get_regression_fixes` | _K | commit 是否被 revert / 引入回归 |
| 25 | `get_patch_series` | _K | message_id → 同一 series 邮件链 |
| 26 | `lookup_subsystem_owner` | _K | file_path → MAINTAINERS 维护者 |
| **E. 杂项** | | | |
| 27 | `get_device_major_mapping` | _K | major number → driver 名 |
| **F. 硬件路由专属** | | | |
| 28 | `get_mce_log` | _HW | 读 MCE 错误 |
| 29 | `get_edac_errors` | _HW | EDAC 计数 |
| 30 | `get_ipmi_sel` | _HW | IPMI SEL 日志 |
| 31 | `get_hardware_inventory` | _HW | dmidecode 摘要 |
| **G. Vmcore 专属** | | | |
| 32 | `analyze_vmcore` | _KV | drgn 分析 vmcore |

> 注：当前主要 demo 跑在 `route=kernel`，暴露 26 个工具（_HW + 1 个 _KV 不含）。

---

## A · 检索类（7 个）

### 1. `search_commits`

- **路由**：_ALL
- **用途**：PG `kernel_commit` 表 BM25 搜索 OLK kernel commit
- **输入**

```json
{
  "type": "object",
  "properties": {
    "keywords": {"type": "string", "description": "Space-separated search terms"},
    "kernel_version": {"type": "string", "description": "OLK-6.6 / OLK-5.10 / None"},
    "limit": {"type": "integer", "default": 10}
  },
  "required": ["keywords"]
}
```

- **输出**：`Evidence[]` 序列化文本（每条带 hash / subject / score / body 截断）
- **契约**：keywords 当作 PG `tsvector` query，多词 OR；当 kernel_version 给且非 None，加 `affected_versions @> :ver` 过滤；**永远过滤 stub commit**（`subject NOT LIKE '[stub upstream%'`）
- **基础 SQL**：见 `M5_retrieval.md § 5`

### 2. `search_lkml`

- **路由**：_ALL
- **用途**：PG `lkml_message` 表 BM25 + 可选 lore.kernel.org 实时检索
- **输入**：`{keywords, limit, also_live: bool default false}`
- **输出**：合并 PG (L2) + lore (L3) 结果
- **契约**：`also_live=true` 时调 lore Atom feed `/all/?q=...&x=A`，限流 1.5s（共享 lore client）

### 3. `search_bugs`

- **路由**：_ALL
- **输入**：`{keywords, source: "kernel.org"|"gitee"|"atomgit"|null, limit}`
- **输出**：bug 行 + body 截断
- **契约**：`source` 给定时加 WHERE 过滤

### 4. `search_syzbot`

- **路由**：_ALL
- **输入**：`{keywords, status: "open"|"fixed"|null, limit}`
- **输出**：syzbot_crash 行 + stack_signature
- **契约**：`status` 默认所有；BM25 在 `body_tsv`（含 title + stack_trace 文本）

### 5. `search_cve`

- **路由**：_ALL
- **输入**：`{cve_id?: string, keywords?: string, limit}`
- **输出**：cve 行 + CVSS + body
- **契约**：`cve_id` 非空时**先精确 ID 查找**（score=1.0），后 BM25 补齐至 limit；`cve_id` 空则纯 BM25

### 6. `search_code`

- **路由**：_K
- **输入**：`{query, kernel_version, limit}`
- **输出**：CodeGraph 命中（file + snippet）
- **契约**：
  - **AND 语义**：所有 token 必须出现同一文件
  - 多词 query 0 命中时**自动按 token 拆词重试 + 合并去重**（详见 `60_GOTCHAS.md` §5 / `M5_retrieval.md § 6 #3`）
  - kernel_version 通过 `repos: [<resolved_repo>]` 参数传 CodeGraph
- **已知陷阱**：query "memcg OOM during reclaim"（自然语言）→ 0 命中；要 `try_charge_memcg` 单词或 quoted phrase

### 7. `lookup_symbol`

- **路由**：_K
- **输入**：`{name, kernel_version, action: "definition"|"references"|"implementations"|"type"}`
- **输出**：SCIP 精确定义 / 引用 / 实现 / 类型链接
- **契约**：name 必须**精确**（区分大小写），CodeGraph SCIP 索引按精确符号 ID 查；模糊查走 `search_code`

---

## B · 日志解析（4 个）

### 8. `parse_dmesg`

- **路由**：_ALL
- **输入**：`{text: string}`
- **输出**：`KernelEvent[]` 序列化文本（含 kind / summary / call_trace / metadata）
- **契约**：见 `M6_host_mcp_tools.md § 3`；**OOM 三种格式都识别**

### 9. `parse_sosreport`

- **路由**：_ALL
- **输入**：`{path: string}`
- **输出**：SystemSummary 序列化文本
- **契约**：见 `M6_host_mcp_tools.md § 4`

### 10. `extract_call_trace`

- **路由**：_ALL
- **输入**：`{text: string}`
- **输出**：栈帧函数名列表 + 偏移
- **契约**：**必须**先剥 `[timestamp]` 前缀（见 `60_GOTCHAS.md` §5.2）；`Call Trace:` 开 / `RIP:` `Code:` `---[ end trace ]---` 关

### 11. `parse_taint_flags`

- **路由**：_ALL
- **输入**：`{taint_value: string}` (如 `"G W OE"` 或十六进制 `0x...`)
- **输出**：字母 → 含义映射（如 `G: Proprietary module loaded`）

---

## C · Commit / Code Detail（8 个）

### 12. `get_commit_detail`

- **路由**：_KC
- **输入**：`{commit_hash: string}` (full or short ≥ 12 hex)
- **输出**：subject / author / date / inclusion_type / upstream_commit / body 截断（max 5K chars）
- **契约**：用 `short_hash` 索引精确匹配（不是 LIKE）；**跳过 stub commit**（返回 "not found" 而不是 stub body）

### 13. `get_commit_diff`

- **路由**：_KC
- **输入**：`{commit_hash, max_chars: int default 5000}`
- **输出**：git diff 文本截断
- **契约**：从本地 git 仓库读（不是 DB），`git -C <repo> show <hash>`

### 14. `check_backport_status`

- **路由**：_K
- **输入**：`{upstream_sha: string, olk_version: string}`
- **输出**：`"BACKPORTED in OLK-X.Y as <olk_sha>"` / `"NOT backported"`
- **契约**：查 `kernel_commit WHERE upstream_commit = :sha AND :olk_version = ANY(affected_versions)`；同时返回找到的 olk commit subject

### 15. `get_function_source`

- **路由**：_K
- **输入**：`{func_name, kernel_version}`
- **输出**：SCIP 定义 + 函数源码
- **契约**：用 `lookup_symbol(action="definition")` SCIP 路径；不命中 fallback 到 `search_code(func_name)`

### 16. `get_call_graph`

- **路由**：_K
- **输入**：`{func_name, direction: "callers"|"callees", kernel_version}`
- **输出**：
  - `direction=callers` → SCIP `references` 查询
  - `direction=callees` → 函数 definition + 调用清单（如果 CodeGraph 支持就走 SCIP，否则 fallback 到 search_code 抓函数体后正则提取调用）
- **契约**：direction 缺省 `callers`；direction 必须改变结果（之前 bug：方向无效，两边一样）

### 17. `expand_query_from_symbol`

- **路由**：_K
- **输入**：`{symbol, kernel_version}`
- **输出**：候选关键词列表（按频次降序）
- **契约**：用 `lookup_symbol(definition)` 拿源码 → 抽 snake_case / ALL_CAPS 标识符 → 去 stopwords（`if/else/for/struct/...`）→ Counter top 20

### 18. `decode_stacktrace`（vmcore 阶段；当前未完全启用）

### 19. `fetch_debuginfo`（vmcore 阶段；同上）

---

## D · KG 派生关系查询（7 个）

### 20. `find_commits_touching_symbol`

- **路由**：_K
- **用途**：reverse lookup —— 给定符号，列出**改过该符号**的 OLK commit
- **输入**：`{symbol, kernel_version, since_months: int default 24, limit: int default 20}`
- **输出**：commit 列表 + subject + 日期
- **契约**：查 `link_commit_symbol` join `kernel_commit`，按 commit_date 倒序

### 21. `find_similar_crashes`

- **路由**：_K
- **输入**：`{trace_text: string, limit: int default 10}`
- **输出**：syzbot / bug / lkml_message / dmesg_event 中**栈签名匹配**的记录
- **契约**：
  1. 算 `stack_signature(trace_text)` —— 用 `kg/signature.py` 同款算法（top-4 distinguishing function names 哈希）
  2. 在 4 张表 `WHERE stack_signature = :sig` 各查 limit/4 条
  3. 合并去重，按 last_seen / date 倒序

### 22. `find_syzbot_fixed_by_commit`

- **路由**：_K
- **输入**：`{commit_hash}`
- **输出**：syzbot_crash 行（如有）
- **契约**：查 `link_syzbot_commit WHERE commit_hash = :h`

### 23. `browse_subsystem_fixes`

- **路由**：_K
- **输入**：`{subsystem_prefixes: string ("mm,net,fs/ext4"), contains: string?, since_months: int, limit}`
- **输出**：commit 列表
- **契约**：`kernel_commit WHERE subsystem LIKE ANY(...prefixes...) AND (contains 为空 OR subject ILIKE %contains% OR body ILIKE)`
- **gotcha**：宽 contains（"crash"、"fix"、"panic"）比窄 contains（"KASAN out-of-bounds skb_put"）效果好得多

### 24. `get_regression_fixes`

- **路由**：_K
- **用途**：**Phase 4 mandatory** —— 检查这个 commit 是否被 revert / 引入回归
- **输入**：`{commit_hash}`
- **输出**：
  ```
  - revert_of: <sha>   (该 commit 是 revert 别人)
  - reverted_by: <sha>(s)  (该 commit 被别人 revert)
  - introduced_regression_fixed_by: <sha>(s)  (从 link_commit_fixes 反查)
  ```
- **契约**：
  - 查 `link_commit_revert WHERE target_hash = :h`（被 revert）
  - 查 `link_commit_revert WHERE revert_hash = :h`（是 revert）
  - 查 `link_commit_fixes WHERE fixes_hash = :h`（被 Fixes: 引用 = 引入回归）
  - 任一非空 → 输出包含 "⚠ WARNING: this commit was REVERTED ..." banner

### 25. `get_patch_series`

- **路由**：_K
- **输入**：`{message_id}`
- **输出**：同一 series 的所有 patch 邮件
- **契约**：通过 `lkml_patch.series_total` + `subject` `[PATCH N/M]` 解析重组

### 26. `lookup_subsystem_owner`

- **路由**：_K
- **输入**：`{file_path}` (如 `mm/memcontrol.c`)
- **输出**：MAINTAINERS section + 维护者 + 邮件列表 + status (Maintained / Orphan / Odd Fixes)
- **契约**：从 OLK kernel `MAINTAINERS` 文件解析（按 F: pattern 反查），输出最具体匹配 + status

---

## E · 杂项（1 个）

### 27. `get_device_major_mapping`

- **路由**：_K
- **输入**：`{major: int, kind: "block"|"char"}`
- **输出**：driver 名 + notes
- **契约**：静态 LANANA 表（`mcp_servers/shared/device_majors.py`）；动态范围标 `dynamic: true` 并提示"看 `/proc/devices`"

---

## F · 硬件专属（4 个，仅 route=hardware）

### 28-31 `get_mce_log` / `get_edac_errors` / `get_ipmi_sel` / `get_hardware_inventory`

- 当前未在主 demo 启用（需要 live host 访问 + `mcelog` / `edac-util` / `ipmitool` 安装）。
- 详细 schema 见 `mcp_servers/hardware/` 目录代码（重建时这一组优先级低）

---

## G · Vmcore 专属（1 个）

### 32. `analyze_vmcore`

- **路由**：_KV（仅 `kernel+vmcore`）
- **用途**：drgn 启动 + crash subcommand 跑常用诊断（`bt` / `mod` / `kmem -i` / `task -l`）
- 当前 deferred（vmcore 路由未在主 demo 启用，需要 drgn 容器化）

---

## 工具调用层契约（适用所有工具）

| # | 契约 | WHY |
|---|---|---|
| C1 | **每个工具实现**必须从一个 `**kwargs: object` 落地：LLM 偶尔会塞额外参数；忽略无关 keys 不报错 | 健壮性 |
| C2 | **失败行为统一**：抛 Exception → 上层 `react/loop.py` catch 改写为 `result = f"error: {exc}"`，`errored=True`，记 trace；不要在工具内静默吞错 | trace 必须能看到失败 |
| C3 | **返回值是 str**：所有工具最终返回 markdown 文本字符串，喂给 LLM；不要返回 dict / 对象 | LLM tool_message content 是字符串 |
| C4 | **长结果截断**：每工具返回 < ~3K chars（约 750 token）；超长内容用 `[truncated, see ...]` 提示 | 控制 token 用量 |
| C5 | **路由白名单**：工具 schema 注册时声明 `routes: frozenset`；ToolRegistry 按 route subsetting | 见 `agent/react/tool_registry.py` |

---

## 验收

实现完 32 个工具后跑：

```bash
python 50_TEST_FIXTURES/run_acceptance.py --module tool_contracts
```

脚本会：
1. 加载每个工具的 schema，验证 JSON Schema 合法
2. 对每个工具喂 minimal 合法输入，验证返回值是 str 且长度 ≤ 3K
3. 故意喂非法输入（缺 required key / 类型错误），验证抛 Exception 而非 silent fail

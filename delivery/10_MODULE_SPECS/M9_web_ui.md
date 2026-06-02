# M9 · Web UI (FastAPI + SSE)

## 1. 目的

把 M7 `diagnose()` 包装成一个值班工程师可以 hold-and-paste 的 web 应用：
- 单文本框收 dmesg / 自由文本
- 一个"诊断"按钮
- 5 个阶段实时滚动展示（triage → retrieval → ReAct deep dive → bind claims → SRE report）
- 双语切换（默认中文）

设计哲学：**让用户看清楚 agent 在干什么**，不是黑盒输出最终报告。

## 2. 公共接口

### 2.1 HTTP endpoints

| 路径 | 方法 | 用途 |
|---|---|---|
| `/` | GET | 返回单页 HTML（含 JS + CSS 全内联） |
| `/api/health` | GET | 返回 `{"status": "ok"}` |
| `/api/diagnose-stream` | POST | SSE 流式诊断；body `{"raw_input": "...", "lang": "zh"\|"en"}` |

### 2.2 SSE 事件 schema

```python
# 流式输出格式: data: <json>\n\n
{
    "type": "triage_input" | "triage_events" | "triage_signals" | "triage_route"
          | "retrieval"
          | "react_prep" | "react_step_start" | "react_tool_call" | "react_terminate"
          | "bind_claims"
          | "report"
          | "error" | "done"
    # 每种 type 有特定 payload，详见 § 4
}
```

## 3. 文件结构

```
web/
├── __init__.py              空
├── server.py                FastAPI app，3 endpoint
├── streaming_pipeline.py    generator 包装 diagnose() 流式 yield 事件
└── templates/
    └── index.html           单页 SPA（HTML+CSS+JS 三合一，零 npm/build）
```

## 4. SSE 事件 payload schema

按 stage 顺序：

```python
# 1. triage_input
{type: "triage_input", input_type: "dmesg"|"sosreport"|"question"|"dmesg_file", raw_input_len: int}

# 2. triage_events
{type: "triage_events", count: int,
 events: [{kind: str, summary: str}]}  # 截前 5 条

# 3. triage_signals
{type: "triage_signals",
 taint_flags: list[str], hardware_signals: list[str],
 io_hang_signals: list[str], has_hardware_signal: bool}

# 4. triage_route
{type: "triage_route", fault_kind: str, fault_summary: str,
 sop_name: str, diagnostic_route: str}

# 5. retrieval
{type: "retrieval", total: int,
 by_route: {"RouteTag.commit": int, ...},
 top: [{route, score, title, commit_hash}]}  # top 10

# 6. react_prep
{type: "react_prep", route: str, tools_available: int,
 system_prompt_len: int, user_prompt_len: int, lang: "zh"|"en"}

# 7. react_step_start (per iteration)
{type: "react_step_start", step: int, tokens_used: int, force_finalize: bool}

# 8. react_tool_call (per tool dispatched)
{type: "react_tool_call", step: int, tool: str, args: str (truncated 500),
 errored: bool, result_preview: str (truncated 300)}

# 9. react_terminate
{type: "react_terminate", verdict: str, content: str,
 iterations: int, tokens_used: int}

# 10. bind_claims
{type: "bind_claims", groundedness: str,
 verified_claims_n: int, speculative_claims_n: int,
 evidence_pool_size: int, overlap_threshold: float,
 claims: [{text, verified, refs, cited_evidence, unresolved_refs,
           evidence_strength, overlap_threshold, reason}]}  # 截前 8 条

# 11. report
{type: "report", report_md_len: int, report_md: str}  # 完整 markdown

# 12. error (any stage)
{type: "error", stage: str, message: str, trace: str}

# 13. done (sentinel)
{type: "done"}
```

## 5. 流式包装：`stream_diagnose(raw_input, lang)`

ReAct loop 是最长阶段，用 in-process Queue 让 loop 的 `on_event` callback 实时 push 到 SSE 流：

```python
def stream_diagnose(raw_input: str, lang: str = "zh") -> Iterator[dict]:
    state = {"raw_input": raw_input}
    state = parse_input(state); yield triage_input_event(state)
    state = extract_events(state); yield triage_events_event(state)
    state = detect_taint_and_hw_signals(state); yield triage_signals_event(state)
    state = classify_fault_and_route(state); yield triage_route_event(state)
    state = retrieve(state); yield retrieval_event(state)

    # ReAct 阶段：worker thread + Queue
    q = queue.Queue(); sentinel = object()
    def worker():
        try:
            run_react_loop(provider=..., on_event=lambda ev: q.put(ev), ...)
        finally:
            q.put(sentinel)
    threading.Thread(target=worker).start()
    while (ev := q.get()) is not sentinel:
        yield react_event(ev)

    state = bind_claims(state); yield bind_event(state)
    md = render_md(state, lang=lang); yield report_event(md)
```

## 6. 前端核心机制

### 6.1 SSE 消费

```javascript
const resp = await fetch("/api/diagnose-stream", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({raw_input: raw, lang: currentLang}),
});
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = "";
while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream: true});
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data:")) continue;
        const payload = chunk.slice(5).trim();
        ingest(JSON.parse(payload));  // 入事件 buffer + handle()
    }
}
```

### 6.2 双语 i18n

JS 端有 `LANG = {zh: {...}, en: {...}}` 字典，所有 UI 文本（标题 / 按钮 / stage 名 / 字段标签）经 `t(key)` 解析。`localStorage.lang` 持久化用户选择。

切换语言时**清 DOM 重渲染整页**（用 `events: list[]` buffer 重放 handle）。新语言会同时影响：
- UI 静态文字
- POST body 的 `lang` 字段（影响 system_prompt + render_md）
- Stage 标题 / 副标题 / 字段 label

### 6.3 5 阶段卡片

每 stage 一个 `.stage` div，含：
- **header**：title + 状态 badge（运行中 / 命中 N 条 / 已扎根 / ...）
- **subtitle**：一行子标题描述"此阶段在干什么"（用户视角）
- **body**：key-value 表 + 各 stage 特有内容（trace 步骤、claim 卡片等）

阶段名（用户视角，**不是工具实现名**）：

| Stage | zh | en |
|---|---|---|
| 1 | 阶段 1 · 解读故障 | Stage 1 · Parse fault |
| 2 | 阶段 2 · 检索历史问题 | Stage 2 · Search prior cases |
| 3 | 阶段 3 · 深入调研 | Stage 3 · Deep investigation |
| 4 | 阶段 4 · 核对证据 | Stage 4 · Verify evidence |
| 5 | 阶段 5 · 出具报告 | Stage 5 · Compose report |

详见 `web/templates/index.html` `LANG` dict。

## 7. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **SSE 而非 WebSocket**：单向流式够用，SSE 简单且浏览器原生支持 | 避免 WS 握手 + 双向状态机 |
| C2 | **fetch + ReadableStream**（不用 EventSource）：因为要 POST body 传 `raw_input` + `lang` | EventSource 只 GET |
| C3 | **流式 yield 必须立即冲刷**：`StreamingResponse` 加 `X-Accel-Buffering: no` header；nginx 反代要 `proxy_buffering off` | 否则用户看不到实时滚动 |
| C4 | **ReAct loop on_event 必须是 thread-safe**：用 in-process Queue（标准库 `queue.Queue`），不要直接 yield（generator + thread 混会卡死） | 见 `web/streaming_pipeline.py` 实现 |
| C5 | **prompt 不在前端拼**：lang 参数传到后端，由 `agent.react.prompts.render_system_prompt(route, state, lang)` 选 `.md` / `.zh.md` 模板 | 集中管理 + safer |
| C6 | **不存历史**：每次 POST 是独立 session，无 server-side state；浏览器关掉即丢 | 隐私 + 简化 |
| C7 | **httpx trust_env=False**：M1 已说，但 web server 起的 LLM client 必须继承此配置 | 见 `60_GOTCHAS.md` §4.2 |

## 8. 已知陷阱（M9 独有）

### #1 · Starlette `TemplateResponse` 新签名

```python
# Starlette ≥ 0.35
return templates.TemplateResponse(request, "index.html")

# Starlette < 0.35（已废弃，会 TypeError: unhashable type: 'dict'）
return templates.TemplateResponse("index.html", {"request": request})
```

### #2 · `until` 循环 watcher 别忘 kill

bash `until [ -f X ]; do sleep N; done` 后台跑会变孤儿（如果守的文件永远不出现）。实测踩过：Run 21 被中途 kill 后 watcher 跑了 6 天。修法：要么 watcher 有超时退出，要么 kill 主 process 后手动 kill watcher。

### #3 · `tool_choice='none'` 触发 DSML 幻觉

ReAct loop 在 `force_finalize` 时 `tool_choice='none'`，DeepSeek 可能输出伪造的 `<｜｜DSML｜｜tool_calls>` token。Web UI 必须能区分这种"虚假完成"和真正 `<final_answer>`，否则报告里渲染一坨 DSML 文本。

修复在 M7 loop.py 的 hallucination retry 机制。M9 web 只需要正确转发 `react_terminate` event。

### #4 · SSE 长流式 + 无 keep-alive ping

如果某 stage 耗时 > 30 s 且无新事件，浏览器 / proxy 可能断连。**对策**：每 stage 至少 yield 一个 event；超长阶段（ReAct 全程几分钟）天然密集 emit。不要给 SSE 加自定义 keep-alive ping，普通 stage 转发已足够。

> **see also**：`60_GOTCHAS.md` §4（LLM provider quirks 整章）/ §5（Agent diagnosis）

## 9. 验收

### 启动

```bash
python -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok"}

curl http://127.0.0.1:8000/ | grep -F "阶段 1 · 解读故障"
# 1
```

### 流式验证

```bash
curl -sS -N -X POST http://127.0.0.1:8000/api/diagnose-stream \
    -H 'Content-Type: application/json' \
    -d '{"raw_input": "ping test", "lang": "zh"}' | head -30
```

期望：连续看到 `data:` 行，按 stage 顺序，最后 `data: {"type":"done"}`。

### 浏览器手测 checklist

1. 打开 `http://127.0.0.1:8000/` — 默认中文界面，右上角 `EN` 切换按钮
2. 点 "填入示例" — textarea 加载 cases_smoke 内容
3. 点 "诊断" — 5 阶段卡片依次滚动出现
4. 切换语言 `EN` — 整页重新渲染英文（包括已显示的 stage）
5. Stage 3 深入调研区显示每步 LLM 调用 + 工具调用，spinner 在每步 header
6. Stage 4 核对证据显示每条 claim + 引用证据 + 判定原因
7. Stage 5 报告含双语 SRE 结构（## 根本原因 / ## 修复建议 / ## 置信度）

## 10. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | FastAPI ≥ 0.115 / Starlette ≥ 0.45 / Jinja2 / uvicorn |
| **依赖** | M7 Diagnosis Agent（`diagnose()` 完整链 + ReAct loop `on_event` 钩子）|
| **依赖** | M8 渲染器（`render_md(state, lang)` 出 markdown）|
| **配置 keys** | `web.host` / `web.port` / 继承 M1 的所有 LLM 配置 |
| **被消费** | 浏览器 / 内网工具集成 / Curl / 任何 SSE 客户端 |

---

> **重建校对**：跑 § 9 启动 + curl health + 手测 7 项 checklist 全部通过。

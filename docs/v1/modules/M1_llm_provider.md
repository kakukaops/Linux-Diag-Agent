# M1 — LLM Provider 抽象层 设计文档

| 字段 | 值 |
|------|---|
| 模块编号 | M1 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [ProjectPlan.md](../ProjectPlan.md) |
| 关联 ADR | [ADR-001](../adr/ADR-001-no-embedding.md) · [ADR-002](../adr/ADR-002-no-reranker.md) · [ADR-003](../adr/ADR-003-openai-chat-schema.md) · [ADR-004](../adr/ADR-004-claude-code-provider.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

### 1.1 目标

为所有上层模块（诊断 Agent / 检索层 / PageIndex 走查 / LLM 重排 / 评测）提供**统一的 LLM 调用接口**，使具体 backend 可通过配置切换，业务代码零改动。

### 1.2 范围

| 在范围 | 不在范围 |
|--------|---------|
| Chat Completion（含 tool calling、streaming、JSON mode） | RAG 编排（属 M5） |
| 多 backend 适配（claude_code / anthropic / openai_compat / vllm / sglang / ollama） | Prompt 工程模板（业务层各自负责） |
| Provider 抽象层与统一 schema | LLM 微调（v1.3 单独模块） |
| 调用缓存（PostgreSQL）+ 限流 + 重试 | Embedding 服务（[ADR-001](../adr/ADR-001-no-embedding.md)：全项目无 embedding） |
| 调用日志 + Prometheus metrics | Cross-encoder reranker 服务（[ADR-002](../adr/ADR-002-no-reranker.md)：用 LLM 重排替代） |
| 多 backend smoke test | 模型权重托管 |

### 1.3 模块角色

```
                ┌──────────────────────────────┐
                │  上层（Agent / 检索 / 评测）  │
                └────────┬─────────────────────┘
                         │ Provider.chat(messages, tools, ...)
                         ▼
            ┌────────────────────────────────┐
   M1 →     │   LLM Provider 抽象层 (本模块)  │
            │  ┌─────┐  ┌─────┐  ┌─────────┐ │
            │  │chat │  │rate │  │ cache   │ │
            │  │adapt│  │limit│  │ (PG)    │ │
            │  └─────┘  └─────┘  └─────────┘ │
            └────────┬───────────────────────┘
                     │
   ┌─────────────────┼──────────────────────────┐
   ▼          ▼      ▼      ▼            ▼      ▼
claude_code  anthropic openai vllm     sglang  ollama
(MVP 默认)             兼容
```

---

## 2. 关键决策摘要

| # | 决策 | ADR |
|---|------|-----|
| D1 | 全项目无 embedding，路 A/B 都靠 BM25+Graph+LLM-walk 召回 | [ADR-001](../adr/ADR-001-no-embedding.md) |
| D2 | 无 cross-encoder reranker，用 LLM 重排（Navigator backend）做精排 | [ADR-002](../adr/ADR-002-no-reranker.md) |
| D3 | 内部统一 schema = OpenAI Chat Completions | [ADR-003](../adr/ADR-003-openai-chat-schema.md) |
| D4 | MVP 默认 backend = `claude_code`（包 `claude -p` subprocess）；6/15 后切 Agent SDK | [ADR-004](../adr/ADR-004-claude-code-provider.md) |
| D5 | 缓存层 = PostgreSQL（复用，零新增依赖） | 本文 §6 |
| D6 | streaming = P0（CLI 长报告必须流式） | 本文 §3 |
| D7 | Prompt Caching MVP 不启用，schema 预留 `cache_control` 字段 | 本文 §3 |
| D8 | Self-Consistency K=1 默认起步（Pro 订阅 rate limit 紧），K=3 评测阶段按需开 | 本文 §7 |
| D9 | 并发上限 = 4（保守，Pro 订阅）| 本文 §7 |

---

## 3. 接口契约（OpenAI Chat Completions 内部统一）

### 3.1 类型定义

```python
from typing import TypedDict, Literal, Protocol

class ContentPart(TypedDict, total=False):
    type: Literal["text", "image_url"]
    text: str
    image_url: dict
    cache_control: dict | None      # {"type": "ephemeral"}, MVP 不下发但 schema 保留

class ToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: dict                   # {name, arguments(str-encoded JSON)}

class Message(TypedDict, total=False):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart]
    tool_calls: list[ToolCall] | None
    tool_call_id: str | None         # 当 role="tool"
    name: str | None

class ToolSchema(TypedDict):
    type: Literal["function"]
    function: dict                   # {name, description, parameters: JSON Schema}

class ChatRequest(TypedDict, total=False):
    messages: list[Message]
    model: str                       # 由 config 解析为具体 backend model id
    tools: list[ToolSchema] | None
    tool_choice: Literal["auto", "required", "none"] | dict | None
    temperature: float               # 默认 0.0
    max_tokens: int | None
    stream: bool                     # 默认 True（业务层按需消费）
    response_format: dict | None     # {"type": "json_object"} 或 {"type": "json_schema", ...}
    metadata: dict                   # trace_id, user_id, sop_id 等

class Usage(TypedDict):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int           # 未启用时恒 0
    cache_write_tokens: int          # 未启用时恒 0
    cost_usd_est: float

class ChatResponse(TypedDict):
    content: str
    tool_calls: list[ToolCall]
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    usage: Usage
    raw_response: dict               # 原生 backend 响应，调试用
    provider: str                    # 实际使用的 provider name
    cached: bool                     # 是否命中本地 PG 缓存

class LLMProvider(Protocol):
    def chat(self, req: ChatRequest) -> ChatResponse: ...
    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]: ...
```

### 3.2 错误模型

```python
class LLMProviderError(Exception):
    code: str                        # rate_limited / context_exceeded / auth / tool_schema_invalid / unknown
    retry_after_seconds: float | None
    raw_error: dict
    provider: str
```

业务层只 catch `LLMProviderError`，根据 `code` 决策。

---

## 4. Provider 适配清单

### 4.1 `claude_code`（MVP 默认）★

**实现策略**：

阶段 A（今天起 → 2026-06-15）：包 `claude -p` subprocess
- 调用方式：`claude -p --output-format stream-json --verbose --include-partial-messages --bare`
- 输入：把 OpenAI Chat `messages` 翻译为 Anthropic Messages 格式，再经 stdin 或 `-p` 参数注入
- 工具：OpenAI `tools` 翻译为 `--allowedTools "Tool1,Tool2,..."`；工具 schema 通过 MCP server 配置（[详见 M6](#)）
- 输出：解析 stream-json 事件流，转回 OpenAI Chat schema
- 认证：继承 Claude Code 已登录的 OAuth（无需独立 API key）
- 错误码映射：`rate_limit_exceeded` → `LLMProviderError(code="rate_limited", retry_after=...)`

阶段 B（2026-06-15 后）：切到 `claude-agent-sdk` Python 包
- 同样的 OpenAI Chat schema ↔ Anthropic Messages 翻译，但调用走 SDK 而非 subprocess
- 业务层零改动；只需更新 `configs/default.yaml` 的 `claude_code.adapter` 从 `subprocess` 改为 `agent_sdk`

**关键限制**（Pro 订阅）：

| 限制 | 数值 | 影响 |
|------|------|------|
| 5-hour window 消息数 | ~45 messages | 单复杂诊断（5-10 LLM 调用）约消耗 5-10 条；5h 可跑 ~5-9 个完整诊断（K=1） |
| 并发 | < 10 | provider 内部限 4 并发 |
| Context 窗口 | 200K 但实际可用 ~60-80K（auto-compaction）| 长 oops + RAG context 需谨慎 chunk |
| Streaming | 支持 stream-json | 启用 |
| Tool use | `--allowedTools` 预审批 | 启用 |
| JSON schema 输出 | `--json-schema` 支持 | 启用 |
| Prompt Caching `cache_control` | 不直接暴露 | MVP 不下发，schema 保留 |

**评测期 Pro 限速规划**：30 例评测每例约 8 messages，总 ~240 messages → 跨 ~6 个 5h 窗口 → **30 例至少分 7 天跑完**。计划层面要在 M9 排期里把这一点写进去。

### 4.2 `anthropic`（独立 API key，可选）

- SDK：`anthropic` 官方包
- 同样 OpenAI Chat schema ↔ Anthropic Messages 翻译
- 启用 `cache_control` 字段下发（项 D7 锁定后再开关）
- 用户当前没有独立 API key，**不作为默认 backend**，但代码留位

### 4.3 `openai_compat`

- SDK：`openai` 官方包，`base_url` 切换
- 适用：OpenAI / DeepSeek / Moonshot / 智谱 / 阿里百炼 / 火山方舟 / Together / Fireworks
- 适配层无需翻译 schema（原生 OpenAI Chat）
- 注意：tool calling 在部分国产 provider 上 schema 兼容性不全，需 smoke test

### 4.4 `vllm`

- SDK：`openai` 兼容（vLLM 自带 OpenAI Compatible Server）
- 启动：`vllm serve <model> --host 0.0.0.0 --port 8000`
- 推荐模型（按 v1.3 路线）：
  - Chat：`Qwen/Qwen2.5-Coder-32B-Instruct` 或 `deepseek-ai/DeepSeek-V3`
  - Navigator：`Qwen/Qwen2.5-7B-Instruct`
- v1.0 阶段只接入接口，不实跑

### 4.5 `sglang`

- 同 vLLM，OpenAI 兼容
- v1.3 评估是否切（高吞吐场景）

### 4.6 `ollama`

- SDK：`openai` 兼容（`http://localhost:11434/v1`）
- 用途：本地轻量验证、unit test、个人开发
- 推荐：`qwen2.5-coder:7b`、`deepseek-r1:14b`

---

## 5. 模型角色矩阵

| 角色 | 用途 | MVP backend / model | 本地切换（v1.3） |
|------|------|---------------------|------------------|
| **Chat** | 主推理：假设生成、根因综合、报告生成 | `claude_code` / `claude-sonnet-4-x`（用户 Pro 订阅默认可用 Sonnet） | vLLM / Qwen2.5-Coder-32B |
| **Navigator** | PageIndex 走查 + LLM 重排 | `claude_code` / `claude-haiku-4-5`（便宜，频繁调用合适） | vLLM / Qwen2.5-7B |
| ~~Embedding~~ | 不部署 | — | — |
| ~~Reranker~~ | 不部署 | — | — |

> Pro 订阅下 Opus 调用消耗额度更快，且 MVP 不需要顶级模型 ‒ 优先用 Sonnet 节省额度；评测期单独跑 Opus 对比。

---

## 6. 缓存策略 — PostgreSQL 单层

### 6.1 Schema

```sql
CREATE TABLE llm_response_cache (
    cache_key       TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    schema_version  INT NOT NULL DEFAULT 1,
    response        JSONB NOT NULL,
    usage           JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_hit_at     TIMESTAMPTZ DEFAULT NOW(),
    hit_count       INT DEFAULT 0,
    cost_saved_usd  NUMERIC DEFAULT 0
);
CREATE INDEX idx_llm_cache_created ON llm_response_cache(created_at);
CREATE INDEX idx_llm_cache_last_hit ON llm_response_cache(last_hit_at);
```

### 6.2 缓存 key

```python
cache_key = sha256(json.dumps({
    "schema_version": 1,
    "provider": req.provider_name,
    "model": req.model,
    "messages": req.messages,
    "tools": req.tools,
    "tool_choice": req.tool_choice,
    "temperature": req.temperature,
    "response_format": req.response_format,
})).hexdigest()
```

### 6.3 策略

| 条件 | 缓存 |
|------|------|
| `temperature == 0` | ✓ 缓存 |
| `temperature > 0` | ✗ 不缓存（非确定） |
| `stream == True` | ✓ 缓存（聚合后存） |
| TTL | 30 天（可配） |
| 清理 | 月度 cron（删 `created_at < now() - interval '30 days'`） |
| 命中行为 | 不调 backend，直接返回；同时 `hit_count++`，更新 `last_hit_at` |
| 评测阶段绕过 | 评测脚本可强制 `cache=false`，确保真实跑 |

### 6.4 与 Anthropic Prompt Caching 的关系

| 路径 | PG 本地缓存 | Anthropic Prompt Cache |
|------|------------|----------------------|
| PG 命中 | ✓ 零调用、零成本 | 不触发 |
| PG 未命中 | 进入 backend | 通过 `cache_control` 字段触发；但 MVP 阶段 `claude_code` provider 不下发该字段（schema 保留） |

---

## 7. 限流、重试、并发

### 7.1 配置（`configs/default.yaml`）

```yaml
llm:
  chat:
    backend: claude_code
    adapter: subprocess              # subprocess | agent_sdk (6/15 后切)
    model: claude-sonnet-4-x
    rate_limit:
      window_seconds: 18000          # 5h
      max_messages: 40               # Pro = 45, 留 5 buffer
    concurrency: 4
    retry:
      max_attempts: 3
      backoff_initial: 2.0
      backoff_max: 60.0
    self_consistency_k: 1            # MVP 默认；评测可改为 3
  navigator:
    backend: claude_code
    adapter: subprocess
    model: claude-haiku-4-5
    rate_limit:
      window_seconds: 18000
      max_messages: 40               # 与 chat 共享 Pro 额度
    concurrency: 4
  cache:
    enabled: true
    ttl_days: 30
    bypass_when_metadata_has: ["eval", "force_fresh"]
```

### 7.2 实现

- **限流**：滑动窗口 + 漏桶，记录最近 5h 内每次调用时间戳，超阈值阻塞（不丢请求）
- **重试**：tenacity，仅对 `rate_limited` / `transient` 错误重试，不对 `auth` / `tool_schema_invalid` 重试
- **并发**：asyncio.Semaphore(concurrency)，每 provider 独立池

### 7.3 Pro 订阅的 5h 预算监控

provider 启动时启一个 background reporter，每 5 分钟向 Prometheus 暴露：
- `llm_rate_budget_used_total{provider="claude_code"}` (0..45)
- `llm_rate_budget_remaining{provider="claude_code"}` (45..0)
- `llm_rate_window_reset_seconds`（距下一个 window 重置秒数）

Grafana 上配告警：剩余 < 5 触发警示。

---

## 8. 可观测性

每次 chat 调用记录：

```python
{
  "ts": "2026-05-14T12:00:00Z",
  "trace_id": "diag-1234",
  "provider": "claude_code",
  "model": "claude-sonnet-4-x",
  "role": "chat",                    # chat | navigator
  "messages_count": 12,
  "tools_count": 5,
  "input_tokens": 4523,
  "output_tokens": 312,
  "cache_local_hit": false,
  "latency_ms": 2340,
  "cost_usd_est": 0.0,               # claude_code provider 实际计入订阅消息数，不是 token 计费
  "subscription_messages_used": 1,
  "error": null
}
```

Prometheus metrics：
- `llm_request_total{provider, model, role, status}`
- `llm_tokens_total{type=input|output, provider, model}`
- `llm_latency_seconds{provider, model, role}`
- `llm_local_cache_hit_total{provider, model}`
- `llm_subscription_messages_used_total{provider}`
- `llm_errors_total{provider, code}`

---

## 9. 测试策略

### 9.1 单元

- Mock `LLMProvider` 返回固定响应
- 测试上层业务逻辑（不依赖真实 backend）

### 9.2 Smoke

每个 backend 一组最小用例：

```
tests/llm/smoke/test_<provider>.py
  - test_chat_basic
  - test_chat_with_tools
  - test_chat_streaming
  - test_chat_json_schema_output
  - test_error_handling
  - test_rate_limit_simulation
```

CI 中：
- 跑 mock + ollama（如 CI runner 有 ollama）
- claude_code smoke：每周手工跑（不在 CI，避免消耗订阅）

### 9.3 一致性（v1.3）

同一 query × 5 backend × 30 例 → 输出对照表，看 Claude API vs vLLM 本地差距。

---

## 10. v1.0 → v1.3 路径切换演练

| 阶段 | backend | 切换动作 |
|------|---------|---------|
| v1.0 MVP（今天 → 6/15）| `claude_code` (subprocess) | 默认 |
| v1.0 (6/15 后) | `claude_code` (agent_sdk) | 改 `configs/default.yaml` 中 `adapter: agent_sdk`；测一遍 smoke；无业务代码改动 |
| v1.2 评测 | 同上 | 评测脚本带 `metadata.eval=true` 绕过缓存 |
| v1.3 本地化 | `vllm` | 起 vllm serve；改 `configs/production.yaml` 中 `backend: vllm` + endpoint；smoke + 30 例 A/B |

---

## 11. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| Pro 订阅 5h 上限撞墙 | `llm_rate_budget_remaining` < 5 告警 | PG 缓存优先；评测分多窗口跑；切 Max |
| `claude -p` 输出 schema 偶发不一致 | smoke test 监控 schema parse 错误率 | Provider 内置兜底 + raw_response 留痕 |
| 6/15 切 Agent SDK 时行为差异 | 切换前后跑同样 30 例 smoke | 不通过则 hold |
| Tool calling 在 OpenAI Chat ↔ Anthropic Messages 翻译时丢字段 | smoke test 覆盖 tool use 全场景 | 翻译层单测 + lint |
| context window auto-compaction 截断关键证据 | 监控 chat 请求 input_tokens 上限 | 业务层按 60K budget 主动 chunk |
| 并发 > 4 触发 backend 限制 | `llm_errors_total{code="concurrency"}` | provider 内 semaphore 严格限 |

---

## 12. 文件结构

```
llm/
├── __init__.py
├── provider/
│   ├── __init__.py
│   ├── base.py                      # LLMProvider Protocol, ChatRequest/Response
│   ├── claude_code.py               # ★ MVP 默认，含 subprocess + agent_sdk 双 adapter
│   ├── anthropic.py                 # 独立 API key 路径
│   ├── openai_compat.py             # OpenAI/DeepSeek/Moonshot/...
│   ├── vllm.py                      # 占位，v1.3 实跑
│   ├── sglang.py                    # 占位
│   ├── ollama.py
│   └── translation/
│       ├── openai_to_anthropic.py   # schema 翻译（claude_code + anthropic 复用）
│       └── stream_parser.py         # stream-json / SSE 解析
├── cache/
│   ├── __init__.py
│   ├── pg_cache.py
│   └── migrations/
├── rate_limit/
│   ├── __init__.py
│   └── sliding_window.py
├── observability/
│   ├── __init__.py
│   ├── logger.py
│   └── prometheus.py
└── tests/
    ├── unit/
    ├── smoke/
    └── fixtures/
```

---

## 13. 后续 backlog（v1.1+）

- `cache_control` 字段在 anthropic provider 启用，按 ADR-005（待写）规则自动插 breakpoint
- Provider 的"分时回退"：5h 窗口快撞墙时自动切到次优 backend（如本地 ollama 轻量小模型）
- 调用日志 PII 脱敏（如果用户输入 dmesg 含 hostname）
- Self-Consistency K 值与订阅档自动联动（Max 5x 自动开 K=3）
- 6/15 切 Agent SDK 后，评估是否启用其 Managed Agents（持久化会话）以减少消息消耗

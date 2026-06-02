# M1 · LLM Provider

## 1. 目的

把"调一次 LLM"抽象成一个**统一接口** `LLMProvider`，让上游（query parser / ReAct loop / reranker / claim binder）不关心背后是 OpenAI / Anthropic / vLLM / DeepSeek 还是 Claude Code subprocess。

**WHY 不直接用 `openai` SDK**：
1. Pro 订阅的 Claude Code（subprocess `claude -p`）签名完全不同，必须 adapt
2. 需要**全局 rate-limit / PG cache / 重试**逻辑跨 backend 共享
3. 需要**角色级配置**（chat / navigator 两个角色，不同 backend / 模型 / rpm）

## 2. 公共接口

```python
class LLMProvider(Protocol):
    provider_name: str

    def chat(self, req: ChatRequest) -> ChatResponse: ...

    def chat_stream(self, req: ChatRequest) -> Iterator[StreamEvent]:
        """Optional. 默认实现：fallback 到 chat() 同步调用。"""

    def health_check(self) -> bool:
        """ping endpoint 一次确认可达。"""


@dataclass
class ChatRequest:
    messages: list[Message]
    model: str = ""                  # 空字符串 → 用 provider 默认
    tools: list[ToolSchema] | None = None
    tool_choice: Literal["auto","required","none"] | dict | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    stream: bool = True
    response_format: dict | None = None        # {"type": "json_object"} etc.
    metadata: dict = field(default_factory=dict)  # trace_id, sop_id, ...

@dataclass
class Message:
    role: Literal["system","user","assistant","tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

@dataclass
class ToolCall:
    id: str                          # OpenAI-style 唯一 ID
    type: Literal["function"] = "function"
    function: dict                   # {"name": str, "arguments": str (JSON-encoded)}

@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: Usage
    finish_reason: Literal["stop","length","tool_calls","content_filter"]
    raw: dict                        # provider-specific 原始响应（debug 用）

@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0       # Anthropic prompt cache 命中字符数
    cache_write_tokens: int = 0
    cost_usd_est: float = 0.0
```

## 3. 数据流

```
   call site
   ───────────
   provider = get_provider("chat" | "navigator")  ← 角色 → backend 配置
   resp = provider.chat(req)
            │
            ↓
   ┌──── BaseProvider ────┐
   │ ① rate-limit gate    │ ← 端点级 sliding window（共享同 endpoint）
   │ ② PG cache lookup    │ ← key = (model, sha256(messages))，30 天 TTL
   │ ③ backend dispatch   │
   └─────────┬────────────┘
             ↓
   ┌─────────────────┐  ┌────────────────┐  ┌──────────────────┐
   │ claude_code     │  │ openai_compat  │  │ vllm             │
   │ (subprocess)    │  │ (httpx + SDK)  │  │ (openai-compat)  │
   └─────────────────┘  └────────────────┘  └──────────────────┘
             │
             ↓
   ④ retry on transient errors（429 / 5xx / timeout）
   ⑤ PG cache write
   ⑥ JSONL audit log → data/logs/<timestamp>.jsonl
             │
             ↓
   ChatResponse
```

## 4. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **角色独立配置**：`get_provider("chat")` 和 `get_provider("navigator")` 可以是**不同 backend 不同模型**。chat 是主体 ReAct loop；navigator 用于 query parser / rerank / fault classify 等低成本任务 | 主体推理可能用 Claude / GPT-4，navigator 用 DeepSeek 省钱 |
| C2 | **rate-limit 按 endpoint 共享**：DeepSeek `api.deepseek.com/v1` 不论是 chat 还是 navigator 角色都共享一个 sliding-window counter | 否则总速率超过 vendor RPM |
| C3 | **`temperature=0.0` 默认**：所有调用确定性，让 PG cache 可命中 | 评测 + 调试需要可重现 |
| C4 | **`max_tokens` 必须有上限**：默认 4096；不设上限时 OpenRouter / DeepSeek 按模型完整输出预算预扣信用（65K+），余额不足会 402 | 见 `60_GOTCHAS.md` §4.3 |
| C5 | **`trust_env=False` 必须强制**：httpx client 不要走主机 HTTPS_PROXY | 见 `60_GOTCHAS.md` §4.2 |
| C6 | **SDK auto-retry 必须关**：`openai.OpenAI(..., max_retries=0)`；应用层有自己的限流感知重试 | SDK 默认 retry 不感知 rate-limit，会浪费配额 |
| C7 | **PG cache key = (model, sha256(messages))**：metadata 不进 key | 让缓存能跨调用复用（仅依赖语义） |
| C8 | **cache TTL = 30 天**；通过 metadata 加 `force_fresh` / `eval` 字段可绕过 | 评测时要 fresh 调用，省 Pro 配额 |

## 5. 三个 backend 详细契约

### 5.1 `claude_code` backend

- 实现：subprocess 调用 `claude -p` CLI，stdin 喂 messages（JSON），解析 stream-json 输出
- 鉴权：复用宿主 Claude Code OAuth session（无需 API key）
- 限流：Pro 订阅 ~45 msg / 5h；保守配置 40 msg / 5h
- 用途：本地开发 / Pro 用户

### 5.2 `openai_compat` backend

- 实现：`openai.OpenAI(base_url, api_key, http_client=httpx.Client(trust_env=False))`
- API key 优先级：`OPENAI_API_KEY` env > `OPENAI_COMPAT_API_KEY` env > `cfg.llm.endpoints.api_key` > `"none"`
- 覆盖：OpenAI 官方 / OpenRouter / DeepSeek 官方 / Moonshot / 智谱 / 百炼 / vLLM / ollama
- 用途：生产推荐路径

### 5.3 `vllm` backend

- 继承 `OpenAICompatProvider`，但允许 `api_key="none"`
- endpoint 通常是 `http://host:8000/v1`
- 用途：自部署 / 私有部署

## 6. 已知陷阱（M1 独有）

### #1 · `chat_stream` 必须把 tool_call fragment 合并

OpenAI stream 把 tool call 拆成多个 chunk：
- 第 1 chunk: `{"index": 0, "id": "call_xxx", "function": {"name": "search_commits"}}`
- 第 2+ chunk: `{"index": 0, "function": {"arguments": "{\"key"}}`
- 第 3 chunk: `{"index": 0, "function": {"arguments": "words\": ..."}}`

实现要按 `index` 累加 `function.arguments` 字符串，直到完整再 emit。否则下游 `json.loads(arguments)` 失败。

### #2 · `chat()` 默认实现走 `list(chat_stream(req))`

非 streaming 调用者用 `chat()`。如果 backend 只实现了 `chat_stream`，默认 `chat()` 做 `"".join(c.content for c in chat_stream(...))` 并把 tool_calls 合并。**响应中的 finish_reason 取最后一个 chunk 的值。**

### #3 · `claude_code` subprocess 启动慢（500-800ms cold start）

不要每次 `chat()` 都新起 subprocess。`ClaudeCodeProvider` 应在构造时启动一个 persistent process，保持 stdin/stdout 流。**关闭时正确清理子进程**避免僵尸。

> **see also**：`60_GOTCHAS.md` §4.1 (DSML 幻觉) / §4.2 (proxy) / §4.3 (max_tokens)

## 7. 验收

### 输入：6 个测试场景

```python
# 1. chat 角色基础调用
provider = get_provider("chat")
resp = provider.chat(ChatRequest(messages=[Message(role="user", content="ping")]))
assert resp.content
assert resp.usage.input_tokens > 0

# 2. tool calling
resp = provider.chat(ChatRequest(
    messages=[Message(role="user", content="search for memcg fixes")],
    tools=[ToolSchema(function=ToolFunction(name="search_commits", ...))],
    tool_choice="auto",
))
assert resp.tool_calls or resp.content

# 3. tool_choice="none" 不许调工具
resp = provider.chat(ChatRequest(..., tool_choice="none"))
assert not resp.tool_calls

# 4. cache 命中
resp1 = provider.chat(req)
resp2 = provider.chat(req)  # 同样的 req
# resp2 应来自 cache（实现细节：检查 raw['cached'] == True，或观察 latency < 100ms）

# 5. rate limit
# 连续 50 次调用，应在第 ~40 次后开始排队（DeepSeek 默认 600 rpm 不会触发）

# 6. 错误：endpoint 不可达
provider._base_url = "http://invalid:9999"
with pytest.raises(LLMProviderError):
    provider.chat(req)
```

### 自动验收

```
python 50_TEST_FIXTURES/run_acceptance.py --module M1
```

脚本应：
1. 构造 minimal mock provider
2. 跑上面 6 个场景断言
3. 检查 PG `llm_response_cache` 表插入了至少 1 行

## 8. 上下游

| 关系 | 模块 / 文件 |
|---|---|
| **被调用** | M5 Retrieval（query parser + rerank）/ M7 Agent（fault classify + ReAct loop + bind_claims）/ M8 Eval（LLM judge）|
| **依赖** | M2 Storage（`llm_response_cache` 表用于 PG cache） |
| **配置 keys** | `llm.chat.{backend,model,...}` / `llm.navigator.{backend,model,...}` / `llm.endpoints.{openai_compat,api_key,...}` — 完整 schema 见 `70_OPERATIONAL.md` |
| **重要 ADR** | ADR-004（三 backend 选型 + claude_code subprocess 决策） |

---

> **重建校对**：实现完 M1 后跑 `run_acceptance.py --module M1`；grep 你的实现确认 C4 (max_tokens=4000 默认) / C5 (trust_env=False) / C6 (max_retries=0) 三条硬约束都有代码体现。

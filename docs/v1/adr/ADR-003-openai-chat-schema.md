# ADR-003 — 内部统一 Schema = OpenAI Chat Completions

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M1 |
| 相关 ADR | ADR-004（claude_code provider） |

## 上下文

M1 LLM Provider 抽象层需要一种内部统一 schema 表示 chat 请求与响应，使具体 backend 可以通过配置切换。候选 schema：

1. **OpenAI Chat Completions schema**（`messages: [{role, content, tool_calls?, tool_call_id?}]`）
2. Anthropic Messages schema（`system: ..., messages: [{role, content: [...]}]`）
3. 自定义 minimal schema

## 决策

**采用 OpenAI Chat Completions schema 作为业务层与 Provider 抽象层之间的内部统一接口**。

具体表现：
- 业务层代码只构造和消费 OpenAI Chat schema 的 `Message` / `ChatRequest` / `ChatResponse`
- 每个 Provider 实现内部做 schema 翻译（如有必要），不向上暴露差异
- 扩展字段：增加 `cache_control`（映射 Anthropic Prompt Caching）和 `metadata`（trace_id / sop_id 等），其他 backend 静默忽略

## 影响

| 维度 | 影响 |
|------|------|
| 生态兼容 | vLLM / sglang / Ollama / OpenAI / DeepSeek / Moonshot / 智谱 / 阿里百炼 等都**原生 OpenAI Chat 兼容**，零翻译 |
| 适配工作 | 仅 `claude_code` 与 `anthropic` 两个 provider 需做 OpenAI Chat ↔ Anthropic Messages 翻译 |
| 翻译要点 | system 消息抽到 Anthropic top-level；OpenAI tools schema 转 Anthropic tools；OpenAI `tool_calls` 响应映射回 Anthropic `tool_use` blocks |
| 下游生态 | LangGraph / LangChain / LlamaIndex / liteLLM 都原生支持 OpenAI Chat schema，未来可直接接入 |
| 风险 | OpenAI 兼容 backend 在 tool use schema 上偶有微差异（特别是部分国产 provider），需 smoke test 覆盖 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| Anthropic Messages schema | MVP 默认走 Claude，但其他所有 backend（vLLM / OpenAI 兼容）都要反向适配，工作量更大；下游生态不兼容 |
| 自定义 minimal schema | 表面"最中立"，实际两端都要翻译，且 LangGraph 等生态无法直接复用 |

## 关键设计细节

### 不复用 Anthropic 官方 OpenAI 兼容层

Anthropic 提供 `https://api.anthropic.com/v1/` 的 OpenAI SDK 兼容入口，但官方文档明确说明：

- Tool use JSON 不保证遵循 schema（不可靠）
- 所有 system 消息被串联成一条（语义损失）
- 仅供测试/评估使用，**生产不推荐**

因此我们的 `anthropic` 和 `claude_code` provider 都**自建薄翻译层**，不走官方 compat。

### `cache_control` 字段处理

扩展字段 `Message.content[].cache_control: {"type": "ephemeral"}` 仅对 `anthropic` provider 有意义。其他 provider（包括 `claude_code` MVP 阶段）静默忽略。这是为未来切到独立 API key 时启用 Prompt Caching 预留。

## 参考

- OpenAI Chat Completions API：https://platform.openai.com/docs/api-reference/chat
- Anthropic Messages API：https://docs.anthropic.com/en/api/messages
- Anthropic OpenAI Compat 已知问题：https://platform.claude.com/docs/en/api/openai-sdk

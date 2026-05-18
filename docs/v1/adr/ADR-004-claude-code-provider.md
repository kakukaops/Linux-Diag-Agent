# ADR-004 — MVP 默认 Backend = `claude_code` Provider，分阶段升级路径

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M1, M5 |
| 相关 ADR | ADR-003（OpenAI Chat schema） |

## 上下文

用户当前用 Claude Pro 订阅 ($20/月) + Claude Code CLI，**没有独立的 Anthropic API key**。希望 MVP / 测试阶段直接复用 Claude Code 的 Claude 访问能力，避免单独付费购买 API。

调研当前（2026-05-14）可行路径：

| 路径 | 今天可用 | 备注 |
|------|---------|------|
| `claude -p` headless 模式 + subprocess 调用 | ✅ | 支持 streaming / JSON / `--json-schema` / `--allowedTools`；继承 Claude Code OAuth |
| Claude Agent SDK Python + Pro 订阅 | ❌（2026-06-15 后官方支持）| 当前 SDK 仍要 ANTHROPIC_API_KEY |
| Claude Agent SDK + 独立 API key | ✅ | 用户无 API key，不适用 |
| Anthropic 官方 SDK | ✅ | 需独立 API key，不适用 |

Pro 订阅限制：**~45 messages / 5h window**，相对紧（Self-Consistency K=3 = 每复杂诊断 3+ messages，5h 内仅能跑 ~15 个完整诊断）。

## 决策

**MVP 阶段引入 `claude_code` provider 作为默认 backend，分两阶段实现**：

**阶段 A（2026-05-14 起 → 6/15）：subprocess adapter**

- 包 `claude -p --output-format stream-json --verbose --include-partial-messages --bare`
- 内部做 OpenAI Chat schema ↔ Anthropic Messages 翻译
- 工具：OpenAI `tools` → Anthropic `tools` → `--allowedTools "Tool1,Tool2"`
- 认证：继承 Claude Code 已登录的 OAuth
- 输出：解析 stream-json 事件流，映射回 OpenAI Chat ChatResponse

**阶段 B（2026-06-15 后）：agent_sdk adapter**

- 切到 `claude-agent-sdk` Python 包（官方支持 Pro/Max 订阅）
- 同样的 schema 翻译层（复用阶段 A 的 `llm/provider/translation/`）
- 业务代码零改动，只改 `configs/default.yaml` 中 `claude_code.adapter` 从 `subprocess` 改为 `agent_sdk`

**Self-Consistency K 默认 = 1**（评测阶段按需开 K=3），并发上限 = 4，rate limit 监控按 40 messages / 5h 阈值（留 5 buffer）。

## 影响

| 维度 | 影响 |
|------|------|
| MVP 成本 | $20/月（用户已有 Pro 订阅），无额外 API 支出 |
| 评测吞吐 | 30 例评测每例 ~8 messages → ~240 messages → 跨 7 个 5h 窗口 → **30 例至少 7 天跑完**（M9 排期纳入） |
| 切到独立 API key | 改 `backend: anthropic` + 设 `ANTHROPIC_API_KEY` 环境变量；零代码改动 |
| 切到本地 vLLM（v1.3）| 改 `backend: vllm` + endpoint；零代码改动 |
| Prompt Caching | `claude -p` 不暴露 `cache_control`，MVP 不启用；6/15 切 Agent SDK 后再评估 |
| 上下文 | Auto-compaction 200K → 实际可用 ~60-80K；业务层按 60K 主动 chunk |
| Tool use | 通过 `--allowedTools` 预审批 MCP tool name；schema 经翻译层下发 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| 等到 6/15 后直接用 Agent SDK | 阻塞 MVP 启动一个月，且 6/15 仅是预期日期，可能延期 |
| 让用户单独付费购买 API key | 用户明确不想；且 Pro 订阅是已沉没成本，复用合理 |
| 业务代码直接调 `claude -p` 不做抽象 | 与 ADR-003 OpenAI Chat schema 统一接口矛盾；切其他 backend 时业务代码大改 |
| 用 liteLLM / openrouter 中间层 | 第三方，增加供应链风险；不解决 Pro 订阅复用问题 |

## 风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| Pro 5h 窗口撞墙 | `llm_rate_budget_remaining{provider="claude_code"} < 5` 告警 | 评测分多窗口跑；考虑升级 Max 5x |
| `claude -p` 输出 schema 偶发变动 | 每周 smoke test | Provider 内置 fallback + raw_response 留痕 |
| 6/15 切 Agent SDK 时行为差异 | 切换前后跑同样 30 例 smoke 对照 | 不通过则保持 subprocess 模式 |
| 并发 > 4 触发 backend 限流 | `llm_errors_total{code="concurrency"}` | provider 内 asyncio.Semaphore 严格限 |
| Auto-compaction 截断关键证据 | input_tokens > 50K 告警 | 业务层主动 chunk + 关键证据置顶 |

## 参考

- Claude Code Headless 模式：https://code.claude.com/docs/en/headless
- Claude Agent SDK 订阅支持（June 15, 2026 起）：https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Anthropic Rate Limits：https://platform.claude.com/docs/en/api/rate-limits

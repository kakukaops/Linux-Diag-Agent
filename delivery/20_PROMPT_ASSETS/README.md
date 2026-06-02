# 20 · Prompt Assets

> **重要**：本目录下的 `.md` 文件**不是文档**，而是**系统资产**。AI agent 重建时应**原样拷贝**，**不要重写或翻译**。

每个 prompt 是一段精心打磨的"diagnostic protocol"，灌输给 LLM 让它按 Phase 0-6 流程思考。改一个标点都可能改变 agent 行为。

## 文件清单

| 文件 | 路由 | 占位符 | 说明 |
|---|---|---|---|
| `kernel.md` | `kernel` | `{kernel_version}` / `{olk_version_tag}` / `{fault_kind}` | 主路由 prompt（英文），最复杂；含 Phase 0-6 协议 + Run-11 anti-patterns |
| `kernel.zh.md` | `kernel` (lang=zh) | 同上 | 中文版（标题也中文化，正文 narrative 中文，函数名/CVE-ID/术语保留英文原文）|
| `kernel_vmcore.md` / `.zh.md` | `kernel+vmcore` | 同上 | vmcore 取证路由 |
| `hardware.md` / `.zh.md` | `hardware` | 同上 | 硬件故障路由（MCE/EDAC/IOMMU） |
| `change.md` / `.zh.md` | `change` | 同上 | 变更关联（regression）路由 |
| `unknown.md` / `.zh.md` | `unknown` | 同上 | 兜底路由 |

## 占位符渲染

Python 重建参考形态（其它语言等价）：

```python
template = (DELIVERY_ROOT / "20_PROMPT_ASSETS" / fname).read_text(encoding="utf-8")
body = template.format(
    kernel_version=triage_state.get("kernel_version") or "unknown",
    olk_version_tag=triage_state.get("olk_version_tag") or "unknown",
    fault_kind=triage_state.get("fault_kind") or "unknown",
)
```

## TERMINATION_FOOTER（en + zh）

5 个路由 prompt 之外，**所有路由共享**一段强制终止规则：假设枚举 + 自我批驳 + 证据 trace 不许幻觉 + 终止规则。完整文本见 `40_BEHAVIORAL_CONTRACTS.md` § 完整 footer。

渲染顺序：
```
system_prompt = route_template(填占位符)
              + io_hang_alert(state, lang)      # 仅当 dmesg 检测到 IO 挂起信号
              + TERMINATION_FOOTER(lang)         # 始终追加
              + ZH_FALLBACK_DIRECTIVE(lang)      # 仅当 lang=zh 且 该 route 缺 .zh.md 时
```

## 语言选择规则

```
lang=zh + 有 .zh.md → 用 .zh.md
lang=zh + 无 .zh.md → 用 .md（英文）+ ZH_FALLBACK_DIRECTIVE
lang=en           → 用 .md
```

## 重建时的红线

- **不要翻译**：英文 prompt 翻成中文（或反之）会改变 LLM 的概念关联
- **不要"优化"**：删 anti-pattern 段、删 critique 强制要求等
- **不要合并**：5 个路由分开是有意的——route 路由决定哪些工具暴露给 LLM，prompt 要匹配
- **不要换标题层级**：`## Root Cause` / `## 根本原因` 等是 Phase 6 output format 的硬契约，下游 bind_claims 可能基于此抽取
- 占位符 `{kernel_version}` 等若用其它语言（如 Jinja `{{ }}` / Handlebars），要保持单大括号语义不变

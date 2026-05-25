# ADR-023 — Hardware-first SOP 路由：先检查 taint 和 Hardware Error，再决定是否走内核路径

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-20 |
| 决策者 | 用户 + Architect |
| 关联模块 | M7（agent/triage）· 新 M11（Hardware Layer）|
| 相关 ADR | [ADR-019](ADR-019-hybrid-react-deterministic.md) |
| 设计基础 | [AgentThink §四 + §H](../../AgentThink.md) |

## 上下文

v1 的 SOP 路由是 `_EVENT_KIND_TO_SOP`（`agent/triage/nodes.py`）：

```python
_EVENT_KIND_TO_SOP = {
    "panic": "panic",
    "oom": "oom",
    "softlockup": "lockup",
    "hardlockup": "lockup",    # ← 问题
    "rcu_stall": "lockup",
    "oops": "generic",
    ...
}
```

这是**只看 fault_kind**的纯文本路由。问题在于：

| fault_kind | 表面看 | 实际常见原因 |
|-----------|--------|------------|
| softlockup | 软件锁/调度问题 | 常常是软件 |
| **hardlockup** | NMI watchdog 检测到 CPU 完全卡死 | **常常是硬件**（坏 DIMM / MCE / 微码 / 固件） |
| panic | 内核致命错误 | 软件多但硬件也常见 |

v1 把 hardlockup 路由到 `lockup` SOP → "找历史上修过 lockup 的 kernel commit"——这对硬件故障是**系统性误诊**。一台坏内存条引起的 hardlockup，在 v1 流程下会被引导去看"linux-mm 的最近 commit"，而不是去看 mcelog / IPMI SEL。

## 决策

**v2 的 triage 阶段引入两阶段路由**：

### 阶段 1：硬件信号检测（新节点 `detect_taint_and_hw_signals`）

扫描以下证据：

| 信号 | 来源 | 含义 |
|------|------|------|
| `taint=M` / `Tainted: M` | dmesg / `/proc/sys/kernel/tainted` | Machine Check Exception 发生过 |
| `Hardware Error` | dmesg | 硬件错误事件 |
| `mce` / `MCE` event | dmesg | Machine Check Exception |
| `EDAC` / `Uncorrected Error` | dmesg | 内存 ECC 不可纠错错误 |
| `MCA` / `mca` | dmesg | Machine Check Architecture |

state 中新增字段：

```python
state["has_hardware_signal"]: bool
state["hardware_signals"]: list[str]  # 具体哪些标记
state["taint_flags"]: list[str]       # 解析后的 taint 位
```

### 阶段 2：路由决策（重构 `classify_fault_and_route`）

```python
if state["has_hardware_signal"]:
    route = "hardware"          # → 类别 H 工具集
elif fault_kind in {"panic", "oops"} and has_vmcore_available:
    route = "kernel+vmcore"     # → 类别 F drgn 工具集
elif user_implies_recent_change():  # 关键词："升级"、"昨天"
    route = "change"            # → 类别 G 变更工具集
else:
    route = "kernel"            # → 类别 A/D 知识图谱
```

### 阶段 3：硬件 SOP 加入

新增 `agent/sop/definitions/hardware.yaml`：

```yaml
name: hardware
description: 疑似硬件故障的诊断 SOP（不去查 kernel commit）
fault_kinds: [hardlockup, panic_with_hw_error, mce]

steps:
  - "解析 mcelog / EDAC / IPMI SEL，定位错误源（CPU / DIMM / Bus）"
  - "查 dmidecode 拿 DIMM 槽位、固件版本、BMC 版本"
  - "对比 IPMI SEL 历史事件，判断是新发还是已知"
  - "查 ras-mc-ctl --summary 看 ECC 累积"
  - "结论：是否硬件故障？建议 RAS 检查 / RMA / 微码升级"

report_sections:
  - hardware_signals_summary
  - error_source_identification
  - recommended_actions
```

报告**明确不引用 kernel commit**（即使 ReAct 路径中调用了 search_commits 也只是辅助）。

> **注**：本 ADR 只规定 hardware SOP 的存在和路由触发条件；完整 SOP 内容（MCE 错误码映射表、IPMI SEL 解释规则、各厂商 BMC 差异处理）放在 **M11 module 详设**中定义（详见 v2 模块文档 [modules/M11_hardware_layer.md](../modules/M11_hardware_layer.md)）。

## 影响

### 优势

| 维度 | 影响 |
|------|------|
| 避免系统性误诊 | hardlockup / panic 中真正是硬件的案例不再被引导去看 kernel commit |
| 工具集精准 | hardware 路由只挂类别 H 工具，ReAct 不会浪费 budget 在 search_lkml 上 |
| 报告可信度 | "疑似硬件故障 → 建议 RAS 检查"比"找到一个相关 kernel commit"更有可操作性 |

### 代价

| 维度 | 影响 |
|------|------|
| 路由复杂度 | 从单层 fault_kind 映射变成两阶段；triage 多 1-2 节点 |
| 误判风险 | 软件 bug 触发了 hw signal（罕见）会被错误路由到 hw；缓解：hardware SOP 仍保留 fallback 到 kernel 路径 |
| 真实数据集需覆盖 | eval 数据集里必须有 hw 误诊案例 + hw-but-actually-software 反例 |

## 路由优先级

当多种信号同时出现，优先级（高 → 低）：

1. `hardware`（hw signal 触发，优先 short-circuit）
2. `kernel+vmcore`（有 vmcore 文件）
3. `change`（用户问题含变更关键词）
4. `kernel`（默认）

`unknown` 路由保留作为 fallback（暴露全工具集，让 ReAct 自己决定）。

## 检测规则的边界

**hw signal 检测必须保守**——宁可漏路由（fallback 到 kernel）也不要错路由（把软件 bug 路由到 hw）。规则：

- 必须看到**强信号**（taint=M / Hardware Error / MCE event）才路由到 hardware
- 弱信号（"slow disk"、"high temp" 等）不触发 hardware 路由
- 详细 taint 位映射表维护在 `agent/triage/taint_flags.py`

## 备选方案（已否决）

### 备选 A：保持单层 fault_kind 路由

否决理由：实测 v1 路由把 hardlockup 误指 kernel，已经是 bug，必须修。

### 备选 B：每个 fault_kind 都加 hw 子检测

否决理由：过度复杂；只有 hardlockup/panic 是 hw vs sw 模糊地带，其他 fault_kind（如 OOM）不需要。

### 备选 C：ReAct 阶段让 LLM 自己决定路由（不在 triage 路由）

否决理由：把硬件 vs 内核的高优判断交给 LLM 风险大；triage 强制路由是确定性的（[ADR-019](ADR-019-hybrid-react-deterministic.md) 确定性骨架）。

## 验证

- v2.0 acceptance：dmesg 含 `Hardware Error` 的案例 100% 不再去搜 kernel commit（[PRD §6.1](../PRD.md)）
- eval 数据集**必须**包含：
  - ≥ 3 例 hw 误诊案例（dmesg 有 hw signal，曾被 v1 误指 kernel）
  - ≥ 2 例 "hw signal 但实际是软件" 反例（防止路由过激）

## 未来回顾

- 跟踪 hw 路由召回率 / 准确率
- 如出现"hw signal 但实际是软件"被频繁误判，调整规则灵敏度或加 LLM cross-check

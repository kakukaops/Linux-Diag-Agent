# ADR-020 — vmcore 分析采用 drgn 而非 crash

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-20 |
| 决策者 | 用户 + Architect |
| 关联模块 | 新 M9（Crash Forensics）|
| 相关 ADR | [ADR-019](ADR-019-hybrid-react-deterministic.md) |
| 设计基础 | [AgentThink §F](../../AgentThink.md) |

## 上下文

v1 完全没有内核崩溃后取证能力。对一个"内核"故障 agent 来说，这是脊梁缺失——panic / hardlockup 的标准排障动作是：

```
panic → kdump 抓 vmcore → 分析 vmcore → bt -a / log / kmem
```

v2 必须补这一块（[AgentThink §F](../../AgentThink.md)，P0/P1 优先级）。问题是：选 `crash` 还是 `drgn`？

| 工具 | 来源 | 形态 | 历史 |
|------|------|------|------|
| `crash` | Red Hat | 交互式 prompt（C 实现）| 标准内核调试器，30+ 年历史 |
| `drgn` | Meta（Omar Sandoval）开源 | **Python 库 + 命令行** | 较新（2020+），活跃开发 |

业界 SRE 团队（Meta、Cloudflare、netflix）越来越多地从 `crash` 迁移到 `drgn`，因为 `drgn` 是为自动化场景设计的。

## 决策

**v2 的 vmcore 分析工具（类别 F，M9）采用 `drgn` 作为主力**：

```python
import drgn
from drgn.helpers.linux import find_task, for_each_task

prog = drgn.program_from_core_dump("/var/crash/vmcore")
# 编程化遍历所有 task 找 D 状态
for task in for_each_task(prog):
    if task.state.value_() & 0x2:  # TASK_UNINTERRUPTIBLE
        ...
```

对 LLM agent 暴露的工具不直接驱动 `drgn` REPL，而是封装为**预定义脚本库**（M9 提供 `analyze_vmcore(query)` 的几种 query mode：`all_stacks` / `locks` / `oom_context` / `network_state`）。

## 影响

### 优势

| 维度 | drgn vs crash |
|------|---------------|
| **可编程性** | Python 库，可被 agent 直接调用；返回结构化数据（dict/dataclass）。`crash` 是交互式 prompt，需要 expect / pty 解析输出 |
| **可组合** | drgn helpers 是模块化 Python 函数，组合写自定义脚本几行代码；`crash` 的扩展靠 `extend` C 模块编译 |
| **错误处理** | Python 异常天然集成 try/except；`crash` 命令失败的结构化捕获难 |
| **类型安全** | drgn 的对象 `task.pid.value_()` 带类型；`crash` 输出全是文本 |
| **测试** | drgn 脚本可单元测试；`crash` 几乎只能集成测试 |
| **多版本兼容** | drgn 用 DWARF 调试信息，跨版本一致；`crash` 跨 RHEL/SUSE 差异大 |

### 代价

| 维度 | 影响 |
|------|------|
| **生态成熟度** | `crash` 30 年生态、社区脚本多；drgn 相对新，需要团队自学 |
| **覆盖范围** | 个别冷门命令（如 `irq`、`fuser`）drgn helpers 尚未覆盖，需要自己写 |
| **OLK 验证** | OLK 自己有没有验证过 drgn？需要 spike 确认（见下）|

## OLK 兼容性确认（spike 必做）

M14 启动前 1 周做 spike，验证：

1. drgn pip install + OLK Python 3.x 兼容
2. drgn 能加载 OLK kernel-debuginfo 的 vmlinux + vmcore
3. 对 OLK-6.6 / OLK-5.10 两个版本都验证（至少各 1 个 vmcore 案例）

若 drgn 在 OLK 上确有坑，降级方案：drgn 主力 + crash 作为 fallback。

## 工具封装设计

M9 对 ReAct loop 暴露的不是 drgn 原始 API，而是高层语义化工具：

| LLM 工具 | drgn 实现要点 |
|----------|--------------|
| `analyze_vmcore(vmcore_path, query="all_stacks")` | `for_each_task` + bt 各 CPU |
| `analyze_vmcore(vmcore_path, query="locks")` | 扫所有 mutex/spinlock，找 owner |
| `analyze_vmcore(vmcore_path, query="oom_context")` | 提取 oom_kill_process 调用栈 + cgroup 状态 |
| `decode_stacktrace(raw_trace, kernel_version)` | 封装 `decode_stacktrace.sh`（不用 drgn）|
| `parse_taint_flags(taint_value)` | 位映射查表 |
| `check_kdump_available(node)` | SSH 远程检查 `kdump.service` 状态 |
| `fetch_debuginfo(kernel_version)` | dnf download --source kernel-debuginfo + sosreport 包内 |

**关键设计原则**：LLM 不直接写 drgn 脚本。M9 维护一个 `query="..."` 的封闭枚举，新 query mode 由 M9 维护者添加。这避免 LLM 写 Python 代码注入风险，也便于审计。

## 备选方案（已否决）

### 备选 A：以 `crash` 为主

- **优势**：成熟、社区广
- **劣势**：交互式 prompt 难驱动 agent；输出解析脆弱；Python 集成差

否决：[AgentThink §F](../../AgentThink.md) 明确推荐 drgn 因其契合 agent 架构。

### 备选 B：让 LLM 直接写 drgn Python 脚本并执行

- **优势**：灵活
- **劣势**：代码注入风险；难审计；调试痛苦

否决：安全和可审计性优先于灵活性。

### 备选 C：自研 vmcore 解析

- **优势**：完全可控
- **劣势**：重新发明轮子，与 v2 "买 L1 工具，不重造"的策略冲突（[AgentThink §5.1](../../AgentThink.md)）

否决。

## 验证

- M14 启动前 spike：drgn + OLK 兼容
- v2.0 acceptance：vmcore 案例 5 例端到端，含持锁 CPU 栈（[PRD §6.1](../PRD.md)）

## 未来回顾

- 若 drgn 在 OLK 上稳定性问题严重，考虑 crash fallback
- 跟踪 drgn 上游（Meta/Linux Plumbers）是否加 OLK-specific 支持

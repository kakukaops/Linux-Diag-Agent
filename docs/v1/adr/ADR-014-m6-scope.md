# ADR-014 — M6 主机工具集 v1.0 范围：仅文件上传 + 两工具 + 无 vmcore

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M6 |
| 相关 ADR | [ADR-008](ADR-008-reuse-codesearch.md) · [ADR-013](ADR-013-m5-retrieval-strategy.md) |

## 上下文

M6 主机 MCP 工具集需要决定 v1 scope。原蓝图涵盖 9 个工具，但经过逐项评估，部分工具复杂度 / 风险 / 价值不匹配 MVP 阶段。

主要权衡点：
1. **vmcore 分析（crash + drgn）**：诊断深度高，但需要 vmlinux+debuginfo、内存开销大、跨发行版工具链兼容性复杂、目标用户不一定有 vmcore 在手
2. **SSH 远程**：可拉实时状态，但凭据管理、网络隔离、权限边界复杂
3. **perf / ftrace 解析**：价值高，但 v1.0 主路径未必每个用户都有
4. **LKML / Bug raw access MCP**：与 M5 retrieve API 功能重叠

## 决策

**v1.0 M6 scope 简化为**：

| # | 决策 | 含义 |
|---|------|------|
| **D1** | 数据获取 = **仅文件上传** | 用户 CLI `--upload <kind>=<file>`；不接 SSH |
| **D2** | v1.0 工具 = **mcp-dmesg-journal + mcp-sosreport** | 文本日志 + 系统快照 |
| **D3** | **不做 vmcore / crash / drgn 工具**（v2 重启）| 暂不支持深内核态钻取 |
| **D4** | **不做 perf / ftrace MCP**（v1.1+ 评估）| MVP 不引入 |
| **D5** | LKML / Bug / commit / CVE **不单独建 MCP server**，由 M5 retrieve + 低阶 API 提供 | 减少进程数 |

## 影响

### 诊断能力

| 故障域 | v1.0 能否诊断 | 备注 |
|--------|------------|------|
| OOM | ✅ | dmesg OOM 抽取 + LKML 历史 + Bug 相似案例 |
| Soft/hard lockup | ✅ | dmesg lockup 报告 + 栈签名匹配 |
| 网络丢包 / 协议异常 | ✅ | dmesg + LKML 网络子系统讨论 |
| Deadlock | ✅ | dmesg lockdep 报告 |
| 内核 panic 根因（需查内核数据结构）| ❌ | 需 vmcore，v2 |
| 性能回归 | ⚠️ 部分 | 仅靠 dmesg / journal 信息，无 profile 数据 |

### MVP 范围

- 工具进程：2（vs 蓝图 9）
- 自研行数：~600（dmesg parser ~300 + sosreport extractor ~300）
- 部署复杂度：低（无 vmlinux 依赖、无 SSH 凭据、无 GPU）
- 评测样本筛选：30 例选**有完整 dmesg + 可选 sosreport** 的，与 v1.0 工具能力对齐

### 不能立即覆盖的真实场景

| 场景 | 临时方案 / 后续 |
|------|--------------|
| 用户已有 vmcore 但无 dmesg | v1.0 拒绝处理 + 提示导出 dmesg；v2 加 vmcore 工具 |
| 性能回归排查需要火焰图 | v1.1 加 mcp-perf |
| 需要查实时 /proc 状态 | v2 评估 SSH 远程 |
| 大量 ftrace 事件分析 | v1.2 加 mcp-ftrace（若评测显示需求） |

### 文档影响

- **PRD** 中 v1.0 故障域支持矩阵更新（删除 vmcore-dependent 项）
- **Architecture** §部署拓扑：移除 mcp-crash-drgn 等
- **KnowledgeGraph_Overview**：诊断能力章节相应调整
- **ProjectPlan**：M6 工时估算从 ~1500 行降到 ~600 行

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **D1 = 文件上传 + SSH 双模** | 凭据管理 + 网络隔离 + 权限边界复杂；v1.0 求简 |
| **D2 = 全 5 工具（dmesg/journal/sos/perf/ftrace）** | v1.0 工作量增 50%；perf/ftrace 在评测 30 例中未必都用到 |
| **D3 = 保留 vmcore 工具** | vmlinux+debuginfo 依赖；跨架构跨发行版 toolchain 复杂；MVP 资源不足 |
| **D5 = 单独 MCP server for LKML/Bug** | 与 M5 retrieve API 功能重叠；多进程 overhead；M7 调用范式不统一 |

## v1.1+ Review 触发条件

满足以下任一时，重评估本 ADR：

1. 30 例评测中 ≥ 5 例需要 vmcore 才能解决 → v2 重启 vmcore 工具优先级提升
2. ≥ 3 例需要 perf.data 火焰图分析 → v1.1 优先加 mcp-perf
3. 用户明确反馈"必须支持 SSH 远程"且能提供真实场景 → v1.2 评估 SSH 模式
4. 文件上传模式遇到大文件（> 5GB sosreport）处理不畅 → v1.2 加分块/流式上传

## 监控指标

- `m6_tool_invocations_total{tool}` — 每工具调用次数
- `m6_parse_failures_total{tool, error_type}` — 解析失败
- `m6_extract_duration_seconds{tool}` — 处理时长
- `eval_cases_vmcore_required_count` — 评测样本中需要 vmcore 的比例

## 参考

- M6 模块设计：[M6_host_mcp_tools.md](../modules/M6_host_mcp_tools.md)
- [ADR-008 codesearch 复用](ADR-008-reuse-codesearch.md)
- [ADR-013 M5 检索策略](ADR-013-m5-retrieval-strategy.md)

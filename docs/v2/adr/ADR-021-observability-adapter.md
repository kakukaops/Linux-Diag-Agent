# ADR-021 — 可观测性通过 Adapter 接入，不直接读 `/proc/`

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-20 |
| 决策者 | 用户 + Architect |
| 关联模块 | 新 M12（Observability Adapter）|
| 相关 ADR | — |
| 设计基础 | [AgentThink §C](../../AgentThink.md) |

## 上下文

v1 没有任何系统实时状态访问能力。v2 必须补这一块（[AgentThink §C](../../AgentThink.md)）。最直观的实现是让 agent 直接读 `/proc/`、执行 `ps`/`free`/`top`。但用户在设计阶段明确反对这条路径，原因：

| 反对 `/proc/` 直读的理由 | 详细 |
|--------------------------|------|
| **历史数据缺失** | `/proc/` 只有当前快照，OOM 常常是数小时泄漏后触发——"故障前 2 小时内存怎么变化"只有时序数据库能回答 |
| **多节点盲区** | 生产故障常跨节点；`/proc/` 只能看本机；外部监控天然有集群视角 |
| **数据已存在** | 企业生产环境几乎必有监控系统，按 15s/1min 粒度持续采集；Agent 重复采集是浪费 |
| **节点宕机后无法访问** | 崩溃后 `/proc/` 读不到，监控历史数据反而是唯一来源 |
| **与业界对齐** | Google SRE Agent 查 Cloud Logging + Cloud Monitoring，Azure SRE Agent 查 Azure Monitor，Datadog Bits AI 查自家 metrics/logs/traces，**无一读 `/proc/`** |

## 决策

**v2 采用 Adapter 模式接入外部监控系统**（详细接口语义见下方"接口语义统一原则"）：

```
        LLM Tool Layer
            ↓
  ObservabilityAdapter (Protocol)
        ├── query_metric(promql, time_range) → TimeSeries
        ├── search_logs(es_query, time_range, level) → LogEntries
        └── (traces v2 不做，留待 v3)
            ↓
   ┌───────────────┬──────────────┬─────────────┬──────────────┐
PrometheusAdapter  ES/Loki    HuatuoAdapter  DatadogAdapter
   直接 PromQL   直接 ES DSL    PromQL→huatuo  PromQL→Datadog
                 (LogQL 内翻译) ES DSL→huatuo  ES DSL→Datadog
                                内部翻译       内部翻译
```

**实施分阶段**：

- v2.1 实现 PrometheusAdapter + ElasticsearchAdapter（最常见的开源组合）
- v2.2 加 HuatuoAdapter + DatadogAdapter（同一份 PromQL/ES DSL 接口，adapter 内翻译）

## 重要限制：metrics 对内核瞬时故障无效

监控系统按 15s/1min 粒度采集，对**不同时间尺度**的故障有效性差异极大：

| 故障类型 | 时间尺度 | metrics 是否有效 |
|---------|---------|----------------|
| 缓慢泄漏型 OOM | 数小时 | ✅ 时序趋势是关键证据 |
| softlockup | 约 20 秒 | ⚠️ 15s 抓取只够 1-2 个数据点 |
| hardlockup / panic | 瞬时，然后节点失联 | ❌ metrics 完全无效，只能靠 vmcore + 日志 |

**结论**：监控系统擅长回答"慢"故障和"故障前系统在做什么"；内核瞬时故障的诊断脊梁仍是 vmcore + kernel log（类别 F）。Adapter 不是万能的，要按时间尺度选择性使用。

## 接口语义统一原则（关键决策）

确认（来自 [huatuo.tech](https://huatuo.tech/) 公开页面）：huatuo 是 **eBPF-based 内核观测平台**，提供 metrics / continuous profiling / traces / kernel events，覆盖内存/调度/网络/块 I/O，宣称 1% overhead。其对外查询接口的 PromQL 兼容性官网未明确文档化。

**Agent 暴露给 LLM 的工具接口统一**（与 backend 解耦）：

| 工具类别 | 统一输入语法 | 业界事实标准 |
|---------|------------|------------|
| **Metrics 类** | **PromQL** | 业界 metrics 查询事实标准 |
| **Logs 类** | **Elasticsearch Query DSL / Lucene** | 业界 logs 查询事实标准 |
| **Traces 类** | **v2 不做**，留待 v3 评估 | OpenTelemetry / Jaeger 生态待成熟 |

每个 adapter 内部负责把统一查询语法翻译到自家 backend：

| Adapter | Metrics（PromQL 输入） | Logs（ES DSL 输入） |
|---------|----------------------|---------------------|
| PrometheusAdapter | 直接转发 | 不适用 |
| ElasticsearchAdapter / Loki | 不适用（metrics 不走 ES）| 直接转发（Loki LogQL 内部翻译）|
| HuatuoAdapter | 若 huatuo 不原生支持 PromQL，adapter 内做查询翻译 | adapter 内翻译 |
| DatadogAdapter | 翻译为 Datadog Metrics Query | 翻译为 Datadog Logs Query |

**结果**：LLM 工具集对所有部署环境是同一份 schema，不需要为每个 backend 学不同语法；切 backend 改 yaml 配置即可。

**huatuo 不再被单独拔高**——它就是另一个 adapter，遵守同一份 PromQL/ES DSL 接口契约。如果 huatuo 的 eBPF 内核态优势需要被利用（如捕获 sub-scrape 事件），通过 adapter 内部增强 `query_metric` 的 PromQL 翻译来实现，而不是给 LLM 加新工具语法。

## 工具设计（M12 暴露给 LLM 的工具）

按"统一输入语法"原则，工具表只看输入语法和语义，不再按 backend 分列：

| 工具 | 输入语法 | 语义 |
|------|---------|------|
| `query_metric(promql, time_range)` | **PromQL** | 通用 metrics 查询；LLM 兜底用 |
| `get_memory_trend(node, duration)` | 内置 PromQL 模板 | `rate(node_memory_MemAvailable_bytes{node=...}[...])` |
| `get_oom_events(node, time_range)` | 内置 PromQL 模板 | `increase(node_vmstat_oom_kill{node=...}[...])` |
| `get_cpu_pressure(node, time_range)` | 内置 PromQL 模板（PSI）| `node_pressure_cpu_waiting_seconds_total` |
| `get_cgroup_memory(cgroup, time_range)` | 内置 PromQL 模板 | `container_memory_usage_bytes` |
| `get_disk_latency(device, time_range)` | 内置 PromQL 模板 | histogram_quantile P99 |
| `get_network_errors(node, interface)` | 内置 PromQL 模板 | `node_network_receive_errs_total` |
| `search_logs(es_query, time_range, level)` | **ES Query DSL** | 通用 logs 查询；LLM 兜底用 |
| `get_kernel_logs(node, time_range, grep)` | 内置 ES 模板 | `kernel.priority:err AND host:...` |
| `get_oom_kill_log(node, time_range)` | 内置 ES 模板 | dmesg 中 OOM kill 完整记录 |
| `get_crash_context(node, crash_time, window)` | metrics + logs 组合 | 时间窗内两类信号同步查询 |

**通用 vs 语义化工具的取舍**：

- 通用 `query_metric(promql, ...)` / `search_logs(es_query, ...)`：LLM 自由写 PromQL/ES DSL，灵活但需要 LLM 懂语法
- 语义化 `get_memory_trend(node, duration)`：内置 PromQL 模板，LLM 不需懂底层

v2 同时提供两层。语义化工具是默认；通用查询作为兜底。**所有 backend 看到的输入都是统一的 PromQL/ES DSL**——adapter 负责翻译。

## 影响

### 优势

| 维度 | 影响 |
|------|------|
| **历史 / 趋势** | 监控系统的时序数据是 OOM 慢泄漏等场景的关键证据来源 |
| **零额外基础设施** | 企业生产必有监控，直接复用，不部署新 agent |
| **多 backend 解耦** | 同一套 LLM 工具，不同环境切 adapter |
| **业界对齐** | 与 Google/Azure/Datadog 的 SRE agent 路径一致 |

### 代价

| 维度 | 影响 |
|------|------|
| **数据语义差异** | LLM 工具层统一 PromQL/ES DSL，但**各 backend 内部的指标名映射不同**（如 Prometheus 用 `node_memory_MemAvailable_bytes`，Datadog 是 `system.mem.usable`），adapter 内部需维护映射表 |
| **指标覆盖度未必全** | 标准 node_exporter 未必导出所有内核指标（如 `/proc/slabinfo` 详细 slab、`/proc/schedstat`），需要确认 + 必要时加 custom exporter |
| **网络依赖** | adapter 调用是网络请求，要处理超时和降级 |
| **数据访问权限** | 需要监控系统的 read-only token，部署时要协调 |

### 不提供的能力

- **不直接读 `/proc/`**：用户明确反对（见上下文）
- **不并发跑 `bpftrace`/`perf`**：违反"不在病机主动探测"原则（[AgentThink 设计原则 5](../../AgentThink.md)）
- **不修改任何 sysctl / cgroup**：v2 是只读诊断

## 接口契约

```python
class ObservabilityAdapter(Protocol):
    def query_metric(
        self, promql: str, start: datetime, end: datetime, step: str = "15s"
    ) -> list[TimeSeriesPoint]:
        """统一接收 PromQL；非 Prometheus backend 内部翻译。"""

    def search_logs(
        self, es_query: str, start: datetime, end: datetime, level: str | None = None
    ) -> list[LogEntry]:
        """统一接收 ES Query DSL；非 ES backend 内部翻译。"""

    def health_check(self) -> bool: ...

    # 注：Traces v2 不实现，留待 v3。
```

backend 选择 via `configs/observability.yaml`（gitignored，每个部署不同）。

## 备选方案（已否决）

### 备选 A：直接读 `/proc/`

否决理由：用户明确反对；缺历史/多节点视图；与业界对齐相左（[AgentThink §C](../../AgentThink.md)）。

### 备选 B：在 agent 侧自部署一个 Prometheus

否决理由：违反"用业界已有方案"原则；重复采集；增加运维负担。

### 备选 C：让 agent 通过 SSH 跑 `ss`、`top` 等命令

否决理由：相当于变相 `/proc/` 直读；增加目标节点负载；只能看当前不能看历史。

## 验证

- v2.1 acceptance：Prometheus adapter 跑通 5 例 慢泄漏 OOM 趋势分析（[PRD §6.2](../PRD.md)）
- v2.2 acceptance：Huatuo 或 Datadog 中至少一个 backend 跑通（[PRD §6.3](../PRD.md)）

## 未来回顾

- 若发现 node_exporter 覆盖度严重不足，评估是否提供 custom exporter（仍归类 L1 集成，不重写 Prometheus）
- Huatuo 上线后 evaluate eBPF kernel observability 对 softlockup 等 sub-scrape 故障的价值

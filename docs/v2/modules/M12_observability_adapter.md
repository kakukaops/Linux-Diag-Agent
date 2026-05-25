# M12 — Observability Adapter 设计文档

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格（2026-05-20）。

| 字段 | 值 |
|------|---|
| 模块编号 | M12（v2 新增，类别 C）|
| 状态 | Design Draft |
| 关联文档 | [../PRD.md](../PRD.md) · [../Architecture.md](../Architecture.md) |
| 关联 ADR | [ADR-021](../adr/ADR-021-observability-adapter.md) |
| 最后更新 | 2026-05-20 |

---

## 1. 目标与边界

### 1.1 目标

为 ReAct agent 提供**对外部监控系统的访问能力**——不重造监控，统一对接业界已有方案（Prometheus / Elasticsearch / Huatuo / Datadog）。

### 1.2 关键决策

| # | 决策 |
|---|------|
| D1 | **不直接读 `/proc/`**（用户明确反对）；走外部监控系统 |
| D2 | **统一接口语义**：metrics 用 PromQL 语法，logs 用 ES Query DSL，traces v2 不做（[ADR-021](../adr/ADR-021-observability-adapter.md)）|
| D3 | 每 adapter 内部翻译统一语法到自家 backend |
| D4 | v2.1 实现 PrometheusAdapter + ElasticsearchAdapter；v2.2 加 HuatuoAdapter + DatadogAdapter |
| D5 | 监控查询走 service account read-only token，超时 30s，失败 fallback 到只用知识图谱 |

### 1.3 在范围

- `ObservabilityAdapter` Protocol
- 4 个 backend 实现（Prometheus / ES / Huatuo / Datadog）
- LLM 工具集（通用 query_metric / search_logs + 语义化 get_memory_trend 等）
- `configs/observability.yaml` schema
- 限制说明：**metrics 对内核瞬时故障无效**（[ADR-021 §限制](../adr/ADR-021-observability-adapter.md)）

### 1.4 不在范围

- 监控数据采集（依赖外部已部署的 Prometheus exporter / fluent-bit 等）
- 自部署 Prometheus / ES
- Traces 工具（v2 不做）
- Grafana dashboard 生成

---

## 2. 部署形态（远程 HTTP 客户端,与 M9-M11 不同）

```
┌──────────── agent 机器 ────────────┐
│                                    │
│  M12 Python 模块（无独立服务）      │
│   clients/observability/           │
│     __init__.py                    │
│     base.py            # Protocol  │
│     prometheus.py      # Adapter   │
│     elasticsearch.py   # Adapter   │
│     huatuo.py          # Adapter   │
│     datadog.py         # Adapter   │
│     tools.py           # MCP tools │
│                                    │
└────────────────────────────────────┘
              ↓ HTTPS
   ┌────┬─────┬───────┬────────┐
Prometheus  ES   Huatuo  Datadog
（用户部署环境提供）
```

注意：M12 是**库形式**集成到 agent，不是独立 MCP server。理由：

- 只是 HTTP 客户端，没有沙箱化需求（不像 M9-M11 跑外部命令）
- ReAct loop 直接调 Python 函数效率更高

---

## 3. ObservabilityAdapter Protocol

```python
# clients/observability/base.py

from typing import Protocol
from datetime import datetime
from dataclasses import dataclass

@dataclass
class TimeSeriesPoint:
    timestamp: datetime
    value: float
    labels: dict[str, str]

@dataclass
class LogEntry:
    timestamp: datetime
    level: str         # debug / info / warn / err / crit
    message: str
    host: str | None
    metadata: dict     # raw fields

class ObservabilityAdapter(Protocol):
    def query_metric(
        self, promql: str, start: datetime, end: datetime, step: str = "15s"
    ) -> list[TimeSeriesPoint]:
        """统一接收 PromQL；非 Prometheus backend 内部翻译。"""

    def search_logs(
        self, es_query: str, start: datetime, end: datetime,
        level: str | None = None, limit: int = 200
    ) -> list[LogEntry]:
        """统一接收 ES Query DSL；非 ES backend 内部翻译。"""

    def health_check(self) -> bool: ...

    # 注：Traces v2 不实现，留待 v3。
```

---

## 4. Adapter 实现要点

### 4.1 PrometheusAdapter

```python
class PrometheusAdapter:
    def __init__(self, url: str, token: str | None = None, timeout_s: int = 30):
        self._url = url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def query_metric(self, promql, start, end, step="15s"):
        resp = self._client.get(
            f"{self._url}/api/v1/query_range",
            params={"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step},
            headers=self._headers,
        )
        resp.raise_for_status()
        return self._parse_prom_response(resp.json())

    def search_logs(self, *args, **kwargs):
        raise NotImplementedError("Prometheus 不存储 logs;请使用 ES adapter")
```

**要点**：直接转发 PromQL，无翻译。

### 4.2 ElasticsearchAdapter

```python
class ElasticsearchAdapter:
    def __init__(self, url: str, index_pattern: str, token: str | None = None):
        self._url = url
        self._index = index_pattern
        self._client = httpx.Client(timeout=30)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def query_metric(self, *args, **kwargs):
        # Loki 兼容路径(若 url 是 Loki):
        if self._is_loki():
            return self._loki_query(*args, **kwargs)
        raise NotImplementedError("ES 不存储 metrics(除非 metricbeat)")

    def search_logs(self, es_query, start, end, level=None, limit=200):
        body = {
            "query": {
                "bool": {
                    "must": [{"query_string": {"query": es_query}}],
                    "filter": [{"range": {"@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}}]
                }
            },
            "size": limit,
            "sort": [{"@timestamp": "desc"}]
        }
        if level:
            body["query"]["bool"]["filter"].append({"term": {"log.level": level}})
        resp = self._client.post(f"{self._url}/{self._index}/_search", json=body, headers=self._headers)
        return self._parse_es_hits(resp.json())
```

**要点**：ES DSL 直接转发；Loki backend 内部翻译为 LogQL（同 adapter,识别 URL）。

### 4.3 HuatuoAdapter

```python
class HuatuoAdapter:
    """huatuo.tech eBPF kernel observability."""
    def __init__(self, url: str, token: str):
        self._url = url
        self._client = httpx.Client(timeout=30)
        self._headers = {"Authorization": f"Bearer {token}"}

    def query_metric(self, promql, start, end, step="15s"):
        # huatuo 若不原生支持 PromQL,做 best-effort 翻译
        huatuo_query = self._promql_to_huatuo(promql)
        resp = self._client.post(f"{self._url}/api/metrics/query", json=huatuo_query)
        return self._parse_huatuo_metrics(resp.json())

    def search_logs(self, es_query, start, end, level=None, limit=200):
        # ES DSL → huatuo logs query
        ...

    def _promql_to_huatuo(self, promql: str) -> dict:
        """Best-effort 翻译。常见模式映射:
           rate(node_memory_MemAvailable_bytes[1h]) → {"metric": "mem.available", ...}
           不支持的 PromQL 抛 UnsupportedQuery,LLM 收到后调通用 query_metric。
        """
        ...
```

**要点**：huatuo API 文档对齐是 v2.2 工作（T-201）。本设计只规定接口契约，具体翻译细节由 spike 决定。

### 4.4 DatadogAdapter

```python
class DatadogAdapter:
    def __init__(self, api_key: str, app_key: str, site: str = "datadoghq.com"):
        ...

    def query_metric(self, promql, start, end, step="15s"):
        dd_query = self._promql_to_datadog(promql)
        # Datadog Metrics Query API
        ...

    def search_logs(self, es_query, start, end, level=None, limit=200):
        dd_query = self._es_dsl_to_datadog_logs(es_query)
        # Datadog Logs API
        ...
```

**要点**：Datadog API 不是 PromQL 兼容；adapter 内做映射表（常见 PromQL 模式 → Datadog query string）。

---

## 5. LLM 工具集（M13 注册）

按"统一输入语法"原则，工具的输入永远是 PromQL（metrics）或 ES DSL（logs），与 backend 无关。

### 5.1 通用工具（兜底）

```python
# 通用 metrics
query_metric(promql: str, time_range: str = "1h") -> list[TimeSeriesPoint]

# 通用 logs
search_logs(es_query: str, time_range: str = "1h", level: str | None = None) -> list[LogEntry]
```

### 5.2 语义化工具（推荐 LLM 首选）

LLM 不需要懂 PromQL/ES DSL 也能用：

| 工具 | 内置 PromQL/ES 模板 |
|------|---------------------|
| `get_memory_trend(node, duration)` | `rate(node_memory_MemAvailable_bytes{node="{node}"}[{duration}])` |
| `get_oom_events(node, time_range)` | `increase(node_vmstat_oom_kill{node="{node}"}[{range}])` |
| `get_cpu_pressure(node, time_range)` | `node_pressure_cpu_waiting_seconds_total{node="{node}"}` |
| `get_cgroup_memory(cgroup_path, time_range)` | `container_memory_usage_bytes{id="{cgroup_path}"}` |
| `get_disk_latency(device, time_range)` | `histogram_quantile(0.99, rate(node_disk_read_time_seconds_bucket{device="{device}"}[5m]))` |
| `get_network_errors(node, interface)` | `node_network_receive_errs_total{device="{interface}",node="{node}"}` |
| `get_kernel_logs(node, time_range, grep)` | ES: `kernel.priority:err AND host:{node} AND message:*{grep}*` |
| `get_oom_kill_log(node, time_range)` | ES: `kernel.subsystem:oom_kill AND host:{node}` |
| `get_crash_context(node, crash_time, window)` | 时间窗内同时查 metrics + logs |

### 5.3 工具实现要点

- 工具内部根据 `configs/observability.yaml` 选 adapter
- 工具调用失败（adapter raise）→ M13 注入 `tool result: error` 给 LLM
- backend 缺失（如 hospital 只装了 Prometheus 没 ES）→ `search_logs` 返回 `{"error": "no_logs_backend_configured"}`

---

## 6. 配置 schema

`configs/observability.yaml`（gitignored,每个部署不同）：

```yaml
# 默认 adapter 选择
default_metrics_adapter: prometheus
default_logs_adapter: elasticsearch

backends:
  prometheus:
    url: http://prometheus.example.com:9090
    token: ${PROMETHEUS_TOKEN}              # 从环境变量取
    timeout_seconds: 30

  elasticsearch:
    url: https://es.example.com:9200
    index_pattern: "kernel-logs-*"
    token: ${ES_TOKEN}
    timeout_seconds: 30

  huatuo:
    url: https://huatuo.internal.example.com
    token: ${HUATUO_TOKEN}
    enable: false                            # v2.2 才启用

  datadog:
    api_key: ${DATADOG_API_KEY}
    app_key: ${DATADOG_APP_KEY}
    site: datadoghq.com
    enable: false
```

加载逻辑（`clients/observability/__init__.py`）：

```python
def get_adapter(kind: Literal["metrics", "logs"]) -> ObservabilityAdapter:
    cfg = load_config()
    name = cfg.observability[f"default_{kind}_adapter"]
    backend_cfg = cfg.observability.backends[name]
    if not backend_cfg.enable:
        raise NoAdapterConfigured(f"{name} disabled")
    return ADAPTER_CLASSES[name](**backend_cfg.to_dict())
```

---

## 7. 与其他模块的集成

| 模块 | 集成方式 |
|------|---------|
| M22 ReAct Loop | M12 工具在 `route ∈ {kernel, change, unknown}` 路由暴露;`hardware` 路由通常不需要监控（用 M11） |
| M7 generate_report | 监控数据作为"故障前 2 小时上下文"证据 |
| 评测 | 慢泄漏 OOM 案例 5 例（[PRD §6.2](../PRD.md)）|

---

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 各 backend PromQL 兼容度不一 | 通用 `query_metric` 兜底; 失败时 fallback 到语义化工具的内置模板 |
| node_exporter 未导出关键指标（如 schedstat）| [ADR-021 §代价](../adr/ADR-021-observability-adapter.md)：评估是否补 custom exporter |
| 监控系统不可达 / 超时 | 30s timeout + tool result: error + LLM 自决降级到只用知识图谱 |
| HuatuoAdapter 实现成本高于预期（API 文档缺失）| v2.2 才做，spike 验证后再投入 T-201 |
| Datadog API 配额 / 成本 | adapter 内做请求合并 + 本地缓存（5 min TTL） |
| token 安全 | 从环境变量取，绝不入 git；定期 rotate |
| 监控数据隐私 | 仅查询故障时间窗 + 节点 ID，不做大范围 scan |

---

## 9. 实施任务对照（[ProjectPlan](../ProjectPlan.md)）

| Task ID | 内容 | PD | 阶段 |
|---------|------|----|----|
| T-110 | `ObservabilityAdapter` Protocol + 注册机制 | 3 | v2.1 |
| T-111 | PrometheusAdapter | 4 | v2.1 |
| T-112 | ElasticsearchAdapter（含 Loki 兼容）| 4 | v2.1 |
| T-113 | LLM 工具（query_metric / get_memory_trend 等 9 个）| 4 | v2.1 |
| T-114 | `configs/observability.yaml` schema + loader | 1 | v2.1 |
| T-115 | M12 集成测试（含 mock backend）| 3 | v2.1 |
| T-201 | HuatuoAdapter（含 spike 对齐 API）| 5 | v2.2 |
| T-202 | DatadogAdapter | 4 | v2.2 |
| T-203 | 多 backend 路由策略（adapter selection by config）| 2 | v2.2 |

合计 v2.1 19 PD + v2.2 11 PD = 30 PD（M17 + M19 主线）。

---

*参考：[Architecture.md](../Architecture.md) · [ADR-021](../adr/ADR-021-observability-adapter.md)*

# ADR-012 — CodeGraph MCP 集成使用 HTTP 传输（非 stdio）

| 字段 | 值 |
|------|---|
| 状态 | Accepted（**需要 codesearch 项目协调**）|
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M4 |
| 相关 ADR | [ADR-008 复用 codesearch](ADR-008-reuse-codesearch.md) |
| 跨项目协调 | ✅ 需要 codesearch 启用 HTTP server 模式 |

## 上下文

[ADR-008](ADR-008-reuse-codesearch.md) 决定 Linux-Diag-Agent 通过 MCP 复用 codesearch 作为 Code Graph + Doc Tree 提供者。MCP 协议支持两种 transport：

| 方案 | 优点 | 缺点 |
|------|------|------|
| **stdio subprocess** | codesearch 原生默认；FastMCP 开箱即用；Claude Code 用同方式 | diag-agent 必须 spawn codesearch 子进程；进程生命周期耦合；升级互相影响 |
| **HTTP / streamable-http** | 完全解耦：codesearch 独立运行、独立升级；多 client 可并发；可远程部署 | codesearch 当前未启用 HTTP server 模式（仅 stdio 默认）|
| 同时支持 | 配置切换 | 抽象层复杂 |

## 决策

**v1 采用 HTTP MCP 传输**。codesearch 项目侧需新增 HTTP server 启动入口。

## 影响

### 部署架构

```
┌────────────────────────────────┐  HTTP MCP (streamable-http)  ┌──────────────────────────┐
│ Linux-Diag-Agent               │  ───────────────────────────▶│ CodeGraph MCP server    │
│ - clients/codegraph/client.py │  http://codesearch:8765      │ FastMCP HTTP transport   │
└────────────────────────────────┘                              │ olk-kernel-v6.6 / v5.10  │
                                                                 └──────────────────────────┘
```

### codesearch 侧需要的工作

FastMCP 原生支持 `streamable-http` transport，只需启动时切换：

```python
# codesearch server/main.py 新增一个入口（与现有 stdio 并存）
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("codesearch")
# ... 注册所有现有工具

if __name__ == "__main__":
    import sys
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8765)
    else:
        mcp.run(transport="stdio")
```

或新增 `pyproject.toml` 入口点：

```toml
[project.scripts]
codesearch-server = "server.main:main"           # stdio (现状)
codesearch-server-http = "server.main:main_http" # HTTP (新增)
```

**工作量预估**：~50 行（含启动脚本 + Dockerfile 暴露端口 + smoke test）。

### Linux-Diag-Agent 侧

- `clients/codegraph/client.py` 用 `mcp.client.streamable_http.streamablehttp_client`
- 配置文件：`codegraph.endpoint = http://localhost:8765`（开发）/ `http://codesearch-svc:8765`（生产；service 名沿用 codesearch 与运维约定一致）
- 启动时跑 `healthcheck`：HTTP ping + 工具 smoke test

### 优点

- **独立生命周期**：codesearch 升级/重启不影响 diag-agent；反之亦然
- **多 client 并发**：未来如其他诊断工具也想用 codesearch，不冲突
- **远程部署**：可把 codesearch 跑在专门的索引节点（资源占用大），diag-agent 跑在轻量节点
- **可观测性**：HTTP 请求/响应可被标准工具（如 Prometheus、Grafana）抓取
- **本地代理友好**：开发时可用 ngrok / reverse proxy 调试

### 风险

- **协调依赖**：v1.0 启动前必须确认 CodeGraph HTTP server 模式可用 + 已部署
- **故障域**：CodeGraph 服务挂掉时 diag-agent 检索能力直接缺失（同 stdio 情况下子进程崩溃；但 HTTP 模式可用 supervisor 单独治理）
- **网络配置**：跨节点部署时需关注网络策略、防火墙、TLS（生产建议加 TLS）

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **stdio subprocess** | 进程耦合；codesearch 升级 / 重启需要 diag-agent 一起重启；多个 Python 进程都 spawn 一份 codesearch，资源浪费 |
| **同时支持两种** | 实现复杂；分裂 codepath；v1 不必要 |

## v1.0 启动前置 checklist

1. ✅ codesearch 项目侧实现 HTTP server 入口（PR 合并）
2. ✅ CodeGraph HTTP server 加入 docker-compose（已规划 OLK kernel docs builder pattern）
3. ✅ Linux-Diag-Agent 启动配置中加 `CODESEARCH_ENDPOINT` 环境变量
4. ✅ 跨项目集成 smoke test：diag-agent 启动 → healthcheck → 调每个工具一次

## 监控指标

- `codegraph_http_request_duration_seconds{tool}` — 各 MCP 工具响应时间
- `codegraph_http_request_errors_total{tool, code}` — 错误率
- `codegraph_http_session_age_seconds` — 当前 MCP session 存活时间
- `codegraph_healthcheck_status` — 0 = healthy, 1+ = 各类异常

## 长期演进

- v1.0：HTTP 单向调用，无 TLS（开发 / 同机部署）
- v1.1：加 TLS + 基础认证（如需跨机）
- v1.2：评估是否把 codesearch streaming 工具（如长文档 `read_doc_section`）改用 SSE 减少首字节延迟
- v2：评估是否拆 codesearch 为多副本（分 repo 路由），高可用部署

## 参考

- MCP streamable-http transport：https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#streamable-http
- FastMCP HTTP 模式：https://github.com/modelcontextprotocol/python-sdk
- codesearch 当前架构（stdio 为主）：`/home/mqq/github/codesearch/docs/design-v2.md`

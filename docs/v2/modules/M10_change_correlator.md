# M10 — Change Correlator（变更关联）设计文档

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格（2026-05-20）。

| 字段 | 值 |
|------|---|
| 模块编号 | M10（v2 新增，类别 G）|
| 状态 | Design Draft |
| 关联文档 | [../PRD.md](../PRD.md) · [../Architecture.md](../Architecture.md) |
| 关联 ADR | — |
| 最后更新 | 2026-05-20 |

---

## 1. 目标与边界

### 1.1 目标

回答 SRE 排障的第一问——**"什么变了"**。最高频的根因就是一个变更（升级、reboot、参数漂移）；v1 完全没有这维度的工具。

### 1.2 关键决策

| # | 决策 |
|---|------|
| D1 | 数据源：**节点本机命令**（rpm/dnf/journalctl），SSH 只读访问 |
| D2 | 维护 `system_baseline` PG 表存关键配置基线（cmdline / sysctl / 关键文件 hash）|
| D3 | `correlate_fault_with_changes` 用**时间窗对齐**算法（不是 ML） |
| D4 | 不做 file-level diff（不是配置管理工具）；只采集"哪些 package 哪天装的"+ 关键参数漂移 |

### 1.3 在范围

- 5 个工具：包升级时间线、boot 时间、cmdline diff、config drift、故障-变更对齐
- system_baseline 表 schema
- 基线采集 cron（与 weekly_sync 集成）

### 1.4 不在范围

- 完整 file integrity monitoring（用 AIDE / Tripwire）
- application-level deployment 变更（K8s rollout 等，留 v3）
- 自动 rollback

---

## 2. 部署形态

```
┌──── M10 部署 ────┐
│                  │
│ agent 机器:       │
│   mcp_servers/   │
│     change_corr/ │
│       server.py  │
│       baseline.py│
│                  │
│ PG:               │
│   system_baseline│
│                  │
└──────────────────┘
        ↓ SSH（read-only）
   被诊断节点
```

同 M9/M11，独立 MCP server；远程节点通过 SSH 拉取命令输出。

---

## 3. MCP 工具

### 3.1 `get_package_history`

```python
{
  "name": "get_package_history",
  "description": "Retrieve kernel and key package install/upgrade timeline via rpm/dnf.",
  "parameters": {
    "node": {"type": "string"},
    "packages": {
      "type": "array",
      "items": {"type": "string"},
      "description": "default = ['kernel', 'kernel-core', 'glibc', 'systemd', 'NetworkManager']"
    },
    "since": {"type": "string", "description": "ISO datetime; default last 90 days"}
  }
}
```

**返回**：

```json
{
  "events": [
    {
      "timestamp": "2026-05-19T11:23:01Z",
      "action": "upgrade",
      "package": "kernel",
      "from_version": "6.6.0-12.0.1.oe2403",
      "to_version": "6.6.0-14.0.1.oe2403",
      "transaction_id": "2456"
    },
    {
      "timestamp": "2026-05-18T03:00:00Z",
      "action": "install",
      "package": "kernel-debug",
      "version": "6.6.0-14.0.1.oe2403",
      "transaction_id": "2455"
    }
  ],
  "total_count": 2
}
```

**实现**：

- 主路径：`dnf history list --since="2026-02-19"`（dnf history JSON 解析）
- 补充：`rpm -q --last <pkg>`（拿单包安装时间）

### 3.2 `get_boot_history`

```python
{
  "name": "get_boot_history",
  "description": "Retrieve reboot/boot timeline from systemd journal.",
  "parameters": {
    "node": {"type": "string"},
    "count": {"type": "integer", "default": 20}
  }
}
```

**返回**：

```json
{
  "boots": [
    {"index": 0, "boot_id": "abc...", "first_entry": "2026-05-19T12:01:33Z", "last_entry": null, "current": true},
    {"index": -1, "boot_id": "def...", "first_entry": "2026-05-15T08:00:01Z", "last_entry": "2026-05-19T12:01:20Z"},
    {"index": -2, "boot_id": "...", "first_entry": "...", "last_entry": "...", "kernel_version": "6.6.0-12.0.1"}
  ],
  "current_uptime_seconds": 7821,
  "anomalies": [
    {"type": "frequent_reboots", "details": "5 boots in last 24h"}
  ]
}
```

**实现**：

- `journalctl --list-boots --output=json`
- `uptime`（当前 uptime）
- 简单异常检测：24h 内 reboot > 3 次 / boot 时长异常短

### 3.3 `get_kernel_cmdline_diff`

```python
{
  "name": "get_kernel_cmdline_diff",
  "description": "Compare current /proc/cmdline against the saved baseline.",
  "parameters": {
    "node": {"type": "string"}
  }
}
```

**返回**：

```json
{
  "current": "BOOT_IMAGE=/vmlinuz-6.6.0-14 root=UUID=... ro quiet splash transparent_hugepage=always",
  "baseline": {
    "saved_at": "2026-05-01T00:00:00Z",
    "cmdline": "BOOT_IMAGE=/vmlinuz-6.6.0-12 root=UUID=... ro quiet splash"
  },
  "diff": {
    "added": ["transparent_hugepage=always"],
    "removed": [],
    "modified": [{"key": "BOOT_IMAGE", "from": "/vmlinuz-6.6.0-12", "to": "/vmlinuz-6.6.0-14"}]
  }
}
```

**实现**：

- 当前：`cat /proc/cmdline` via SSH
- 基线：从 `system_baseline` PG 表取该节点最近一次快照
- diff：token-level 比较（空格分隔，等号拆 key=value）

### 3.4 `get_config_drift`

```python
{
  "name": "get_config_drift",
  "description": "Compare current sysctl and key config files against baseline.",
  "parameters": {
    "node": {"type": "string"},
    "scope": {
      "type": "string",
      "enum": ["sysctl", "key_files", "all"],
      "default": "all"
    }
  }
}
```

**返回**：

```json
{
  "sysctl_drift": [
    {"key": "vm.overcommit_memory", "baseline": "0", "current": "1", "changed_estimate": "since 2026-05-10"}
  ],
  "key_files_drift": [
    {"path": "/etc/security/limits.conf", "baseline_hash": "abc...", "current_hash": "def...", "changed_estimate": "..."}
  ],
  "key_files_monitored": ["/etc/sysctl.conf", "/etc/security/limits.conf", "/etc/fstab", "/etc/modprobe.d/", "/etc/cgroup/"]
}
```

**实现**：

- sysctl：`sysctl -a` 全量 → diff baseline 中的 dict
- 文件：维护 `system_baseline.key_files_hash` JSONB，比对当前 sha256

### 3.5 `correlate_fault_with_changes`

**最核心的工具**——把上述变更时间线与故障时间点对齐。

```python
{
  "name": "correlate_fault_with_changes",
  "description": "Align fault occurrence with recent changes; return time-correlated suspects.",
  "parameters": {
    "node": {"type": "string"},
    "fault_time": {"type": "string", "description": "ISO datetime of fault"},
    "lookback_days": {"type": "integer", "default": 30}
  }
}
```

**返回**：

```json
{
  "fault_time": "2026-05-19T14:30:00Z",
  "correlated_changes": [
    {
      "change": {
        "type": "kernel_upgrade",
        "from": "6.6.0-12.0.1",
        "to": "6.6.0-14.0.1",
        "timestamp": "2026-05-19T11:23:01Z"
      },
      "time_delta_seconds": 11219,
      "correlation_strength": "high",
      "rationale": "Kernel upgrade 3 hours before first fault; high temporal correlation"
    },
    {
      "change": {
        "type": "sysctl_drift",
        "key": "vm.overcommit_memory",
        "from": "0",
        "to": "1",
        "timestamp_estimate": "2026-05-10T??"
      },
      "time_delta_seconds": 770000,
      "correlation_strength": "low",
      "rationale": "Changed >9 days before fault; weak temporal correlation"
    }
  ],
  "interpretation": "Strong candidate: kernel upgrade 2026-05-19 11:23. Compare 6.6.0-12 vs 6.6.0-14 changelog."
}
```

**算法**（不是 ML，规则化）：

```python
def correlate(fault_time, changes):
    for change in changes:
        delta = (fault_time - change.timestamp).total_seconds()
        if delta < 0: continue  # 故障在变更前,排除
        if delta < 3600:        strength = "very_high"  # 1h 内
        elif delta < 24 * 3600: strength = "high"       # 1d 内
        elif delta < 7 * 86400: strength = "medium"     # 1w 内
        elif delta < 30 * 86400: strength = "low"       # 30d 内
        else:                   continue                 # 30d 外不算
        yield {"change": change, "time_delta_seconds": delta, "strength": strength}
```

输出按 strength + 故障类型相关度排序（如 kernel upgrade 与 kernel panic 高度相关；libc 升级与 panic 相关度低）。

---

## 4. `system_baseline` PG 表

```sql
CREATE TABLE system_baseline (
    id              BIGSERIAL PRIMARY KEY,
    node            TEXT NOT NULL,
    snapshot_time   TIMESTAMPTZ NOT NULL,
    cmdline         TEXT,
    sysctl_dict     JSONB,                -- {"vm.overcommit_memory": "0", ...}
    key_files_hash  JSONB,                -- {"/etc/sysctl.conf": "sha256...", ...}
    kernel_version  TEXT,
    metadata        JSONB                 -- 任意附加信息
);

CREATE INDEX idx_baseline_node_time ON system_baseline (node, snapshot_time DESC);
```

**基线采集**：

- 每周 cron 跑一次（`weekly_sync.sh` 集成）：`python -m m10.baseline collect --node <hostname>`
- 也可手动触发（节点首次接入时）

**基线滚动**：保留每节点最近 12 周快照（按 snapshot_time DESC 删旧）。

---

## 5. 与其他模块的集成

| 模块 | 集成方式 |
|------|---------|
| M22 ReAct Loop | M10 工具在 `route ∈ {change, kernel, unknown}` 路由暴露 |
| Triage `classify_fault_and_route` | 用户问题含"升级"、"昨天"等关键词时触发 `change` 路由（[Architecture §2.1](../Architecture.md)）|
| weekly_sync.sh | 集成 baseline 采集 cron |
| M12 监控 | 变更时间线 + 监控历史趋势在 ReAct 中可联合查（如 "升级后 metrics 变化"）|

---

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| dnf history 在长期未升级机器上记录可能滚动丢失 | 同时查 `rpm -q --last` 兜底 |
| 节点首次接入无基线 → cmdline/config drift 不可用 | 工具返回 `{"error": "no_baseline_for_node", "suggest": "trigger m10 baseline collect"}` |
| journalctl --list-boots 在系统时间错乱后不准 | bmc_clock_skew 校正（同 M11） |
| sysctl 全量 diff 噪声大 | 只关注 ~30 个"危险" sysctl（vm.overcommit_*、kernel.panic*、net.ipv4.tcp_*）|
| SSH 凭据缺失 | tool 返回错误,提示 SRE 配置 |
| 远程命令输出格式跨发行版差异 | 仅支持 OLK（v2 范围限定）|

---

## 7. 实施任务对照（[ProjectPlan](../ProjectPlan.md)）

| Task ID | 内容 | PD |
|---------|------|----|
| T-101 | `get_package_history` | 2 |
| T-102 | `get_boot_history` | 1 |
| T-103 | `get_kernel_cmdline_diff` + 基线管理 | 2 |
| T-104 | `get_config_drift`（sysctl + 文件 hash）| 3 |
| T-105 | `correlate_fault_with_changes`（时间对齐算法）| 2 |
| T-106 | M10 集成测试 | 2 |

合计 12 PD（v2.1 M16 主线）。

---

*参考：[Architecture.md](../Architecture.md) · [M12](M12_observability_adapter.md)*

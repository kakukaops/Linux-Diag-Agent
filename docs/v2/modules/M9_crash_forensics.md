# M9 — Crash Forensics（崩溃转储分析）设计文档

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格（2026-05-20）。

| 字段 | 值 |
|------|---|
| 模块编号 | M9（v2 新增，类别 F）|
| 状态 | Design Draft |
| 关联文档 | [../PRD.md](../PRD.md) · [../Architecture.md](../Architecture.md) · [M6](../../v1/modules/M6_host_mcp_tools.md) |
| 关联 ADR | [ADR-020](../adr/ADR-020-drgn-over-crash.md) |
| 最后更新 | 2026-05-20 |

---

## 1. 目标与边界

### 1.1 目标

为 ReAct agent 提供**编程化的内核崩溃转储分析能力**——这是 v1 完全缺失、对"内核"故障 agent 最核心的脊梁工具集。

### 1.2 关键决策

| # | 决策 |
|---|------|
| D1 | **使用 `drgn`** 作为主力（Python 库,可编程,契合 agent）；`crash` 作为 fallback（[ADR-020](../adr/ADR-020-drgn-over-crash.md)）|
| D2 | LLM **不直接写 drgn Python 脚本**——M9 维护预定义 query 枚举（封闭集）|
| D3 | `fetch_debuginfo` 自动从 OLK 仓库拉；sosreport 包内有则优先用 |
| D4 | 全部工具**只读**操作 vmcore，不修改 live system |
| D5 | drgn 在 **agent 机器** 运行，不在被诊断节点（避免病机加压）|

### 1.3 在范围

- vmcore 加载 + 符号表对齐
- 5 类预定义 query mode（all_stacks / locks / oom_context / network_state / memory_state）
- decode_stacktrace 集成
- taint flags 解析（位映射表）
- debuginfo 自动获取
- 容器化封装（drgn + OLK debuginfo 环境隔离）

### 1.4 不在范围

- vmcore 文件传输逻辑（在 [Architecture §8.0](../Architecture.md)）
- LLM 自由写 drgn 脚本（安全 + 可审计原因）
- live kernel debugging（v2 仅事后取证）
- 跨发行版兼容（仅 OLK-6.6 / OLK-5.10）

---

## 2. 部署形态

```
┌────────────────── agent 机器 ──────────────────┐
│                                                │
│  M9 容器（drgn + OLK debuginfo 缓存）          │
│    ├── /opt/m9/                               │
│    │   ├── debuginfo_cache/                   │
│    │   │   ├── kernel-6.6.0-...vmlinux        │
│    │   │   └── kernel-5.10.0-...vmlinux       │
│    │   └── queries/                            │
│    │       ├── all_stacks.py                  │
│    │       ├── locks.py                       │
│    │       ├── oom_context.py                 │
│    │       └── ...                            │
│    └── /tmp/diag/<diagnosis_id>/vmcore        │
│                                                │
│  MCP server (FastAPI) 暴露工具给 agent          │
└────────────────────────────────────────────────┘
                       ↓ HTTP
              agent ReAct loop（M13）
```

容器化的目的：drgn 依赖 OLK 特定版本的 libelf / Python，与 agent 主进程隔离，避免依赖冲突。

---

## 3. MCP 工具

### 3.1 `check_kdump_available`

```python
{
  "name": "check_kdump_available",
  "description": "Check whether the given node has kdump configured and check if recent vmcore exists.",
  "parameters": {
    "node": {"type": "string", "description": "hostname or 'local' for the diagnosis target"},
  }
}
```

**返回**：

```json
{
  "kdump_enabled": true,
  "crashkernel_param": "1G-4G:256M,4G-64G:512M,64G-:768M",
  "recent_vmcores": [
    {"path": "/var/crash/2026-05-19-14:30/vmcore", "size_bytes": 4123456789, "ctime": "2026-05-19T14:30:12Z"}
  ],
  "kdump_service_status": "active"
}
```

**实现**：SSH 远程执行 `systemctl is-active kdump`、`cat /proc/cmdline | grep crashkernel`、`ls -la /var/crash/`。

### 3.2 `fetch_debuginfo`

```python
{
  "name": "fetch_debuginfo",
  "description": "Fetch the kernel-debuginfo (vmlinux with symbols) matching the given kernel version. Caches locally.",
  "parameters": {
    "kernel_version": {"type": "string", "description": "e.g. '6.6.0-12.0.1.oe2403.x86_64'"},
    "from_sosreport": {"type": "string", "description": "optional path to sosreport, will check pkg list first"},
  }
}
```

**返回**：

```json
{
  "vmlinux_path": "/opt/m9/debuginfo_cache/6.6.0-12.0.1/vmlinux",
  "source": "olk_repo | sosreport_embedded | already_cached",
  "size_bytes": 845678901
}
```

**实现策略**（按优先级）：

1. 缓存命中 → 直接返回
2. sosreport 包内有 `kernel-debuginfo-*.rpm` → 解包提取 vmlinux
3. OLK 仓库 `dnf download --source kernel-debuginfo-${VERSION}` → 解包
4. 全部失败 → 抛 `DebuginfoNotAvailable`，agent 走 insufficient_evidence

### 3.3 `analyze_vmcore`

**封闭枚举 query**：LLM 选 query name，不传任意脚本。

```python
{
  "name": "analyze_vmcore",
  "description": "Analyze a kernel vmcore using a predefined query.",
  "parameters": {
    "vmcore_path": {"type": "string"},
    "vmlinux_path": {"type": "string"},
    "query": {
      "type": "string",
      "enum": ["all_stacks", "locks", "oom_context", "network_state", "memory_state"]
    }
  }
}
```

**Query 语义**：

| query | drgn 脚本逻辑 | 返回字段 |
|-------|-------------|---------|
| `all_stacks` | `for_each_task(prog) → stack_trace(task)` | per_cpu_stacks, per_task_stacks |
| `locks` | 扫所有 `struct mutex` / `spinlock`，找 owner | held_locks (lock_addr, owner_pid, wait_pids) |
| `oom_context` | 找 `oom_kill_process` 帧 → 提取 cgroup state | killed_process, cgroup_path, oom_score, zone_state |
| `network_state` | `socket_table` 遍历 | tcp_states, listen_sockets, conntrack |
| `memory_state` | `/proc/meminfo` 等效 from kernel struct | zones, slabs, vmstat_summary |

**实现要点**：所有 query 脚本在 `queries/` 目录维护；每个 query 是独立 Python 文件，PR review 后合入；LLM 永远不能动态写脚本。

### 3.4 `decode_stacktrace`

```python
{
  "name": "decode_stacktrace",
  "description": "Translate raw stack addresses to file:line. Wraps decode_stacktrace.sh.",
  "parameters": {
    "raw_trace": {"type": "string", "description": "multi-line stack trace from dmesg"},
    "kernel_version": {"type": "string"},
  }
}
```

**返回**：

```json
{
  "frames": [
    {"address": "ffffffff81234567", "function": "do_mmap", "file": "mm/mmap.c", "line": 1521, "offset": "+0x123/0x456"},
    ...
  ]
}
```

**实现**：调用 `scripts/decode_stacktrace.sh vmlinux < raw_trace`（kernel 自带）。需要 vmlinux 路径（来自 fetch_debuginfo）。

### 3.5 `parse_taint_flags`

```python
{
  "name": "parse_taint_flags",
  "description": "Decode kernel taint flags integer or letter string into structured fields.",
  "parameters": {
    "taint_value": {"type": "string", "description": "either integer (e.g. '4096') or letter string (e.g. 'G B C')"},
  }
}
```

**返回**：

```json
{
  "flags": [
    {"letter": "G", "code": 0, "description": "Proprietary module loaded", "implications": "..."},
    {"letter": "M", "code": 11, "description": "Machine Check Exception occurred", "implications": "Likely hardware issue; check mcelog/EDAC"}
  ],
  "high_priority_flags": ["M"],  // 提示路由到 hardware
  "search_narrowing_hints": ["exclude_mainline_only", "include_proprietary_module_context"]
}
```

**实现**：taint 位映射表统一放在 `mcp_servers/shared/taint_flags.py`（**M-4 修复**：M9 `parse_taint_flags`、M11 hardware layer、`agent/triage/nodes.py:detect_taint_and_hw_signals` 三处共享 import 此模块，避免实现三次）。

---

## 4. 实现要点

### 4.1 drgn OLK 兼容性 spike（M14 启动前）

**spike 任务**（1 周）：

1. `pip install drgn` 在 OLK Python 3.11 环境
2. 取 OLK-6.6 vmcore 1 个 + 对应 vmlinux
3. 运行 `drgn -c vmcore -s vmlinux` 进入 REPL
4. 测试基本 helpers：`for_each_task`、`stack_trace`、`prog['init_task']`
5. 测试 OLK-5.10 同上
6. 输出兼容性报告（任何 helper 在 OLK 不工作）

**失败回退**：若 spike 不通过，M9 主路径改为 `crash` + Python pexpect 包装（增加 ~5 PD）。

### 4.2 vmcore 路径管理

vmcore 文件按 `<diagnosis_id>` 隔离：

```
/tmp/diag/<diagnosis_id>/
  ├── vmcore               # 拷贝 / 符号链接
  ├── vmlinux              # 符号链接到 debuginfo_cache
  └── analysis/            # query 输出缓存
```

诊断完成后整个目录清理（`agent/graph.py:save_audit_trail` 阶段触发）。

### 4.3 容器化

```dockerfile
FROM openeuler/openeuler:24.03

# OLK Python + drgn 依赖
RUN dnf install -y python3.11 python3.11-pip libelf-devel libdw-devel \
                   kernel-debuginfo  # 注：版本动态从配置取

RUN pip install drgn==0.0.30

WORKDIR /opt/m9
COPY queries/ queries/
COPY server.py .

EXPOSE 8090
CMD ["python3.11", "server.py"]
```

### 4.4 错误处理

| 错误 | 工具响应 |
|------|---------|
| vmlinux 与 vmcore 版本不匹配 | `{"error": "version_mismatch", "vmcore_version": "...", "vmlinux_version": "..."}` |
| 缺 debuginfo | `{"error": "debuginfo_missing", "package_needed": "kernel-debuginfo-6.6.0-..."}` |
| drgn 加载失败 | `{"error": "drgn_load_failed", "details": "..."}` |
| query 不在枚举内 | LLM tool schema 应已校验；如果到达，返回 schema_error |

---

## 5. 与其他模块的集成

| 模块 | 集成方式 |
|------|---------|
| M22 ReAct Loop | M9 工具仅在 `route ∈ {kernel+vmcore, unknown}` 路由暴露 |
| M11 Hardware Layer | `parse_taint_flags` 输出 `M` 位 → 提示 ReAct 调 M11 |
| Triage (`detect_taint_and_hw_signals`) | 共享 taint_flags 位映射表 |
| Architecture §8.0 vmcore 传输 | M9 假设 vmcore 已在 `/tmp/diag/<id>/`；不负责传输 |
| M23 eval | 工具调用轨迹通过 `agent_tool_trace` 表流入 eval |

---

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| drgn OLK 不兼容 | spike 验证；crash fallback 路径 |
| vmcore 巨大（>20GB）导致传输/分析超时 | analyze_vmcore 内部分块加载；query 都有 30s timeout |
| debuginfo 仓库不可访问 | 多镜像 + sosreport 包内查找 + 缓存 |
| 容器版本与 vmcore kernel 版本对齐 | 容器化时 `kernel-debuginfo` 装多个版本；动态 chroot 切换 |
| LLM 误用 query | 封闭枚举，schema 强校验 |

---

## 7. 实施任务对照（[ProjectPlan](../ProjectPlan.md)）

| Task ID | 内容 | PD |
|---------|------|----|
| T-013 | drgn 集成 + OLK 兼容 spike + 容器化 | 13 |
| T-014 | `fetch_debuginfo` 实现 | 3 |
| T-015 | `decode_stacktrace` | 2 |
| T-016 | `parse_taint_flags` + 位映射表 | 1 |
| T-017 | `analyze_vmcore` 5 个 query mode | 5 |
| T-018 | M9 容器化封装 | 2 |
| T-023 | M9 集成测试（with M11）| 2（M9 部分）|

合计约 28 PD（v2.0 M14 主线）。

---

*参考：[Architecture.md](../Architecture.md) · [ADR-020](../adr/ADR-020-drgn-over-crash.md) · [M11](M11_hardware_layer.md)*

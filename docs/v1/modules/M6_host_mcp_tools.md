# M6 — 主机 MCP 工具集 设计文档

> **⚠️ 设计规格文档**：本文档为实施前的原始设计规格（定稿于 2026-05-15）。实际实现以代码为准，两者可能存在偏差。如需了解当前实现状态，请阅读对应目录下的 `CLAUDE.md` 和源代码。


| 字段 | 值 |
|------|---|
| 模块编号 | M6 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M5](M5_retrieval_orchestration.md) · [M7（待写）] · [Architecture](../Architecture.md) |
| 关联 ADR | [ADR-014](../adr/ADR-014-m6-scope.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

### 1.1 目标

把用户上传的故障证据文件（dmesg / journalctl export / sosreport.tar.xz）解析为**结构化、可被 M7 诊断 agent 消费**的字段，作为独立 MCP server 暴露给 agent。

### 1.2 关键决策（[ADR-014](../adr/ADR-014-m6-scope.md)）

| # | 决策 |
|---|------|
| D1 | **数据获取仅支持文件上传**（不接 SSH 远程）|
| D2 | **v1.0 工具集 = mcp-dmesg-journal + mcp-sosreport**（perf / ftrace 延后至 v1.1）|
| D3 | **vmcore / crash / drgn 全部移除 v1**（延后至 v2，待用户场景验证）|
| D4 | **LKML / Bug / commit 不单独建 MCP**，由 M5 retrieve + 低阶 API 提供 |

### 1.3 在范围

| 工具 | 输入 | 输出 |
|------|------|------|
| mcp-dmesg-journal | dmesg 文本 / journalctl JSON export | oops/panic 结构化 + 时间窗事件 + 模块映射 |
| mcp-sosreport | sosreport.tar.xz | 解包后的关键文件目录 + 系统快照摘要 |

### 1.4 不在范围

| 项 | 处理位置 |
|----|---------|
| vmcore / crash dump 分析 | v2+ |
| perf.data / ftrace 解析 | v1.1+ |
| SSH 到目标主机执行命令 | 不做（[ADR-014](../adr/ADR-014-m6-scope.md)）|
| LKML / Bug 数据访问 | M5 retrieve + 低阶 API |
| CodeGraph 工具调用 | M4 CodeGraph HTTP client |
| 写操作（如 sysctl 修改） | 永不做（只读边界硬约束）|

## 2. 部署形态

```
┌─────────────────────────────────────────────────────────────────────┐
│  Linux-Diag-Agent                                                   │
│                                                                     │
│  ┌──────────────────┐   ┌──────────────────┐  ┌──────────────────┐  │
│  │ CLI / Web API    │ → │ M7 Diagnosis     │ → │ M5 Retrieval     │  │
│  │ user uploads     │   │   Agent          │   │   (CodeGraph +   │  │
│  │ file via CLI     │   │                  │   │    PG/Neo4j)     │  │
│  └──────────────────┘   └─────────┬────────┘   └──────────────────┘  │
│         │                         │                                 │
│         │ stores                  │ MCP stdio/HTTP                  │
│         ▼                         ▼                                 │
│  data/uploads/<run_id>/    ┌──────────────────┐  ┌────────────────┐ │
│  ├── dmesg.txt             │ mcp-dmesg-       │  │ mcp-sosreport  │ │
│  ├── journalctl.json       │ journal          │  │                │ │
│  └── sosreport.tar.xz      │ (M6)             │  │ (M6)           │ │
│                            └──────────────────┘  └────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

每个 M6 工具作为独立 FastMCP server 启动，可被 M7 agent 通过 MCP 调用。运行时不依赖网络（纯本地文件解析）。

## 3. mcp-dmesg-journal

### 3.1 范围

解析两类文本日志：
- **dmesg 文本**（来自 `dmesg` 或 `cat /var/log/dmesg`，含 oops / panic / lockup 报告）
- **journalctl JSON export**（来自 `journalctl -o json` 导出）

### 3.2 MCP 工具

| Tool | 输入 | 输出 |
|------|------|------|
| `parse_dmesg` | `file_path: str` | OopsReport / LockupReport / OOMReport / 普通事件列表 |
| `extract_oops_signature` | `file_path: str` | OopsSignature（栈签名 sha256 + 栈帧序列）|
| `query_journal` | `file_path: str, unit: str \| None, time_window: tuple \| None, level: int \| None` | JournalEntries 列表（结构化）|
| `find_panic_window` | `file_path: str` | 包围 panic 前后的关键事件（默认前 5min + 后 1min）|
| `extract_lockdep_report` | `file_path: str` | LockdepReport（含 lock 持有链）|
| `extract_oom_kill` | `file_path: str` | OOMReport（含 gfp_mask / order / triggering_pid / killed_pid）|

### 3.3 输出 Schema

```python
@dataclass
class OopsReport:
    timestamp: datetime           # dmesg 时间戳
    kernel_version: str | None    # "5.10.198", "6.6.30"...（从 banner 抽）
    panic_type: str               # "BUG:", "WARNING:", "Unable to handle..."
    panic_message: str
    stack_frames: list[StackFrame]
    stack_signature: str          # sha256(规范化栈帧序列)
    cpu: int | None
    pid: int | None
    process_name: str | None
    modules_loaded: list[str]     # Modules linked in: ...
    tainted_flags: list[str]      # Tainted: G   OE  ...
    raw_text: str                 # 原始 oops 文本块（截断 ~5K）

@dataclass
class StackFrame:
    index: int                    # 0 = top
    function: str                 # "oom_kill_process"
    offset: str | None            # "+0x142/0x250"
    module: str | None            # "kernel/i915/..."
    raw_line: str

@dataclass
class OOMReport:
    timestamp: datetime
    gfp_mask: str                 # "GFP_KERNEL|__GFP_NOFAIL"
    order: int                    # 4
    zone: str                     # "Normal"
    triggering_task: str          # 触发的 task name
    triggering_pid: int
    killed_task: str | None       # OOM killer 杀的 task
    killed_pid: int | None
    nodes_info: list[dict]        # Node 0 normal: ...

@dataclass
class LockupReport:
    timestamp: datetime
    lockup_type: Literal['soft', 'hard']
    duration_seconds: int         # "watchdog: BUG: soft lockup - CPU#0 stuck for 22s!"
    cpu: int
    pid: int
    function: str                 # top of stack
    stack_frames: list[StackFrame]
    raw_text: str

@dataclass
class LockdepReport:
    timestamp: datetime
    lock_classes: list[str]
    holding_chain: list[dict]     # 持锁顺序
    deadlock_circular: bool
    raw_text: str

@dataclass
class JournalEntry:
    timestamp: datetime
    unit: str | None
    level: int                    # 0-7 syslog
    message: str
    pid: int | None
    comm: str | None              # process name
    metadata: dict                # 原始 journalctl JSON
```

### 3.4 实现要点

```
parse_dmesg pipeline:
  1. 读 dmesg 文本（容忍 timestamp 或 [    0.000] 两种格式）
  2. 多段扫描：oops / WARNING / lockup / OOM 各自有起始 marker
  3. 每段抽 stack frame：regex `\[\s*<\w+>\]\s+(\w+)\+0x[0-9a-f]+/0x[0-9a-f]+`
  4. 规范化 stack frame（去 offset / address）→ sha256 → stack_signature
  5. 抽 banner / Modules linked in / Tainted
  6. 返回结构化对象
```

依赖：纯 Python regex，无外部工具。

### 3.5 与 CodeGraph 联动

`extract_oops_signature` 返回的 `stack_frames` 可被 M7 直接用于：
- 调 `codegraph.lookup_symbol(symbol=top_function)` 拉源码位置
- 调 `M5.retrieve(stack_frames=[...])` 拉历史相似 oops

M6 本身**不**主动调 CodeGraph（保持工具独立性 + 单职责）。

## 4. mcp-sosreport

### 4.1 范围

解包 sosreport.tar.xz（Red Hat / openEuler / SUSE 标准格式），提取关键内核 / 系统状态文件，输出结构化系统快照。

### 4.2 MCP 工具

| Tool | 输入 | 输出 |
|------|------|------|
| `extract_sosreport` | `archive_path: str` | 解包目录路径 + manifest |
| `read_sos_file` | `extract_dir: str, file_path: str` | 文件内容（限制 2MB）|
| `query_sysctl` | `extract_dir: str, key: str` | sysctl 值 |
| `query_proc` | `extract_dir: str, file_path: str` | /proc/<file> 内容 |
| `list_loaded_modules` | `extract_dir: str` | lsmod 解析 |
| `get_system_summary` | `extract_dir: str` | SystemSummary（关键字段汇总）|

### 4.3 输出 Schema

```python
@dataclass
class SystemSummary:
    hostname: str | None
    kernel_release: str           # 5.10.198-12.0.1.olk6_x86_64
    kernel_version_display: str   # 推断为 v6.6 / v5.10（M5 路由用）
    arch: str                     # x86_64 / aarch64
    distro: str                   # openEuler / RHEL / ...
    uptime_seconds: int | None
    loaded_modules: list[str]
    cmdline: str                  # /proc/cmdline
    memory_total_kb: int
    memory_free_kb: int
    cpu_count: int
    
    # 故障线索
    last_oops_in_dmesg: bool       # sos_commands/dmesg/dmesg 内是否含 oops
    has_vmcore: bool               # sos_commands/kdump/kdump.conf + /var/crash/ 是否含 vmcore
    has_kdump_enabled: bool
    
    files_index: dict              # {file_path: size_bytes} 解包后 manifest

@dataclass
class SosManifest:
    extract_dir: str               # 解包目录绝对路径
    archive_sha256: str
    extracted_at: datetime
    file_count: int
    total_size_bytes: int
```

### 4.4 实现要点

```
extract_sosreport pipeline:
  1. 校验扩展名 .tar.xz / .tar.gz / .tar
  2. 用 Python tarfile + lzma 安全解包到 data/uploads/<run_id>/sos_extracted/
     - 拒绝 path traversal（".." 路径）
     - 限制单文件 100MB、总解包 5GB
  3. 解析 sos_commands/general/dmidecode -> hostname / vendor
  4. 解析 sos_commands/kernel/uname -> kernel_release
  5. 解析 etc/os-release -> distro
  6. 解析 sos_commands/kernel/cat_-proc-cmdline -> cmdline
  7. 解析 sos_commands/memory/free_-m -> memory
  8. 解析 sos_commands/kernel/lsmod -> loaded_modules
  9. grep sos_commands/dmesg/dmesg for "Oops" → last_oops_in_dmesg
  10. 输出 SystemSummary + 落 manifest 到 PG `ingest_runs` 副表（仅记录解包元数据）
```

### 4.5 内核版本映射

从 `kernel_release`（如 `5.10.198-12.0.1.olk6_x86_64`）映射到 CodeGraph repo 名：

```python
def map_to_codegraph_repo(kernel_release: str) -> str | None:
    """5.10.x → olk-kernel-v5.10；6.6.x → olk-kernel-v6.6"""
    m = re.match(r'^(\d+)\.(\d+)\.', kernel_release)
    if not m:
        return None
    major, minor = m.group(1), m.group(2)
    
    # OLK 命名约定
    if major == '5' and minor == '10':
        return 'olk-kernel-v5.10'
    if major == '6' and minor == '6':
        return 'olk-kernel-v6.6'
    return None  # 未知版本，agent 决定如何降级
```

## 5. 用户上传协议

### 5.1 CLI 入口

```bash
diag-agent diagnose \
  --upload dmesg=path/to/dmesg.txt \
  --upload journal=path/to/journalctl.json \
  --upload sosreport=path/to/sosreport.tar.xz \
  --description "kernel panic during boot on v5.10 production server" \
  --output report.md
```

每个 `--upload <kind>=<file>`：
- `kind` ∈ {dmesg, journal, sosreport, perf, ftrace}（v1.0 只用前三个）
- 文件被 copy 到 `data/uploads/<run_id>/<kind>/<original_name>`
- run_id 是 UUID，每次 diagnose 调用一份独立目录

### 5.2 上传 manifest

CLI 生成的 manifest 落到 `data/uploads/<run_id>/manifest.json`：

```json
{
  "run_id": "abc-123-def",
  "created_at": "2026-05-14T12:00:00Z",
  "description": "kernel panic during boot on v5.10 production server",
  "uploads": [
    {"kind": "dmesg", "original": "dmesg.txt", "stored": "dmesg/dmesg.txt", "size": 4523, "sha256": "..."},
    {"kind": "sosreport", "original": "sosreport-prod1-2026-05-14.tar.xz", "stored": "sosreport/sosreport.tar.xz", "size": 52428800, "sha256": "..."}
  ]
}
```

agent 启动时把 manifest 喂给 M7（diagnose 入口参数）。

### 5.3 清理策略

- `data/uploads/<run_id>/` 默认保留 30 天（用于复盘 / 评测）
- 用户可显式 `diag-agent purge --run-id <id>` 删除
- 月度 cron 清理过期目录

## 6. 沙箱与超时

| 项 | v1.0 策略 |
|----|----------|
| 单 MCP 工具调用 timeout | 60s（sosreport 解包稍长用 120s）|
| 解包大小硬限 | 5 GB（防恶意 archive bomb）|
| 单文件大小硬限 | 100 MB |
| 内存限制 | 工具进程 `ulimit -v 4G`（v1.0 简化，v1.1 评估 cgroup）|
| CPU 限制 | 单进程默认；v1.1 可考虑 nice / cgroup |
| 文件系统访问 | 限制在 `data/uploads/<run_id>/` 与解包子目录下 |
| 网络访问 | 全部 M6 工具应**不访问网络**（纯文件解析）|
| 写权限 | 仅写 `data/uploads/<run_id>/` 下解包目录 |

## 7. 与其他模块的契约

### 7.1 与 M5 的关系

M6 输出（OopsReport / OOMReport / SystemSummary 等）是 M7 的输入。M7 拿到结构化字段后**调 M5 retrieve(query)**做证据召回。M6 不直接调 M5。

### 7.2 与 M7 的关系

M7 是 M6 的主要消费者。典型流程：

```
M7 调度顺序：
  1. M7 接到 diagnose 请求（manifest 路径）
  2. M7 调 M6 mcp-sosreport.extract_sosreport → 拿到解包目录 + SystemSummary
  3. M7 调 M6 mcp-dmesg-journal.parse_dmesg → 拿到 OopsReport 列表
  4. M7 调 M6 extract_oops_signature → 拿到 stack_signature + frames
  5. M7 构造 RetrievalQuery：
       - kernel_version 从 SystemSummary.kernel_version_display
       - stack_frames 从 OopsReport
       - fault_domain 从 panic_type 推断
  6. M7 调 M5 retrieve(query) → 拿到 evidence 列表
  7. M7 进入 hypothesis-driven 诊断流程（见 M7 文档，待写）
```

### 7.3 与 CodeGraph 的关系

M6 工具**不直接调 CodeGraph**。M6 输出结构化字段后，由 M7 或 M5 决定何时调 CodeGraph（如 lookup_symbol）。这样保持 M6 单职责。

### 7.4 输出 ref 命名规范

M6 输出的字段如需被 Claim-Evidence Binding 引用，统一命名：

| 来源 | ref 格式 | 示例 |
|------|---------|------|
| dmesg 报告 | `dmesg:<run_id>:line=<line_no>` | `dmesg:abc-123:line=12453` |
| OOM 报告 | `dmesg:<run_id>:oom@<line_no>` | `dmesg:abc-123:oom@12453` |
| sosreport 文件 | `sosreport:<run_id>:<path_in_archive>` | `sosreport:abc-123:sos_commands/memory/free_-m` |
| journal 条目 | `journal:<run_id>:cursor=<cursor>` | `journal:abc-123:cursor=s=abc;i=...` |

这些 ref 与 M5 Evidence.ref 一起被 M7 报告引用。

## 8. 文件结构

```
mcp_servers/
├── __init__.py
├── dmesg_journal/
│   ├── __init__.py
│   ├── server.py                    # FastMCP server entry
│   ├── parser/
│   │   ├── dmesg.py                 # oops/panic/lockup/OOM 正则与状态机
│   │   ├── journal.py               # journalctl JSON 解析
│   │   ├── lockdep.py
│   │   └── signature.py             # stack 规范化 + sha256
│   └── tests/
│       └── fixtures/                # 各类 oops 样本
└── sosreport/
    ├── __init__.py
    ├── server.py
    ├── extractor.py                 # tarfile + safe extract
    ├── parser/
    │   ├── system_summary.py
    │   ├── modules.py
    │   ├── cmdline.py
    │   └── memory.py
    └── tests/
        └── fixtures/                # 缩减版 sosreport 样本

ui/
└── cli/
    └── diagnose.py                  # 用户 CLI 入口（含 --upload）
```

## 9. 测试策略

| 层 | 测什么 |
|----|--------|
| 单元 | 各正则、stack 规范化、tar safe extract |
| 集成 | 真实 oops / sosreport 样本端到端跑通 |
| Fixture | 至少 5 类 oops（NULL deref / soft lockup / OOM / KASAN / WARNING）+ 2 个 sosreport |
| 安全 | 恶意 archive（path traversal、bomb）拒绝 |
| 性能 | dmesg 解析 < 1s / 1MB；sosreport 解包 < 60s / 100MB |

## 10. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| 不同发行版 dmesg 格式差异 | 解析失败率 | 多 fixture + 解析器多版本 fallback |
| sosreport 版本差异（sos v3 vs v4 目录结构变）| 关键文件 not found 比例 | 多版本兼容 + 优雅降级 |
| stack frame regex 漏抽 | 评测时 oops 抽取准确率 | 人工抽样校验 + 加更多 fixture |
| sosreport bomb | 解包大小监控 | 5GB 硬限 + 单文件 100MB 硬限 |
| 用户上传敏感数据 | 日志中是否含 IP / hostname / 密钥 | M6 不主动脱敏；M7 在送 LLM 前做脱敏（参考 [Architecture §6.1](../Architecture.md)）|

## 11. v1.0 → v1.3 演进

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | mcp-dmesg-journal + mcp-sosreport 全功能上线；30 例评测 fixture 完整 |
| v1.1 | 加 mcp-perf（perf.data 火焰图与热点）；perf-tools 链路开通 |
| v1.2 | 加 mcp-ftrace（trace.dat 解析），若评测显示需要 |
| v1.3 | 评估是否引入 mcp-bpftool（eBPF）；评估 SSH 远程模式（v2 候选）|
| v2 | 重启 vmcore / crash / drgn 分析（基于 v1 学到的输入分布做更靠谱的设计） |

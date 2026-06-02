# M6 · MCP Host Tools (dmesg / sosreport)

## 1. 目的

把"用户提供的原始机器数据"解析成结构化对象，供 M7 triage 节点消费。两个独立 parser：

- **dmesg_journal**: 从 raw dmesg / `journalctl -k` 文本抽取 `KernelEvent` 列表（OOM / panic / oops / lockup / hung_task 等）
- **sosreport**: 解压 sosreport `.tar.xz` 或目录，提取 hostname / kernel_version / OLK tag / dmesg tail

> 为什么叫 "MCP"：原计划这两个 parser 作 MCP server 跑（让其它 agent 也能用），后来发现项目内单进程调用更省事，**当前只作为 Python 库**，但命名保留 MCP 前缀以备将来拆分。

## 2. 公共接口

### 2.1 dmesg_journal

```python
from enum import Enum
from dataclasses import dataclass

class EventKind(str, Enum):
    oom         = "oom"
    softlockup  = "softlockup"
    hardlockup  = "hardlockup"
    rcu_stall   = "rcu_stall"
    panic       = "panic"
    oops        = "oops"
    bug         = "bug"
    warn        = "warn"
    lockdep     = "lockdep"

@dataclass
class KernelEvent:
    kind: EventKind
    summary: str                       # 一行人话总结
    raw: str                           # 原始 block（事件相关 60 行内）
    call_trace: list[str]              # 抽出的栈帧函数名（剥前缀后）
    metadata: dict[str, Any]           # event-specific：pid / comm / score / cpu / order ...

    def to_dict(self) -> dict: ...     # 序列化

def extract_events(log_text: str) -> list[KernelEvent]:
    """解析 raw dmesg / journald，返回检测到的事件列表（按出现顺序）。"""
```

### 2.2 sosreport

```python
@dataclass
class SystemSummary:
    hostname: str | None
    kernel_version: str | None
    olk_version_tag: str | None
    uptime_seconds: float | None
    dmesg_tail: str | None             # 最后 N 行 dmesg
    os_release: dict[str, str]         # /etc/os-release 解析

def parse_sosreport(path: str | Path) -> SystemSummary:
    """支持 .tar.xz / .tar.gz / .tar.bz2 归档或解压后目录。"""
```

## 3. dmesg extractor 算法

### 3.1 主流程

```
1. log_text.splitlines()
2. for each line, strip leading [timestamp] prefix: _TIMESTAMP_RE
3. 顺序尝试 _match_event 的 9 个 pattern
4. 命中后 _collect_block 取上下文（默认 60 行，OOM 30 行）
5. 从 block 中 _extract_call_trace_frames 抽栈帧函数名
6. 返回 KernelEvent
```

### 3.2 各事件正则（**严格按此实现**）

```python
# Timestamp prefix（先剥）
_TIMESTAMP_RE = re.compile(r"^\[\s*[\d.]+\]\s*")

# Panic（最高优先）
_PANIC_RE = re.compile(r"Kernel panic[- ]+not syncing:\s*(.+?)(?:\n|$)", re.I)

# OOM —— 3 种形态都要识别（详见 `60_GOTCHAS.md` §5.2 / M7 § 7 #2）
_OOM_CONSTRAINT_RE = re.compile(
    r"oom-kill:constraint=(\S+?),.*?task=(\S+),\s*pid=(\d+)", re.I
)   # 现代 head: oom-kill:constraint=CONSTRAINT_MEMCG,...,task=java,pid=N

_OOM_KILLED_RE = re.compile(
    r"Out of memory:\s+Killed\s+process\s+(\d+)\s+\((\S+)\)", re.I
)   # 6.x: Out of memory: Killed process N (java) total-vm:...

_OOM_RE = re.compile(
    r"(?:Out of memory:\s+)?Kill process\s+(\d+)\s+\((\S+)\)\s+score\s+(\d+)", re.I
)   # 5.x legacy: Kill process N (java) score N

# 内存分配失败
_ALLOC_FAIL_RE = re.compile(
    r"(\S+):\s+page allocation failure:\s*order:(\d+)", re.I
)

# Hung task
_HUNG_TASK_RE = re.compile(
    r"task\s+(\S+):(\d+)\s+blocked for more than (\d+) seconds", re.I
)

# BUG / WARNING
_BUG_RE  = re.compile(r"BUG:?\s+(.+?)(?:\n|$)", re.I)
_OOPS_RE = re.compile(r"Oops:\s*(\S+)", re.I)
_WARN_RE = re.compile(r"WARNING:?\s+(.+?)(?:\n|$)", re.I)

# Lockup / RCU
_SOFT_LOCKUP_RE = re.compile(r"soft lockup.*?CPU#(\d+)", re.I)
_HARD_LOCKUP_RE = re.compile(r"NMI watchdog.*?Hard LOCKUP.*?CPU (\d+)", re.I)
_RCU_STALL_RE   = re.compile(r"RCU.*?stall detected.*?CPU (\d+)", re.I)

# Lockdep
_LOCKDEP_RE = re.compile(
    r"WARNING: possible (circular locking dependency|deadlock)", re.I
)

# Call trace 抽取
_CALL_TRACE_START = re.compile(r"Call Trace:", re.I)
# 栈帧：可能带 [<addr>] 前缀，肯定有 `name+0xHEX/0xHEX` 形态
_CALL_FRAME_RE = re.compile(
    r"^\s*(?:\[<?[0-9a-f]+>?\]\s+)?(\w\S+\+0x[0-9a-f]+/0x[0-9a-f]+)", re.I
)
```

### 3.3 优先级（同一行可能命中多 pattern）

`Panic > OOM (3 种) > AllocFail > HungTask > BUG > WARN > SoftLockup > HardLockup > RcuStall > Lockdep`

### 3.4 `_collect_block`

从匹配行往后取 max_lines（默认 60，OOM 30），在以下任一情况停：
- 空行 + 已经收过至少一个 frame
- 遇到下一个 timestamp 跳变 > 1 秒（不同事件块）
- max_lines 用完

返回 `(raw_block, frame_list)`。frame 抽取在剥前缀后做。

## 4. sosreport parser

### 4.1 归档支持

`.tar.xz` / `.tar.gz` / `.tar.bz2`（通过 tarfile 自动识别）+ 已解压目录。

### 4.2 必读文件（按存在性 fallback）

```python
hostname:    'hostname'  or  'etc/hostname'  or  'sos_commands/host/hostname'
kernel_ver:  'sos_commands/kernel/uname_-a'  or  'uname'
os_release:  'etc/os-release'
uptime:      'uptime'  or  'sos_commands/host/uptime'
dmesg_tail:  'sos_commands/kernel/dmesg'  or  'var/log/dmesg'  (取最后 200 行)
```

### 4.3 `_enrich(summary)` 推导

- `olk_version_tag`：从 `kernel_version` 抽（用 `agent/triage/nodes.py::_extract_kernel_version` 的同款正则；OLK 标签 = `OLK-<major>.<minor>` 当含 `oeNNNN` 时）
- `uptime_seconds`：解析 `up X days, Y hours, Z minutes` 或 `/proc/uptime` 数字

## 5. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **`_TIMESTAMP_RE` 必须先剥**：生产 dmesg 都带 `[12345.678]` 前缀，不剥后续所有正则都 0 命中 | 见 `60_GOTCHAS.md` §5.2 |
| C2 | **OOM 三种形态都要识别**：legacy `Kill process score N` / 6.x `Killed process` / canonical `oom-kill:constraint=` 头 —— 任一命中算 OOM | 内核版本跨度 5.x ~ 6.x，三种都见过 |
| C3 | **优先级固定**（§ 3.3）：panic 最高，避免 panic 被 BUG 抢先匹配 | panic 通常带 BUG 字样 |
| C4 | **call trace 抽取必须只在 "Call Trace:" 之后**：扫到 `Call Trace:` 开关，扫到 `RIP:` / `Code:` / `---[ end trace ]---` 关闭 | 否则会把无关代码上下文抽进 trace |
| C5 | **抽出的 frame 是函数名**（不含 `+0xN/0xM` 偏移），适合喂 find_commits_touching_symbol | symbol 反查只用函数名 |
| C6 | **dmesg_tail 取最后 N 行**，不要从头读：OOM 等关键事件在结尾 | `tail -200` 行为 |

## 6. 已知陷阱（M6 独有）

### #1 · tar 文件可能含 absolute path

恶意 / 早期 sosreport 归档里 member 名可能是 `/etc/hostname`（绝对路径）。tarfile 默认拒绝抽取（CVE-2007-4559），但**列出 members 时**仍会出现。匹配时要 normalize：

```python
def _normalize(name: str) -> str:
    return name.lstrip("/")  # 去前导 /

# 然后匹配
for m in tar.getmembers():
    n = _normalize(m.name)
    if n in {"hostname", "etc/hostname"}:
        ...
```

### #2 · 大 sosreport 归档（>1 GB）

完整 sosreport 含 `/var/log/journal` 二进制 + `core.*` dump 文件，归档可以 1-10 GB。**不要全部解压到 /tmp**，只解析 § 4.2 列的必读文件即可（按需 `tar.extractfile(member)` 流式读）。

### #3 · uptime 格式漂浮

各发行版 `uptime` 命令输出格式不同：

```
 14:32:01 up 2 days,  3:45,  4 users,  load average: 0.12, 0.23, 0.45
up 2 hours, 15 minutes
3713.42 5882.27       ← /proc/uptime 纯数字
```

实现要按几种分别 try parse，不识别就返回 `None`（不抛错）。

### #4 · journalctl -k 没有 `[timestamp]` 前缀

如果用户喂的是 `journalctl -k | head` 输出（而非 `dmesg`），行起始是 ISO 日期：

```
Jun 02 14:32:01 host kernel: oom-kill:constraint=CONSTRAINT_MEMCG,...
```

`_TIMESTAMP_RE` 只匹配 `[\d.]+`，对 ISO 不生效。要么用户先 strip，要么 dmesg extractor 加第二个时间戳正则。**当前实现按"假定剥过 ISO 前缀"** —— 文档要在 `90_FAQ.md` 提示用户。

> **see also**：`60_GOTCHAS.md` §5（Agent / Diagnosis 整章，dmesg parser 在 §5.2）

## 7. 验收

### 单元：dmesg 三种 OOM 都识别

```python
samples = [
    # 1. canonical head (现代)
    "[12345.677000] oom-kill:constraint=CONSTRAINT_MEMCG,task=java,pid=8821",
    # 2. 6.x Killed process
    "[12345.677500] Out of memory: Killed process 8821 (java) total-vm:...",
    # 3. 5.x legacy
    "[ 142.312462] oom_kill_process: Kill process 1234 (java) score 800 or sacrifice child",
]
for s in samples:
    events = extract_events(s)
    assert len(events) == 1 and events[0].kind == EventKind.oom
```

### 单元：call trace 抽取

```python
dmesg = """[12345.680111] Call Trace:
[12345.680112]  <TASK>
[12345.681001]  dump_stack_lvl+0x4d/0x6c
[12345.683003]  oom_kill_process+0x10c/0x110
[12345.685005]  mem_cgroup_out_of_memory+0xed/0x100"""
events = extract_events(dmesg)
assert events  # 即使没有 OOM head，可能抽到 BUG/WARN 后的 trace
# 或单独测 extract_call_trace_frames(dmesg)
```

### 集成

```python
summary = parse_sosreport("/path/to/sosreport.tar.xz")
assert summary.hostname
assert summary.kernel_version          # e.g. "6.6.0-21.0.0.21.oe2403.x86_64"
assert summary.olk_version_tag         # e.g. "OLK-6.6"
assert summary.dmesg_tail              # 非空
```

## 8. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | Python 标准库（tarfile / re）；无第三方依赖（关键设计：parser 必须 standalone，便于做 MCP server） |
| **被调用** | M7 triage `extract_events` 节点（dmesg）+ `parse_input` / `extract_events` 节点（sosreport） |
| **被调用** | M7 ReAct 工具 `parse_dmesg` / `parse_sosreport` / `extract_call_trace` / `parse_taint_flags`（详见 `30_TOOL_CONTRACTS.md`） |

---

> **重建校对**：跑 § 7 三段测试通过；在 `cases_smoke.json` 的 raw_input 上调 `extract_events()`，验证返回 1 个 `kind=oom` 事件且 `metadata.task == "java"`。

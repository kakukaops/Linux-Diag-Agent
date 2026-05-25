# M11 — Hardware & Firmware Layer 设计文档

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格（2026-05-20）。

| 字段 | 值 |
|------|---|
| 模块编号 | M11（v2 新增，类别 H）|
| 状态 | Design Draft |
| 关联文档 | [../PRD.md](../PRD.md) · [../Architecture.md](../Architecture.md) · [M9](M9_crash_forensics.md) |
| 关联 ADR | [ADR-023](../adr/ADR-023-hardware-first-sop-routing.md) |
| 最后更新 | 2026-05-20 |

---

## 1. 目标与边界

### 1.1 目标

为 ReAct agent 提供**硬件与固件层的诊断能力**，修复 v1 把 hardlockup / panic 系统性误指 kernel commit 的陷阱（[ADR-023](../adr/ADR-023-hardware-first-sop-routing.md)）。

**关键命题**：很多"内核 panic"其实是硬件故障（坏 DIMM、MCE、微码、固件）。dmesg 出现 `Hardware Error` / `Machine Check` 时，agent 应立刻停止搜 kernel commit，转向硬件诊断。

### 1.2 关键决策

| # | 决策 |
|---|------|
| D1 | 封装业界成熟工具：`mcelog`、`ras-mc-ctl` / `edac-util`、`ipmitool`、`dmidecode`——**不重造** |
| D2 | hardware SOP yaml 作为本模块产物（路由触发条件见 [ADR-023](../adr/ADR-023-hardware-first-sop-routing.md)）|
| D3 | 远程节点通过 **SSH only-read** 用户执行；本地节点直接执行 |
| D4 | 报告**明确不引用 kernel commit**（[ADR-023 §阶段 3](../adr/ADR-023-hardware-first-sop-routing.md)）|
| D5 | 检测规则**保守**：宁可漏路由（fallback kernel）也不要错路由（软件 bug → hardware） |

### 1.3 在范围

- 4 个核心硬件查询工具
- hardware SOP yaml 完整定义（[ADR-023](../adr/ADR-023-hardware-first-sop-routing.md) 提到延后到本模块）
- MCE 错误码映射表（CPU / 内存子类）
- IPMI SEL 事件解释规则
- BMC / 固件版本检查
- 与 M9 共享 taint flags 处理

### 1.4 不在范围

- 硬件修复 / RMA 自动化（仅出建议）
- 厂商特定 BMC 高级 API（如 Dell iDRAC、HPE iLO）—— v2 仅 ipmitool 通用接口
- live MCE 注入测试
- 跨架构（x86_64 + aarch64 覆盖；其他不做）

---

## 2. 部署形态

```
┌─────────── M11 部署 ───────────┐
│                                │
│ 本地节点（默认）:               │
│   apt/dnf install mcelog \    │
│     rasdaemon ipmitool \      │
│     dmidecode                  │
│                                │
│ 远程节点:                       │
│   SSH user@host \              │
│     -- ipmitool sel list       │
│   （ssh 用户必须是 read-only） │
└────────────────────────────────┘
              ↓
   M11 MCP server（FastAPI）
              ↓
   agent ReAct loop（M13）
```

不容器化（同 M9）—— mcelog / ipmitool 直接在主机上调；如果远程则 SSH。

---

## 3. MCP 工具

### 3.1 `get_mce_log`

```python
{
  "name": "get_mce_log",
  "description": "Retrieve Machine Check Exception (MCE) records from mcelog.",
  "parameters": {
    "node": {"type": "string", "description": "hostname or 'local'"},
    "since": {"type": "string", "description": "ISO datetime; default last 24h"},
  }
}
```

**返回**：

```json
{
  "events": [
    {
      "timestamp": "2026-05-19T14:30:01Z",
      "cpu": 12,
      "bank": 2,
      "mci_status": "0xff20000080000900",
      "mci_addr": "0x...",
      "error_type": "Memory ECC Uncorrected",   // 解析后的人类可读类型
      "severity": "uncorrected",
      "dimm_location": "DIMM A2",                // 若可从 DMI 解析
      "raw_log": "..."
    }
  ],
  "total_count": 1,
  "severity_breakdown": {"corrected": 23, "uncorrected": 1, "fatal": 0}
}
```

**实现**：`mcelog --client` 取实时事件 + 解析 `/var/log/mcelog` 历史；用 MCE 错误码映射表（`m11/mce_codes.py`）翻译 mci_status 为人类可读类型。

### 3.2 `get_edac_errors`

```python
{
  "name": "get_edac_errors",
  "description": "Retrieve memory ECC errors via ras-mc-ctl/edac-util.",
  "parameters": {
    "node": {"type": "string"},
    "since": {"type": "string", "description": "default last 7d"},
  }
}
```

**返回**：

```json
{
  "summary": {
    "ce_count": 47,        // Corrected Errors
    "ue_count": 1,         // Uncorrected Errors
    "memory_controllers": 2,
    "csrows_with_errors": ["mc0_csrow2"]
  },
  "details_by_dimm": [
    {"dimm": "mc0/csrow2/ch0", "ce_count": 45, "ue_count": 1, "label": "CPU_SrcID#0_Ha#0_Chan#0_DIMM#0"}
  ],
  "trend": "increasing | stable | recent_spike"  // 简单趋势判断
}
```

**实现**：`ras-mc-ctl --summary` + `ras-mc-ctl --errors`；趋势判断基于近 1h vs 近 24h CE 数量比较。

### 3.3 `get_ipmi_sel`

```python
{
  "name": "get_ipmi_sel",
  "description": "Read IPMI System Event Log via ipmitool.",
  "parameters": {
    "node": {"type": "string"},
    "since": {"type": "string"},
    "severity_filter": {"type": "string", "enum": ["all", "critical_only"], "default": "all"}
  }
}
```

**返回**：

```json
{
  "events": [
    {
      "id": "0x012a",
      "timestamp": "2026-05-19T14:29:55Z",
      "sensor": "DIMM_A2",
      "type": "Memory",
      "event": "Uncorrectable ECC | Asserted",
      "severity": "Critical"
    }
  ],
  "interpretation": "1 critical memory event 5 seconds before kernel panic at 14:30 — strong hardware correlation",
  "bmc_clock_skew_seconds": 3   // IPMI 时钟与 OS 时钟偏差,影响时间对齐
}
```

**实现**：`ipmitool sel list` + `ipmitool sel elist`（含 sensor 解析）；维护 IPMI SEL 事件类型映射表。

### 3.4 `get_hardware_inventory`

```python
{
  "name": "get_hardware_inventory",
  "description": "Retrieve hardware inventory and firmware versions via dmidecode.",
  "parameters": {
    "node": {"type": "string"},
  }
}
```

**返回**：

```json
{
  "system": {"vendor": "Dell Inc.", "product": "PowerEdge R750", "serial": "..."},
  "bios": {"vendor": "Dell Inc.", "version": "1.13.2", "release_date": "2026-03-12"},
  "bmc": {"version": "5.10.50.00"},
  "cpu": [{"model": "Intel Xeon Gold 6338", "microcode": "0xd0003a5"}],
  "memory": {
    "dimms": [
      {"slot": "DIMM_A1", "size_gb": 32, "manufacturer": "Hynix", "speed_mts": 3200, "rank": 2},
      {"slot": "DIMM_A2", "size_gb": 32, "manufacturer": "Hynix", "speed_mts": 3200, "rank": 2},
      ...
    ],
    "total_gb": 256
  }
}
```

**实现**：`dmidecode -t system,bios,bmc,processor,memory` 解析。

---

## 4. hardware SOP yaml（完整定义）

在 `agent/sop/definitions/hardware.yaml`（[ADR-023](../adr/ADR-023-hardware-first-sop-routing.md) 注明详细内容在本模块）：

```yaml
name: hardware
description: 疑似硬件故障的诊断 SOP
fault_kinds_triggering:
  - hardlockup
  - panic_with_hw_error
  - mce
trigger_signals:
  # 任一为 true 即路由到此 SOP（详见 ADR-023）
  - has_hardware_error_in_dmesg: true
  - taint_M_set: true
  - mce_event_in_dmesg: true
  - edac_uncorrected_in_last_1h: true

steps:
  - id: hw_step_1
    description: "查 mcelog 定位 MCE 事件"
    tool: get_mce_log
    args:
      since: "now-24h"
    next_if_events_found: hw_step_2
    next_if_no_events: hw_step_3   # 仍要查 EDAC

  - id: hw_step_2
    description: "解析 MCE 错误源（CPU / 内存 / Bus）"
    tool: __internal_parse_mce__
    next: hw_step_3

  - id: hw_step_3
    description: "查 EDAC 累积 ECC 错误"
    tool: get_edac_errors
    next: hw_step_4

  - id: hw_step_4
    description: "查 IPMI SEL 看 OS 视野外的硬件事件"
    tool: get_ipmi_sel
    args:
      since: "now-24h"
      severity_filter: "critical_only"
    next: hw_step_5

  - id: hw_step_5
    description: "拿硬件 inventory 确定 DIMM 槽位、固件版本"
    tool: get_hardware_inventory
    next: conclude

  - id: conclude
    description: "综合输出 RAS 建议"

report_sections:
  - hardware_signals_summary       # 触发本路径的原始信号
  - mce_analysis                    # MCE 错误源汇总
  - edac_trend                      # ECC 累积趋势
  - ipmi_sel_critical              # OS 看不到的硬件事件
  - hardware_inventory_snapshot     # 当前硬件配置
  - recommended_actions             # 例如:
    # - "建议联系硬件维护方 RMA DIMM A2(7 天内 1 UE + 45 CE)"
    # - "BIOS 版本 1.13.2 已知有 MCE 误报问题,建议升级到 1.14+"
    # - "微码 0xd0003a5 比厂商最新版 0xd000405 旧,建议更新"

forbidden:
  - "不引用 kernel commit"   # 强制
  - "不指 kernel bug"        # 即使 ReAct 调了 search_commits 也只是辅助

confidence_threshold:
  high: "MCE uncorrected + EDAC UE + IPMI critical 三者中 ≥2"
  medium: "三者中 1 个 + taint=M"
  low: "仅 taint=M,无具体硬件证据"
```

---

## 5. MCE 错误码映射表（设计要点）

`m11/mce_codes.py` 维护 `mci_status` 整数 → 人类可读类型的映射。来源：Intel SDM Vol 3B Chapter 16 + AMD APM。

```python
MCE_STATUS_DECODE = {
    # bit 63: VAL (valid)
    # bit 62: OVER (overflow)
    # bit 61: UC (uncorrected)
    # bit 60: EN (enabled)
    # bit 59: MISCV (MISC valid)
    # bit 58: ADDRV (ADDR valid)
    # bit 57: PCC (processor context corrupted) — fatal
    # bit 56: S (signaling)
    # bit 55: AR (action required)
    # bits 31:16: MCG status code
    # bits 15:0: MCA error code
}

MCA_ERROR_CODES = {
    # Generic errors
    0x0001: "Generic unclassified error",
    # Memory hierarchy errors
    0x0010: "L1 cache error",
    0x0020: "L2 cache error",
    0x0030: "L3 cache error",
    # ... (Intel SDM table 16-7)
}

def decode_mci_status(value: int) -> dict:
    """Decode raw mci_status to structured fields."""
    return {
        "valid": bool(value & (1 << 63)),
        "uncorrected": bool(value & (1 << 61)),
        "fatal": bool(value & (1 << 57)),   # PCC
        "mca_error_code": MCA_ERROR_CODES.get(value & 0xFFFF, "Unknown"),
        ...
    }
```

---

## 6. 与其他模块的集成

| 模块 | 集成方式 |
|------|---------|
| Triage `detect_taint_and_hw_signals` | M11 暴露 `get_taint_signals_summary` 给 triage 用（轻量,只读 dmesg）|
| M9 `parse_taint_flags` | **共享** taint 位映射表 at `mcp_servers/shared/taint_flags.py`（M-4 修复：三处共享 import）|
| M22 ReAct Loop | M11 工具只在 `route=hardware` 时暴露（避免 kernel route 误引向硬件分析）|
| M7 generate_report | route=hardware 时调 hardware report renderer，不引用 kernel commit |
| 评测 | hw 误诊案例 ≥ 3 例 + 反例 ≥ 2 例（[ADR-023](../adr/ADR-023-hardware-first-sop-routing.md)）|

---

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 软件 bug 触发 hw signal（罕见误判）| hardware SOP 在 conclude 阶段允许 fallback 到 kernel 路径；confidence_threshold 低时不锁死结论 |
| 厂商 BMC 差异（Dell vs HPE vs Inspur）| 仅用 ipmitool 通用命令；厂商特定字段忽略或标 "vendor_specific" |
| mcelog 服务未启动 | 工具返回 `{"error": "mcelog_not_running"}`,提示 SRE 启用 |
| IPMI 凭据缺失 | tool 返回 `{"error": "ipmi_credentials_missing"}`,降级到只看 dmesg/mcelog |
| BMC 时钟与 OS 时钟偏差 | `get_ipmi_sel` 返回 `bmc_clock_skew_seconds`,agent 推理时校正 |
| 误判软件为硬件后无法回退 | hardware SOP `confidence_threshold: low` 时报告中标"待 SRE 二次确认" |

---

## 8. 实施任务对照（[ProjectPlan](../ProjectPlan.md)）

| Task ID | 内容 | PD |
|---------|------|----|
| T-019 | mcelog / EDAC / IPMI / dmidecode 封装（4 个工具）| 5 |
| T-020 | hardware SOP yaml + 路由集成 | 1 |
| T-001 | `detect_taint_and_hw_signals` 主任务在 M7 triage（[V2 supplements §1.1](../V2_v1_modules_supplements.md)）；M11 提供 `get_taint_signals_summary` helper + 共享 taint_flags 表，**不重复计入 PD**（M11 实际贡献含于 §taint_flags 维护工时） | 0 |
| T-023 | M9/M11 集成测试 | 1（M11 部分）|

合计 7 PD（v2.0 M14 部分；T-001 归 M7 triage，M11 不重复计入）。

---

*参考：[Architecture.md](../Architecture.md) · [ADR-023](../adr/ADR-023-hardware-first-sop-routing.md) · [M9](M9_crash_forensics.md)*

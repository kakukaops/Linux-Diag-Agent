# ADR-024 — "无法诊断"出口路径：证据不足时输出"请收集 X"而非强行下结论

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-20 |
| 决策者 | 用户 + Architect |
| 关联模块 | M7（agent/diagnosis）|
| 相关 ADR | [ADR-019](ADR-019-hybrid-react-deterministic.md) |
| 设计基础 | [AgentThink 设计原则 6](../../AgentThink.md) |

## 上下文

v1 的诊断流水线**永远产报告**——`generate_report` 节点是流水线终点，无论证据有多薄弱。`bind_claims` 会保留未验证（unverified=True）的声明，但报告仍呈现完整 RCA 结构，给人"已诊断"的印象。

这在 SRE 实践中是有害的：

| 场景 | 问题 |
|------|------|
| 只有一段截断的 oops trace，无 vmcore | LLM 会基于片段猜测，给出 60% 置信度的"根因"——可能完全错 |
| dmesg 末尾被 logger 截断了关键 stack | 报告读起来煞有介事，但漏了真正的根因 |
| 用户问"昨天系统不稳定"但没贴任何日志 | 流水线还是产报告，纯属编造 |

**给一份基于薄弱证据的自信报告，比说"我不知道"危害大**——前者会误导决策（运维去改不相关的配置），后者只是延误（运维去补证据）。

## 决策

**v2 的 Report 阶段引入显式"无法诊断"出口**：

### 1. 证据强度评估（新加在 `bind_claims` 之后）

引入 `evidence_strength_score`，规则化打分（确定性，不交给 LLM）：

```python
def assess_evidence_strength(state):
    score = 0
    if state.has_full_dmesg:           score += 2
    if state.has_complete_stacktrace:  score += 3
    if state.has_vmcore:               score += 3
    if state.has_sosreport:            score += 2
    if state.has_monitoring_data:      score += 2
    if state.has_taint_decoded:        score += 1
    if state.has_kernel_version_id:    score += 1

    # 关键证据缺失的负分
    if route == "kernel" and not state.has_complete_stacktrace:
        score -= 3
    if route == "kernel+vmcore" and not state.has_vmcore:
        score -= 5

    return score
```

- `score ≥ 5`：证据充分 → 正常报告
- `2 ≤ score < 5`：证据中等 → 报告但加置信度警告
- `score < 2`：证据不足 → **走 insufficient_evidence 出口**

### 1.1 校准计划（必做，避免拍脑袋）

上述权重是 **v2.0 初始值**，必须配合显式校准流程：

| 阶段 | 校准方法 |
|------|---------|
| **v2.0** | 用上面初始权重，**逐例标注**（每例 by 评测人员标"该 say IDK 吗 yes/no"）|
| **v2.1** | 积累 ≥ 30 例标注数据后，用 **logistic regression** 拟合权重，覆盖原始权重；连续 4 周新增数据后再 refit |
| **v2.2** | 接入 eval 反馈环（[ProjectPlan T-204](../ProjectPlan.md)）自动 retrain；权重 + 阈值都进 `configs/evidence_score.yaml`，方便手动 override |

校准产出的权重变更必须有 ADR-024 增订附录或 ADR-025+ 记录决策依据。

### 2. Insufficient-evidence 报告结构

不是"我不知道",而是：

```json
{
  "verdict": "insufficient_evidence",
  "evidence_strength_score": 1,
  "what_we_know": "故障类型疑似 hardlockup，发生时间约 14:30，节点 prod-42",
  "what_we_cannot_determine": [
    "持锁 CPU 的具体调用栈（无 vmcore）",
    "是否为硬件故障（无 mcelog 数据）"
  ],
  "insufficient_evidence_actions": [
    {
      "priority": "P0",
      "action": "enable_kdump",
      "command": "systemctl enable kdump && systemctl start kdump",
      "rationale": "需要 vmcore 才能定位持锁 CPU 栈"
    },
    {
      "priority": "P1",
      "action": "install_debuginfo",
      "command": "dnf install kernel-debuginfo-$(uname -r)",
      "rationale": "符号表用于栈解析"
    },
    {
      "priority": "P1",
      "action": "check_mcelog",
      "command": "mcelog --client && cat /var/log/mcelog",
      "rationale": "排除硬件 MCE 触发"
    }
  ],
  "recommended_next_step": "采集 vmcore 后重新运行: diag-agent diagnose --vmcore /var/crash/..."
}
```

报告 Markdown 形式也明确标题为 **"证据不足报告"** 而非 "RCA 报告"。

### 3. ReAct 阶段也可主动触发

ReAct loop 的终止条件之一就是 LLM 主动输出 `insufficient_evidence`（见 [ADR-019](ADR-019-hybrid-react-deterministic.md)）。具体在 prompt 中明确：

> "若你认为现有工具调用结果不足以确定根因，请输出 `{action: 'insufficient_evidence', missing: [...]}` 而非编造结论。"

LLM 主动触发 + 规则化打分（兜底），双路径保障"该说不知道时说不知道"。

## 出口路径汇总

`generate_report` 节点根据 verdict 选择报告格式：

| verdict | 报告形式 |
|---------|---------|
| `diagnosed` | 标准 RCA 报告（含证据、claims、recommended actions）|
| `insufficient_evidence` | "证据不足"报告（含 what we cannot determine + actions to collect）|
| `hardware_suspected` | "疑似硬件故障"报告（不引用 kernel commit，[ADR-023](ADR-023-hardware-first-sop-routing.md)）|
| `inconclusive` | "未能确定根因"报告（达到 max_iter / budget 但 LLM 未主动 insufficient）|

四种出口都是合法的、设计内的——不是"失败"。

## 影响

### 优势

| 维度 | 影响 |
|------|------|
| 诊断可信度 | 不再产生自信但错误的结论 |
| 运维体验 | 收到"请收集 X"清单比收到错误诊断更有可操作性 |
| 评测 ground truth | 加 "should have said insufficient" 类案例进 eval 数据集，可量化 |
| 与用户期望对齐 | 用户明确表态：宁可说"不知道"也不要乱猜 |

### 代价

| 维度 | 影响 |
|------|------|
| 评估规则维护 | `evidence_strength_score` 规则需要随数据集迭代调整 |
| 报告模板增加 | 需要写 insufficient_evidence_report 模板 |
| 评测复杂度 | acceptance 不只是看"诊断对不对",还要看"该 say IDK 的有没有 say"（双向召回率）|

## 评测指标（新）

| 指标 | 目标 |
|------|------|
| Insufficient-evidence 召回率 | 该 say IDK 的案例中 ≥ 80% 走出口 |
| Insufficient-evidence 精确率 | 走出口的案例中 ≥ 90% 确实证据不足（不应过激）|

## 备选方案（已否决）

### 备选 A：让 LLM 自由判断"能不能诊断"

否决理由：LLM 倾向于自信输出；缺少规则化兜底；单点失败风险高。

### 备选 B：永远输出报告，但加全局置信度

否决理由：置信度数字容易被忽略；用户/运维仍会按"已诊断"对待。

### 备选 C：证据不足直接报错 / 抛异常

否决理由：用户看到的应该是结构化的"请做 X"，不是 stack trace；产品体验差。

## 实施

`agent/diagnosis/nodes.py` 改动：

1. `bind_claims` 之后加 `assess_evidence_strength` 子节点
2. `generate_report` 根据 verdict 分支到不同 render 函数
3. 新增 `agent/report/insufficient_evidence.py` 渲染器

## 验证

- v2.0 acceptance：至少 1 例证据不足案例返回"请收集 X"而非强行下结论（[PRD §6.1](../PRD.md)）
- v2.2 acceptance：insufficient-evidence 出口召回率 ≥ 80%（[PRD §6.3](../PRD.md)）

## 未来回顾

- 跟踪 insufficient-evidence 出口的命中分布（哪些证据缺得最多）
- 据此推动用户的 kdump / debuginfo 默认开启
- 若规则化打分被发现频繁误判（漏报或过激），引入 LLM cross-check 作为辅助

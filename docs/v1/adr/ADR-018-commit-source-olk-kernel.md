# ADR-018 — Commit 来源 = OLK 内核仓为主 + mainline 为辅；双生态桥接策略

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-15 |
| 决策者 | 用户 + Architect |
| 关联模块 | M2, M3, M4, M7 |
| 相关 ADR | [ADR-008](ADR-008-reuse-codesearch.md) · [ADR-009](ADR-009-multi-version-via-codesearch-repos.md) · [ADR-010](ADR-010-lkml-2y-bootstrap.md) · [ADR-011](ADR-011-bugzilla-kernel-org-only.md) |
| 驱动原则 | **诊断信息准确性优先** |

## 上下文

本项目诊断目标是 **OLK 内核（openEuler Linux Kernel）v6.6 + v5.10**。CodeGraph（codesearch）已正确索引 `olk-kernel` repo。

但 M3 §3.6 原设计的 commit ingester 远端配置为 `git://git.kernel.org/.../linux-stable.git`，checkout `v6.6.y` / `v5.10.y` / `master` 三分支——**这是 kernel.org 官方内核仓，与诊断目标 OLK 内核不同源**。

后果：代码图谱（CodeGraph = OLK）与 commit 图谱（M3 = kernel.org）基于不同内核源，Cross-Graph Linker 焊接时系统性错位：
- 用户 oops 中的 OLK 特有 commit hash → `kernel_commit` 表查不到
- 崩溃发生在 openEuler 原生代码 → 该 commit 在 kernel.org 仓根本不存在
- 公共代码的 backport 在 OLK 与 mainline 行号 / 逻辑可能不同

### OLK commit 的类型（实测数据，2026-05-15）

OLK commit 通过 commit message 顶部的 **inclusion 头**自我标注来源。inclusion 类型是**开放集合**——实测 OLK-6.6 近 8000 个非 merge commit 出现 **40+ 种**：`stable`(5129) / `mainline`(1100) / `hulk`(435) / `driver`(414) / `urma`(107) / `sunway` / `kunpeng` / `kylin` / `hygon` / `loongarch` / `phytium` / ... 但对桥接只需二分：

| 类型 | inclusion 标记 | 上游锚点 | 上游讨论生态 |
|------|---------------|---------|------------|
| **backport 型**（实测 ~78%）| `mainline inclusion` / `stable inclusion` | ✓ | kernel.org（LKML / bug / CVE）|
| **openEuler 生态原生** | 其它任意 `<tag> inclusion`（hulk / driver / urma / 厂商名 …）| ✗ | openEuler 生态 |

**判定规则**：`tag ∈ {mainline, stable}` → backport；否则 → 原生。**不需枚举全部 40+ 种**。

真实 inclusion 头示例（`stable inclusion`，OLK-6.6 commit `99795c4ef16c`）：

```
stable inclusion
from stable-v6.6.124
commit 7c54d3f5ebbc5982daaa004260242dc07ac943ea         ← stable 树 SHA（非 mainline）
category: bugfix
bugzilla: https://atomgit.com/src-openeuler/kernel/issues/13892
CVE: CVE-2026-23261
Reference: https://git.kernel.org/.../stable/linux.git/commit/?id=7c54d3f5...

--------------------------------

[ Upstream commit d1877cc7270302081a315a81a0ee8331f19f95c8 ]   ← 真正的 mainline SHA

<上游原始 commit message 原文，含 Fixes:/Link: trailer>
```

⚠️ **关键**（实测）：
- `mainline inclusion` 的 `commit <sha>` 直接是 mainline SHA；
- `stable inclusion` 的 `commit <sha>` 是 **stable 树 SHA**（不是 mainline）；真正的 mainline SHA 在分隔线下方的 `[ Upstream commit <sha> ]`，**覆盖率仅 ~67%**（1/3 的 stable backport 无此标记）；
- bugzilla 链接域名为 **atomgit.com**（`src-openeuler/kernel` 与 `openeuler/kernel` 两个 repo），非 gitee；
- ~7.6% 的 OLK commit 是 MR merge commit（`See merge request: openeuler/kernel!NNNNN`），无 inclusion 头。

## 决策

| # | 决策 |
|---|------|
| **D1** | commit 主仓库 = **openEuler 内核 git**（`atomgit.com/openeuler/kernel`，分支 `OLK-6.6` / `OLK-5.10`），**已确认与 CodeGraph 索引 `olk-kernel` 同源**（见下文「已确认」）|
| **D2** | 辅仓库 = **kernel.org `linux-stable.git`**（含 mainline 历史 + 全部 stable 分支）。实测：OLK 的 `stable inclusion`（占 backport 大头）锚点指向 stable 树，纯 `torvalds/linux.git` 解析不到 → 必须用 linux-stable.git 才能同时解析 mainline 与 stable SHA |
| **D3** | OLK backport commit 桥接到上游：`mainline inclusion` 取头部 `commit <sha>`；`stable inclusion` 取分隔线下 `[ Upstream commit <sha> ]`（缺失则降级用头部 stable SHA）。openEuler 生态原生 commit 标记「无上游讨论」|
| **D4** | v1.0 **只 ingest kernel.org 讨论 / bug 生态**（LKML / bugzilla.kernel.org / syzbot / NVD，维持 ADR-010/011）；openEuler 生态（kernel@openeuler.org 邮件列表 / atomgit Issues / openEuler-SA）**留 v1.1+** |
| **D5** | 诊断报告对 commit 证据**分级标注**：`mainline-backed`（高）vs `openEuler-原生-仅 commit message`（中）|

### 为什么不全接 openEuler 生态（方案对照）

| 备选 | 拒绝理由 |
|------|---------|
| **B：v1.0 双生态全接** | openEuler 邮件列表（mailman3）/ atomgit Issues 结构化程度远低于 LKML（public-inbox）/ syzbot；v1.0 仓促接入引入**未受控噪声**，损害精确性。且 scope 翻倍稀释核心链路打磨投入。**违背准确性原则** |
| **C：commit 图谱回退 mainline** | 代码图谱（OLK）与 commit 图谱（mainline）不同源，系统性错位；诊断 openEuler 原生 bug 完全失效。**最不准确** |

**准确 ≠ 覆盖全**：准确的系统在无可靠证据时应明确标注边界，而非用低质量数据硬凑。方案 A 对 backport commit（OLK 主体）提供高精确、可追溯链路；对原生 commit 诚实标注「无上游讨论」——两者都"准确"。

## 准确性强化条件（实施约束，不可省略）

选择方案 A 的前提是**严格执行**以下三条，否则 A 同样会不准：

### C1 — inclusion 类型必须严格区分

inclusion 类型是开放集合（40+ 种），M4 OLK commit parser 按 **`tag ∈ {mainline, stable}` ? backport : 原生** 二分判定。**绝不能把 openEuler 原生 commit（hulk/driver/urma/厂商名…）误判为 backport，去链一个错误的 mainline SHA**。误链比不链更糟。inclusion 头缺失 / 格式无法解析时一律按"原生"保守处理 + 标 quarantine。

### C2 — 上游 SHA 必须验证可解析才建链

backport commit 的上游 SHA（mainline inclusion 的 `commit`、stable inclusion 的 `[ Upstream commit ]`）必须在 `linux-stable.git` 辅仓真的 `git rev-parse` 解析到，才建立 `olk_commit → upstream_commit` 边；解析不到则降级为 `unresolved_upstream` 标记，**不硬链**。

### C3 — 代码"真相"以 olk-kernel 为准，mainline 仅供讨论脉络

顺锚点回到 mainline 后，mainline 的代码 / 行号可能与 OLK 不一致（OLK 可能修改过 backport）。诊断报告引用**代码状态 / 行号必须用 CodeGraph 的 `olk-kernel`**，mainline 仅用于还原根因讨论与 LKML/CVE 关联。呼应 [Spike_Report](../spike/Spike_Report.md) F6（行号漂移）。

## 影响

| 模块 | 修订 |
|------|------|
| **M2** | `kernel_commit` 表加 `origin`（`olk`/`mainline`）、`upstream_commit`（指向 mainline SHA，NULL=原生）、`olk_inclusion_type`（**开放 TEXT**：`mainline`/`stable`/`hulk`/`driver`/`urma`/…，NULL=无 inclusion 头）三列；重定义 `affected_versions` 语义 |
| **M3** | §3.6 主仓库改 openEuler 内核 git（OLK-6.6/OLK-5.10）+ mainline 辅仓；commit ingester 加 OLK inclusion 头解析 |
| **M4** | §3.1 short SHA 解析优先级：OLK 仓优先 → mainline 辅仓 → GitHub API；新增 §3.9 OLK inclusion parser + `olk_commit ↔ upstream_commit` linker |
| **M7** | 诊断报告模板加 commit 证据分级标注（D5）|

## 已确认（2026-05-15 核对）

OLK 内核 git 源已与实际环境核对：

- **源** = `https://atomgit.com/openeuler/kernel.git`，分支 `OLK-6.6`（v5.10 对应 `OLK-5.10`）
- **本地 checkout** = `/data1/lingqu/codes/OLK-6.6/kernel`（1,251,433 commits，内核 6.6）
- **同源验证**：6 个函数定义行号（`oom_badness` / `__oom_kill_process` / `oom_kill_process` / `qdisc_tree_reduce_backlog` / `net_tx_action` / `__ext4_fill_super`）与 CodeGraph `olk-kernel` 索引 **6/6 完全一致** → M3 commit ingestion 与 CodeGraph 代码索引**确认同源同版本**
- M3 commit ingestion 主仓库即用此源；映射写入 `clients/codegraph/repos.py` 旁配置

## v1.1+ 演进

- 评估接入 openEuler 生态（kernel@openeuler.org 邮件列表 + atomgit Issues + openEuler-SA），让 openEuler 原生 commit 也能链到讨论
- 触发条件：v1.0 评测中"openEuler 原生 commit 类"案例的诊断质量明显低于 backport 类，且样本占比 > 20%

## 参考

- [ADR-008 复用 codesearch](ADR-008-reuse-codesearch.md) — CodeGraph 索引 olk-kernel
- [ADR-009 多版本通过 repo 隔离](ADR-009-multi-version-via-codesearch-repos.md)
- [M3 Ingestion §3.6](../modules/M3_ingestion.md)
- [M4 Cross-Graph Linker §3.9](../modules/M4_cross_graph_linker.md)
- [Spike_Report F6](../spike/Spike_Report.md)

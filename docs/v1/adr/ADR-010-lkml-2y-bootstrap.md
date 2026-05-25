# ADR-010 — LKML 首次 bootstrap 窗口 = 2024-05 起 2 年

> **⚠️ 已被取代（2026-05-21）**：本 ADR 的"LKML bulk 摄入"决策被 [v2 ADR-025](../../v2/adr/ADR-025-knowledge-data-online-default.md) 取代。实测表明 bulk 按 list+日期窗口摄入对 `link_commit_message` 几乎无效（85% 引用超窗）且耗时 34.5h。v2 改为 LKML 三层模型（定向抓取 + 懒加载缓存 + lore live search）。

| 字段 | 值 |
|------|---|
| 状态 | **Superseded by [ADR-025](../../v2/adr/ADR-025-knowledge-data-online-default.md)** |
| 日期 | 2026-05-14（2026-05-21 标记取代）|
| 决策者 | 用户 + Architect |
| 关联模块 | M3 |
| 相关 ADR | [ADR-007](ADR-007-data-dir-in-repo.md) · [ADR-008](ADR-008-reuse-codesearch.md) · [v2/ADR-025](../../v2/adr/ADR-025-knowledge-data-online-default.md) |

## 上下文

M3 Ingestion 层需要决定 LKML（lore.kernel.org）的首次抓取窗口。LKML 全量数据约 25 年（1998-至今），千万级邮件量，全量抓取需数周以上，且存储大。需要选择起点：

| 选项 | 邮件量 | 存储（gzip mbox）| 首次 bootstrap 估时 | 覆盖力 |
|------|--------|------------------|-------------------|--------|
| A | 5 年（2020-12 至今）| ~80 GB | 1-2 周 | 完整覆盖 v5.10 LTS 整个生命周期 |
| B | **2 年（2024-05 至今）** | **~35 GB** | **3-5 天** | 覆盖 v6.6 LTS 主要讨论 |
| C | 10 年 | ~160 GB | ~3 周 | 含许多老旧 incident，但价值递减 |

## 决策

**选择 B：LKML 首次 bootstrap 窗口 = 2024-05 起约 2 年**。

理由：
- 2024-05 在 v6.6 LTS 发布（2023-10）之后 6 个月，覆盖该 LTS 的活跃维护期与 stable 派发期
- v5.10 LTS 已进入维护后期，新 patch 主要走 stable backport（仍能在该窗口内捕获）
- 3-5 天首次 bootstrap 是工程上可接受的"开机时间"
- 35GB mbox 存储舒适
- 不足部分可后续按需回补（[v1.1 演进路径](#)）

## 影响

| 维度 | 影响 |
|------|------|
| 首次启动时间 | 3-5 天（vs 1-2 周 / 3 周）|
| 磁盘 | ~35 GB（vs 80 / 160 GB）|
| Pro 订阅 LLM 摘要消耗 | 减少（长线程数随时间窗收缩）|
| 历史 incident 覆盖率 | 失去 2020-2024 早期 v5.10 历史；如 30 例评测样本含 2024 前的 incident，需评估补充策略 |
| v1.0 验收 30 例 | 优先选 2024-05 后已有完整讨论的样本 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| A 5 年（2020-12 起，原计划）| 首次 bootstrap 太久，v1.0 启动门槛太高；早期 2020-2023 历史在 v1.0 的 30 例评测中**未必**用到 |
| C 10 年 | 远超必要；老旧讨论价值递减；存储与时间不划算 |

## 后续

如果 v1.0 评测发现：
- 大量 30 例样本指向 2024 前的 incident
- 或用户提问明显引用 2020-2023 早期 LKML 讨论

则在 v1.1 启动"分段回补 LKML"任务：
1. 改 `lkml_ingester.bootstrap_window` 配置到 5 年
2. 跑一次"窗口扩展"特别任务（仅抓 2020-12 → 2024-05 段）
3. 摘要任务后台异步补做

此 ADR 不锁死 2 年上限，仅锁定 v1.0 起步窗口。

## 监控指标（v1.0）

- 30 例评测样本中"日期 < 2024-05"的比例 → 决定是否触发 v1.1 回补
- 用户查询命中 `lkml_message` 但返回为空（按月分布）→ 暴露窗口不足的征兆

## 参考

- lore.kernel.org 协议：https://lore.kernel.org/_/text/help/
- v6.6 LTS release：2023-10
- v5.10 LTS release：2020-12

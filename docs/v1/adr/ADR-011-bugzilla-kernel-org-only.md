# ADR-011 — Bugzilla 范围 = 仅 bugzilla.kernel.org（v1）

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M3 |
| 相关 ADR | [ADR-010](ADR-010-lkml-2y-bootstrap.md) |

## 上下文

M3 Ingestion 层需要决定 Bugzilla 接入范围。候选：

| 选项 | 来源 | 规模 | API key | CVE 关联 | backport 信息 |
|------|------|------|---------|--------|--------------|
| A | **仅 bugzilla.kernel.org** | ~50K bugs | 无需 | 弱（仅依赖 see_also）| 无 |
| B | kernel.org + bugzilla.redhat.com | ~50K + ~200K | 需 RHBZ_API_KEY | ★★★ 强 | ★ |
| C | A/B + Debian BTS + Launchpad | + ~100K | 多种凭据 | 中 | ★ |

## 决策

**v1 仅接入 bugzilla.kernel.org**。Red Hat BZ 与 Debian/Ubuntu 数据源延后到 v1.1+ 评估。

理由：
- v1 用户未提供 RHBZ API key，规避凭据管理
- 简化 ingestion 复杂度：少一个外部依赖，少一套 schema 归一化
- 50K bugs 是合理的起点规模
- CVE 关联损失可通过**NVD 直接抓取**部分补回（NVD 引用 commit + 影响 product）

## 影响

| 维度 | 影响 |
|------|------|
| Ingestion 复杂度 | 显著降低（无 RHBZ schema 差异 + 凭据管理）|
| Bug 总量 | 50K（vs 250K）|
| **CVE-bug 关联覆盖率** | 显著降低（RHBZ 是 CVE-kernel 关联最完整的源）|
| Backport 状态可见性 | 失去（RHBZ 标注 RHEL backport 状态）|
| 企业级 incident 覆盖 | 减弱（RHEL 用户的真实生产 incident 多在 RHBZ）|
| 评测样本来源 | 30 例评测仍可主要从 syzbot + LKML 取，bug 报告仅作补充 |

## 缓解：用 NVD 补 CVE-kernel 关联

NVD CVE feed 含 `references` 字段，常引用：
- commit hash（如 `https://git.kernel.org/.../commit/abc1234`）
- Bugzilla URL（含 bugzilla.kernel.org 和 bugzilla.redhat.com）
- LKML 邮件 URL

我们仍能通过 NVD ingester 抽取 commit hash → 关联到 `kernel_commit.fix_commits`，从而把 CVE → kernel commit 关联建好（虽然没有 RHBZ 的 bug 维度）。

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| B kernel.org + Red Hat | 用户当前无 RHBZ API key；引入凭据 + schema 复杂度；v1 优先简洁 |
| C 全部发行版 | 多套凭据 + 多套 schema；v2 再说 |

## v1.1+ 回顾触发条件

满足以下任一条件，v1.1 启动 "Red Hat BZ 接入" 子任务：

1. 30 例评测中 CVE 关联召回率 < 60%（说明 NVD 补强不够）
2. 用户报告"想看 RHEL 真实生产 incident 但 agent 不知道"
3. 团队获取到 RHBZ API key 且愿意承担凭据管理

接入工作量：~300 行 Python（参考已有 kernel.org ingester 复用 80%）+ schema 归一化映射表扩展。

## 监控指标（v1.0）

- `bug` 表中 source='bugzilla.kernel.org' 行数（增长趋势）
- 30 例评测命中 `bug` 表的比例（衡量当前覆盖是否足够）
- CVE-bug 关联条数（衡量 NVD 补强效果）

## 参考

- bugzilla.kernel.org REST API：https://bugzilla.kernel.org/docs/en/html/api/index.html
- bugzilla.redhat.com REST API（备查）：https://bugzilla.redhat.com/docs/en/html/api/index.html
- NVD JSON Feed v2.0：https://nvd.nist.gov/developers/vulnerabilities

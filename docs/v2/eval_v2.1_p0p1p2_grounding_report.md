# v2.1 P0/P1/P2 Evaluation Report — Grounding Truth Revealed

| 字段 | 值 |
|---|---|
| 跑次日期 | 2026-05-27 |
| 数据集 | `eval/data/cases_v2.json` 15 例 |
| 模型 | openrouter `deepseek/deepseek-v4-flash`（付费 tier） |
| 配置 | `concurrency=3`, `TOKEN_BUDGET=200K` |
| Wall time | **44.6 min** |
| ERROR | **0 / 15** ✓ |

---

## 1. 本轮变更（v2.1 P0/P1/P2）

5 项 fact-grounding 修复，回应用户反馈"要事实依据，不凑测试结果"：

| Patch | 内容 |
|---|---|
| **P0-1** `bind_claims` 硬闸门 | unverified claims 不再 rubber-stamp；状态分 `verified_claims` / `speculative_claims`；新增 `groundedness ∈ {grounded, speculative, n/a}` |
| **P0-2** claim ↔ commit body 语义重叠检查 | `verified = hash_exists AND keyword_overlap ≥ 0.20`；带 `evidence_strength` 字段 |
| **P1-1** Grounding metrics in `runner_v2` | `grounded_rate / speculation_rate / abstention_rate / grounded_correct_rate / avg_verified_claim_rate / judge_uncertain_rate` |
| **P1-2** Dual LLM judge | Judge A 肯定 framing + Judge B 反证 framing；不一致标 `judge_uncertain` 排除出分母 |
| **P2** Evidence trace requirement in prompt | `<final_answer>` 必须含 `### Evidence trace` 列具体 ID 链；做不到强制 `<insufficient_evidence>` |

报告渲染层同步改：报告头加 `Diagnosis quality: ✓ GROUNDED / ⚠ SPECULATIVE` banner；声明分两段呈现。

---

## 2. 头条数字（与前两轮对照）

| 指标 | Run 5<br>(P0+P1+P2 parser) | Run 6<br>(v2.1 P1a/b/c) | **Run 7**<br>**(v2.1 P0/P1/P2 grounding)** |
|---|---|---|---|
| Wall time | 60.5 min | 63.0 min | **44.6 min** |
| ERROR | 0 | 0 | **0** ✓ |
| diagnosed | 10 | 14 | 11 |
| budget_exhausted | 3 | 0 | 1 |
| max_iter_reached | 2 | 0 | 2 |
| insufficient_evidence | 0 | 1 | 1 |
| Route accuracy | 100% | 100% | **100%** ✓ |
| Recall@10 | 53.3% | 53.3% | 53.3% (BM25 上限) |
| Root-cause acc (judge) | 60% (6/10) | 53.8% (7/13) | **37.5% (3/8)** |
| **Grounded rate (NEW)** | — | — | **0%** ⚠⚠⚠ |
| **Speculation rate (NEW)** | — | — | **100%** |
| **Abstention rate (NEW)** | — | — | 6.7% (1/15) |
| **Judge uncertain (NEW)** | — | — | 27.3% (3/11) |

---

## 3. 核心发现：100% speculation 不是 bug，是 honest reporting

P0-1/P0-2 修复**第一次让系统诚实**报告自己的接地能力。

### 验证 overlap 算法是否准确

跑了两个对照场景：

| 场景 | claim text | evidence body | overlap | 期望 |
|---|---|---|---|---|
| 真无关 | "Initramfs missing dm-mod for LVM mount" | "block: introduce blkdev_get_by_dev() helper" | **0%** | 拒绝 ✓ |
| 词汇有差异、同语义 | "Memory pressure in cgroup caused OOM kill" | "mm: memcontrol fix overcommit accounting trigger premature OOM kill cgroup" | **40%** | 通过 ✓ |

阈值 0.20 校准合理。**100% speculation 不是阈值太严**。

### 为什么仍然 100% speculation？

#### 情形 1：ground truth 是配置/硬件，根本没 commit 可引用
- panic-001：缺 initramfs 模块（系统配置）
- hardware-001 / hardware-002：硬件故障（缺 DIMM / MCE）
- io-hang-001：存储 timeout
- 这类 case agent 仍要写 `<final_answer>`，从 evidence 池里**拣一些 commit 作装饰**，跟 claim 文本无关 → speculative ✓

#### 情形 2：BM25 召回不到正确 commit
- OOM 类长期 recall 0%（按类别表）—— evidence 池里**根本不含** ground truth commit
- agent 引用什么是什么，跟它写的 claim 没语义交集 → speculative ✓

### 一个具体的好例子：panic-001

| 维度 | 状态 |
|---|---|
| Dual judge 一致 | ✓（"agent 答 initramfs 缺 dm 模块，匹配 GT"）|
| Groundedness | speculative |
| Claims | 7 个，全 speculative |
| ground_truth_commit_hashes | 空（这是配置故障）|

**判断是对的，但没 commit 可指**。系统诚实说"我答对了，但我引用的 commit 跟答案没语义关系"。

---

## 4. Dual judge 也工作正常

| 维度 | Run 6 单 judge | Run 7 双 judge |
|---|---|---|
| 看似 root-cause acc | 53.8% (7/13) | **37.5% (3/8)** |
| 判断不一致 | 隐藏 | **27.3% (3/11)** 显式 |

3 case 双 judge 分歧 → 这些在 Run 6 被单 judge 随机判定。Run 7 明确标 "uncertain"，不入分母 → 更可靠的 accuracy 下限。

---

## 5. 逐 case 状态

| Case | 类别 | Verdict | Judge | Grounded | 备注 |
|---|---|---|---|---|---|
| oom-001 | oom | diagnosed | ✗ | spec | recall 0%，agent 凭印象答 |
| oom-002 | oom | diagnosed | - (uncertain) | spec | dual judges 分歧 |
| oom-003 | oom | diagnosed | ✗ | spec | 类似 oom-001 |
| softlockup-001 | lockup | **insufficient_evidence** | - | n/a | **主动认怂，正确** |
| softlockup-002 | lockup | diagnosed | ✗ | spec | |
| kasan-001 | oops | max_iter_reached | - | n/a | 不收敛 |
| kasan-002 | oops | diagnosed | ✓ | spec | judge 对但 spec |
| panic-001 | panic | diagnosed | ✓ | spec | 配置故障无 commit |
| panic-002 | panic | diagnosed | ✓ | spec | |
| hardware-001 | hardware | diagnosed | ✗ | spec | |
| hardware-002 | hardware | max_iter_reached | - | n/a | |
| rcu-001 | lockup | diagnosed | - (uncertain) | spec | |
| lockdep-001 | oops | diagnosed | ✗ | spec | 3 iter 早结束（可疑）|
| change-001 | change | diagnosed | ✗ | spec | 2 iter 早结束（可疑）|
| io-hang-001 | io_hang | diagnosed | - (uncertain) | spec | |

### 引起注意的两个早终止

`lockdep-001`（3 iter，26K tokens）和 `change-001`（2 iter，9.9K tokens）—— 远低于平均 ~12 iter。可能是 P2 evidence trace 要求触发了"做不到 trace → insufficient" 路径但 verdict 没正确反映。需要看 react_tool_trace 确认。

---

## 6. 真正的 v2.0 五项验证项达成度

| 验证项 | 目标 | Run 7 实测 | 状态 |
|---|---|---|---|
| Route accuracy | ≥ 90% | 100% | ✓ |
| Root-cause correctness（旧定义）| ≥ 60% | 37.5% (3/8) | ✗ |
| **Grounded+Correct rate（新严格定义）** | ≥ 30% | **n/a (0% grounded)** | ✗ |
| 系统稳定性 (ERROR rate) | ≤ 10% | 0% | ✓ |
| Wall time 全量 eval | ≤ 60 min | 44.6 min | ✓ |

之前 v2.0 final 四项达标的认知，**在严格 metric 下 root-cause 项 fail**。

---

## 7. 这告诉我们什么

> **核心**：100% speculation rate **不是 metric 太严**，是 BM25 召回失败的影子 + 配置/硬件类故障**根本没 commit 可引用**两件事被诚实暴露。

之前 Run 4-6 的 "75% / 60% / 53% root-cause acc" 都是**幻觉**——单 judge lenient + 表面 verified 拼出来的好看数字。Run 7 给出真实地基：

- 真正稳的 case：**0 个**（grounded + judge ✓ = 空集）
- 主动 abstain 算成功：1 case（softlockup-001）✓
- 判断诚实分歧：3 case（27%）暴露 single-judge 噪声

---

## 8. v2.2 决策（必做）

P0：**修复 BM25 召回是 unblocking 一切 grounding 的前提**。

两条路：

### 路径 A — Tantivy / Lucene BM25Okapi 替换 PG `ts_rank_cd`
- 自建 BM25 服务（Rust Tantivy 或 Java Lucene + HTTP wrapper）
- ts_rank_cd 是 coverage-bias，对内核短 commit 排序不佳；BM25Okapi 在内核语料表现更好（行业验证）
- **工作量：1-2 周**（迁移 commit / lkml / bug / cve / syzbot 5 张表）
- **预期增益**：oom 类 recall 0% → 30%+，整体 recall 53% → 70%+
- **风险**：新依赖、容器化、运维一份

### 路径 B — AND-priority recall + LLM rerank top-100
- 先 AND-match（要求所有关键词都命中），0 hit 才 OR 降级
- candidate pool 10 → 100 给 LLM rerank
- **工作量：2 天**（修 retrieval/recall/*.py）
- **预期增益**：oom 类 recall 0% → 15-25%；reranker 帮挽回部分
- **风险**：rerank 成本上升 ~6× LLM token；AND-match 在 query keyword 模糊时召回 0

### 推荐
**先做 B（2 天），看是否够 grounded_correct_rate > 30%。不够再上 A**。

理由：
- B 复用现有 PG + reranker，工程风险低
- A 是架构改动，应在 B 失败后才上
- 评估"够不够"的 metric 就是 grounded_correct_rate —— 这是 v2.2 的真正 KPI

---

## 9. 也要做的（次级）

| | |
|---|---|
| 调查 lockdep-001 / change-001 2-3 iter 早终止 | P2 prompt 可能太严，agent 跳过 final_answer 但 verdict 没正确反映 |
| 修 atomgit gap 59 → ~3（API 上确实可访问的） | 补 backfill |
| evaluation 集扩到 30+ case | 当前 15 例统计意义弱，尤其分类后 n=1-3 |
| docs/v2/ProjectStatus.md §8.2 重排：grounded_correct_rate 升为 KPI | metric 中心化 |

---

*生成时间：2026-05-27 · 数据来源：`eval/results_v2/summary.json` + 单 case JSON*

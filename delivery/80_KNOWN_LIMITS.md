# 80 · Known Limitations

> 本系统**还没做** + **故意不做** + **故意延后** 的事。重建者要决定是接受还是补齐。

---

## 1. 未做 — 中优先级（建议先补）

| 项 | 影响 | 工时估计 |
|---|---|---|
| **nvme/scsi/SAN timeout 自动触发 hardware route** | io_hang 类故障当前走 kernel route，本应走 hardware（ADR-023 模式扩展） | 1 PD |
| **subsystem 自动分类**（从 commit 改的 files 路径推断） | 当前 `kernel_commit.subsystem` 列填充率 ~80 %；剩余 NULL 用 LLM rerank 时不便过滤 | 0.5 PD |
| **增量 issue 更新机制**（gitee / atomgit） | 当前 ADR-022 只做一次性全量 backfill；新 issue 进不来；周度跑 stale | 1 PD |
| **eval 数据集扩展 22 → 30+ 例** | 当前每类 fault_kind n=1-3，统计意义弱 | 0.5 PD |

## 2. 未做 — 长期 P3 / 已搁置

| 项 | 原因 |
|---|---|
| **mainline + linux-stable git 入库**（ADR-018 Phase B 完整 ingest） | 60K 悬空 upstream SHA。stub commit 已被 M5 过滤所以影响小；真正补全需要 1-2 天 ingest + 索引时间 |
| **Tantivy / Lucene 替换 PG `ts_rank_cd`** | recall@10 已降级为副指标（不再是验收门槛）；不必上独立 search server |
| **drgn 容器化**（M9 T-018） | vmcore 路径暂未启用；产品 demo 不需要 |
| **Prometheus / Elasticsearch 集成**（M17） | 当前用 PG `ingest_runs` + JSONL 日志，单机够用；多实例部署再考虑 |
| **`agent_tool_trace` 表迁移设计** | trace 当前存 state + eval JSON，未持久化到独立表；未做查询型 trace 分析 |
| **CI 自动门禁**（GitHub Actions / Gitea Actions） | 本仓库未配；测试可手动跑 `pytest tests/`，但无 PR-level 自动验证 |

## 3. 故意**不做** — 明确决策

| 不做 | 决定来源 |
|---|---|
| **跨 distro**（Ubuntu / RHEL / Android / Debian） | `90_FAQ.md` Q9 论证：跨 distro 后 70 %+ 数据重复，运维成本 10×，效果增益 < 10%；OLK 单 distro 是产品 scope |
| **Semantic embedding（vector search）** | ADR-001 拒绝；内核领域 BM25 + LLM rerank 表现优于 vector |
| **Cross-encoder reranker（如 BGE / ColBERT）** | ADR-002 拒绝；LLM rerank 直接当 reranker |
| **Live SSH 到生产机执行命令** | 安全 + 复杂；改用 sosreport 归档 + dmesg 文本输入 |
| **Live vmcore 集成测试** | 只在 lab env 跑过 drgn；CI 不含 |
| **SRE 完整 incident response 流程**（severity tiering / mitigation playbook / postmortem 自动化） | 我们是**诊断助手**，不是 incident manager。报告里给配置/backport 建议，**不**自动 mitigate |
| **Red Hat Bugzilla 接入** | ADR-011 v1 scope；deferred 到 v1.1+，目前用 gitee/atomgit 补 |
| **Self-consistency K=3 默认** | 评测时设 K=3 看一致性，生产 K=1 省 token |

---

## 4. **已知但可接受**的产品行为

### 4.1 LLM 答案非确定性

同一 dmesg + 同一 case，连续两次 `diagnose()` 可能：
- 不同 verdict (`diagnosed` ↔ `insufficient_evidence` 漂移)
- 不同候选 commit 排序
- 不同 confidence 等级

**为什么可接受**：M8 process compliance 主 KPI 是**确定性**（agent 走了哪些 KG 路径，重跑 trace 同分），答案文本的漂移是 LLM 本质特性，我们**显式接受**（不靠 prompt 强行收敛）。

### 4.2 `recall@10` 长期 ~10-20 %

`expected_commit_hashes` 与 evidence pool top-10 的 short-SHA 交集；很多 case "agent 找到了一个**等价**的 commit 但不是 ground-truth 列的那个"。

**为什么可接受**：见 ADR / KPI 设计；recall 是副指标，process compliance 才是验收门槛。

### 4.3 OOM-by-design 类问题被诊断为 Form C

像 cgroup memory.max 设得太低引起的 OOM，agent 会输出 "Form C — no commit fix applies; tune memory.max"。这是**对的**：内核机制按设计工作，不是 bug。

### 4.4 `link_commit_bug` 历史上 = 0（已修）

OLK commits 引用的 issue 主要在 gitee（63K）/ atomgit（7.9K），ADR-022 之前只 ingest bugzilla.kernel.org（~1.6K），所以 link_commit_bug 永远 0。ADR-022 完成后 link 数 = 58K。

### 4.5 `force_finalize` 时 DeepSeek 偶尔幻觉 DSML token

详见 `60_GOTCHAS.md` §4.1。现有 hard-reset retry 机制覆盖大多数 case；个别仍会漏。

---

## 5. **会出错**的输入边界

| 输入 | 行为 | 建议 |
|---|---|---|
| dmesg 含 ISO-style `Jun 02 14:32:01 host kernel:` 前缀（journalctl -k 输出） | `_TIMESTAMP_RE` 不剥；后续正则 0 命中 → 走 LLM 分类 fallback | 文档提示用户先 `dmesg` 命令而非 `journalctl -k` |
| 输入 > 30000 字符 | `user_prompt` 截断到 3000 字符；agent 看不到尾部 | 提示用户精简到关键段 |
| 中文 dmesg / 自由文本中**英文术语过于稀少** | `_LANG_DIRECTIVE` 触发但 keyword extraction 可能漏（query_parser 倾向英文术语） | LLM 一般能补上，但 recall 降低 |
| sosreport 含绝对路径 member | `tarfile` 默认拒绝，但 list_members 仍报；normalize 时记得 `lstrip('/')` | 见 M6 § 6 #1 |
| 用户问"对比 OLK-6.6 vs OLK-5.10 处理 X 的方式" | 不支持跨版本对比；agent 会就单版本给答案 | 未来功能 |

---

## 6. 安全边界

| 安全项 | 状态 |
|---|---|
| **HTTPS / TLS** | 当前 web UI 监听 `127.0.0.1:8000`；公网部署需反代 + Let's Encrypt |
| **认证 / 授权** | **无** — `/api/diagnose-stream` 公开，任何能访问 8000 端口的人都能 query。多人共享必须补 OAuth/SSO 等 |
| **速率限制 per-user** | **无** — 只有 vendor 级 RPM 限流。多人共享要补 IP / user-token 级 |
| **API key 存储** | `configs/local.yaml`（gitignored）。生产建议用 Vault / k8s secret |
| **PII / 敏感数据** | dmesg 偶尔含 hostname / 内核地址；当前不做 redaction。**不要把含敏感数据的诊断结果共享出去** |
| **SQL injection** | 用 SQLAlchemy + 参数化绑定；无字符串拼接 SQL。**新加查询请用 `text(":name")`** 不要 f-string SQL |
| **XSS** | web UI 所有 LLM 输出走 `escapeHtml`。**新加渲染请保持 escape** |
| **Subprocess command injection** | `kernel_commit` ingester 调 git；参数走 `subprocess.run([list])` 不走 `shell=True`。**保持** |

---

## 7. 数据完整性边界

### 7.1 数据快照漂移

KG 周度增量 ingest。新故障如果在最近 7 天内的 commit 修复，可能尚未入库。

### 7.2 stack_signature 算法变更

算法在 `kg/signature.py`。若改算法（如扩 `_INTERNAL_FRAMES`），必须重签 4 张表（syzbot_crash / bug / lkml_message / dmesg_event）。脚本：`scripts/resign_signatures.py`。**忘了重签 = `find_similar_crashes` 在新算法下错配**。

### 7.3 link_commit_symbol 假阴

符号抽取规则有保守过滤（拒绝短串 / 通用 keyword）。某些"真符号"如 `do_fork` 可能被拒。

---

> 重建版本要不要补 §1 / §6，由客户决定。本交付以**单机 dev / 内网共享**为默认场景。

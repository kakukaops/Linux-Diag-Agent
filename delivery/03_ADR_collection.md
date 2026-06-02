# 03 · ADR Collection（25 条架构决策记录）

> ADR (Architecture Decision Record) 是一次性决策的**"WHY"档案**。本表把 25 条决策合并为速查 + 完整内容。
>
> 重建者**必须读 § 2 速查表**。完整内容（在原仓库 `docs/v1/adr/` 和 `docs/v2/adr/`）只在你想"做相反选择"时回看——因为 WHY 写得很详细，反对的话至少要驳得过原 ADR。

## 1. 状态约定

- **accepted** — 当前生效
- **superseded by ADR-XXX** — 被后续 ADR 替代
- **deprecated** — 实际未执行 / 已撤销

## 2. 速查表

| # | 标题 | 状态 | 一句话决策 |
|---|---|---|---|
| 001 | 不使用 Embedding 检索 | accepted | 内核领域术语稀疏，BM25 + LLM rerank 比 vector embedding 召回准确率高且运维便宜 |
| 002 | 不使用 Cross-Encoder Reranker | accepted | rerank 直接用 LLM；不另起 reranker 服务（避免运维 + 模型生命周期错位） |
| 003 | LLM 统一 schema = OpenAI Chat Completions | accepted | 3 backend 都翻译到这套消息结构 + tools / tool_calls |
| 004 | MVP 默认 backend = claude_code | accepted | Pro 订阅免 API 计费；后续可切 openai_compat |
| 005 | 双版本 = 单库 + kernel_version 列 | **SUPERSEDED by ADR-009** | 原计划用 kernel_version 列区分 OLK-6.6 / 5.10；后改 CodeGraph 双 repo |
| 006 | BM25 = PG `tsvector` + GIN | accepted | 简单 + 运维少；够用到 100M 行规模 |
| 007 | 数据存储目录 = `<repo>/data/`（可配置） | accepted | dev/prod 都在 repo 内；gitignore 排除大文件 |
| 008 | Code Graph + Doc Tree = 复用 codesearch | accepted | 不自建 Zoekt + SCIP 索引服务 |
| 009 | 多版本通过 CodeGraph 双 repo 隔离 | accepted | OLK-6.6 / OLK-5.10 在 CodeGraph 注册为独立 repo |
| 010 | LKML 首次 bootstrap = 2024-05 起 2 年 | accepted | 平衡覆盖 vs 入库时间（实际 6 个月 ≈ 100K 邮件） |
| 011 | Bugzilla 范围 = 仅 bugzilla.kernel.org | accepted（v1） | Red Hat BZ deferred；gitee/atomgit 通过 ADR-022 补 |
| 012 | CodeGraph MCP = HTTP 传输 | accepted | 不用 stdio（多 client 共享更易） |
| 013 | M5 检索 = LLM query parse + always-fire-all 7 路 | accepted | 选择性路由历史上总漏对的源 |
| 014 | M6 主机工具 v1.0 范围 = 仅文件上传 + 2 工具 + 无 vmcore | accepted | 后期可扩；vmcore 走 drgn (ADR-020) |
| 015 | M7 = LangGraph + Reject-Regenerate + 3 SOP | accepted v1；**M22 spike 后 ReAct 取代 Reject-Regenerate**（ADR-019） |
| 016 | M8 v1.0 = 文件日志 + 一次性评测 | accepted v1 |
| 017 | codesearch 服务统一称 "CodeGraph" | accepted | 项目内全部用 CodeGraph 这个名字 |
| 018 | Commit 来源 = OLK 主 + mainline 辅 | accepted | 详见 § 3.18 |
| 019 | Hybrid 模式 = 确定性 Triage + ReAct Investigation + 确定性 Report | accepted | M22 spike 后定型 |
| 020 | vmcore 用 drgn 不用 crash | accepted | drgn Python API 友好，crash 只能交互 |
| 021 | 观测性通过 Adapter 接入 | accepted | 不直接读 `/proc/`，便于 mock + 测试 |
| 022 | gitee / atomgit issue ingester 补齐 link_commit_bug | accepted | OLK commit 引用的 issue 主要在 gitee（63K）/ atomgit（7.9K） |
| 023 | Hardware-first SOP 路由 | accepted | dmesg 有 Hardware Error / MCE / EDAC 信号 → 优先 hardware 路由，避免误诊为内核 bug |
| 024 | "无法诊断"出口路径 | accepted | 证据不足时输出 `<insufficient_evidence>` 列出缺什么，而非强行编造 |
| 025 | 知识数据 = offline-first → online-default | accepted | KG silent 时 fallback 到 first-principles + LLM intrinsic knowledge，但**显式标 low confidence** |

## 3. 关键 ADR 详解（不可省）

> 完整 ADR 文本在原仓库 `docs/v1/adr/ADR-NNN-*.md` 和 `docs/v2/adr/`。这里把"重建时必须理解"的 6 条核心 ADR 摘要再写一遍——其它 19 条简略表已够用。

### 3.1 ADR-001 · 不用 Embedding 检索

**背景**：所有"AI 项目"惯性想用 vector embedding 做语义检索。

**决定**：内核领域**全 BM25 + 图谱 + LLM rerank**，不用 embedding。

**WHY**：
1. **领域术语稀疏**：内核 commit / 邮件里 `mem_cgroup_out_of_memory` / `CONFIG_MEMCG` 这类术语，BM25 完美命中；通用 embedding 模型可能把 "mem_cgroup" 和 "memory cgroup" 编码到相近但**不完全一致**的向量，反而失分。
2. **可解释性**：BM25 命中能说"因为 query 里有 'try_charge_memcg' 这个 token"；vector top-K 只能说"相似度 0.83"，工程师不信任。
3. **运维成本**：embedding 要嵌入维度选择 + 模型版本管理 + 重新嵌入旧数据；BM25 + GIN 索引零额外组件。
4. **rerank 由 LLM 兜底**：BM25 召回 50 条后让 LLM rerank top-10，等价于"LLM 做了精排"，比 cross-encoder reranker 灵活。

**反对此决定需要的证据**：在 22 case 上 embedding+rerank 能比 BM25+LLM rerank **recall@10 高 ≥ 15 个百分点**——否则不值得引入新组件。

### 3.2 ADR-013 · M5 = always-fire-all 7 路

**背景**：直觉是"按 fault_kind 选路由——OOM 类只查 commit 和 LKML，不查 syzbot 和 CVE"。

**决定**：**所有 7 路无条件并发**触发。

**WHY**：
1. **历史教训**：选择性路由曾把 oops 类导到只查 commit，但真正的 fix 在 CVE 数据库里。每加一个 fault_kind 都要更新路由表，错配率高。
2. **成本可控**：7 路并行总耗时 ≈ max(单路) ≈ 500 ms - 1.5 s。
3. **召回胜过精排**：先广召回再 LLM rerank，比先选路再 BM25 的总召回率更高。

### 3.3 ADR-018 · Commit 来源 = OLK 主 + mainline 辅

**背景**：本来要直接用 `kernel.org/torvalds/linux.git`。

**决定**：**主 commit 源 = `atomgit.com/openeuler/kernel.git`**（OLK 仓），辅以 linux-stable 填补 backport 链。

**WHY**：
1. **OLK 是产品 scope**：我们诊断的是 openEuler 内核，不是 mainline。OLK 含 mainline + openEuler 厂商补丁。
2. **mainline 行号会不一致**：mainline 和 OLK 同一行内核代码可能在不同行号上；用 mainline commit 查 OLK 是错位的。
3. **upstream 链通过 `upstream_commit` 字段重建**：OLK backport commit 的 body 里都有 `upstream-commit: <sha>` trailer，linker 用这字段建桥。

### 3.4 ADR-019 · Hybrid Pipeline = 确定性 Triage + ReAct Investigation + 确定性 Report

**背景**：M22 spike 发现 LangChain `create_react_agent` 需要 LangChain `BaseChatModel`，会绑定 LangChain LLM 抽象——丢失 M1 的 cache + rate limit guard。

**决定**：
- Triage / Retrieve / Bind / Report 用 **确定性 LangGraph 节点**
- 中间 Investigation 用 **自实现 ReAct loop**（直接调 M1 ChatRequest）

**WHY**：
1. Triage 确定性 → 同一 dmesg 永远走同一 route，可测
2. ReAct Investigation 非确定性 → 让 LLM 自主决定查什么，灵活
3. Report 确定性 → 同一 final_answer 渲染 markdown 永远一样
4. **`create_react_agent` 不灵活**：要绑 LangChain LLM，丢 PG cache + 限流；M22_spike_findings.md 详记

### 3.5 ADR-023 · Hardware-first SOP 路由

**背景**：早期 v1 不论 dmesg 含什么都走 kernel 路由，导致 hung_task on blk_mq_get_tag（IO 后端挂死） 被诊断成内核死锁。

**决定**：triage 阶段检测以下信号 → 强制 `route=hardware`：
- `Hardware Error` 字面值
- `Machine Check Exception` / `^mce:`
- `EDAC.*Uncorrected|UE`
- taint 字母含 `M`

**WHY**：硬件错误的诊断逻辑（看 BMC、DIMM 替换）完全不同于内核 bug；走错路径后查 commit 库永远找不到答案。

### 3.6 ADR-025 · 知识数据 online-default

**背景**：v1 设计"完全 offline，KG 是唯一真理"；实际 22 case 验证发现 ~20 % 问题 KG 没有任何相关条目（design-by-design / 配置问题 / 罕见硬件）。

**决定**：保持 KG-first，但 KG silent 时 **fallback 到 LLM intrinsic knowledge**——前提是**显式标 low confidence**。

**WHY**：
1. KG 不全是事实；预期它会有 silent zones（特别是 by-design / 配置类问题）
2. 用户不需要 100% 的 KG 命中——需要"如果 KG 没答案，告诉我你的训练知识猜的，并明确标注"
3. 比 `<insufficient_evidence>` 一刀切更有用——`<insufficient_evidence>` 留给"需要 vmcore / 全 dmesg" 这种真缺数据的场景

## 4. 历史决策遗迹（已 SUPERSEDED）

| ADR | 原决定 | 为什么改 |
|---|---|---|
| 005 | 用 `kernel_version` 列区分 OLK 版本 | CodeGraph 用双 repo 隔离（ADR-009）更干净 |
| 015 part | M7 用 Reject-Regenerate | M22 spike 后改 ReAct loop（ADR-019） |

## 5. 重要"不做"的决策（未列入正式 ADR 但应记）

| 不做 | 原因 |
|---|---|
| 跨 distro（Ubuntu / RHEL / Android） | FAQ Q9 论证：跨 distro 后 70%+ 数据重复，运维 10×，效果增益 < 10% |
| 自建 BM25 / vector 服务 | PG 够用；引入 Tantivy / Elasticsearch 增加运维负担 |
| Live SSH 到生产机执行命令 | 安全 + 复杂；用 sosreport 归档替代 |
| Prometheus / ES 集成（M17）| v1 不做；v2 可选 |
| 完整 SRE incident response 流程（severity / mitigation / postmortem） | 我们是**诊断助手**，不是 incident manager |
| LLM judge 单 judge | 误判率 25%+；改 dual judge with uncertain region |

---

> AI agent 重建时，**遇到"为什么不这样做"的疑问优先查本表**。如果你的设计和某条 ADR 冲突，要么改回 ADR 的决定，要么有具体证据证明 ADR 假设已不成立。

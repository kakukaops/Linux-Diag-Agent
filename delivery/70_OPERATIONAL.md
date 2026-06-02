# 70 · Operational

> 部署 / 配置 / 运行所需的全部环境信息。重建后照此搭起来。

---

## 1. 运行时依赖

### 系统

| 组件 | 版本 | 备注 |
|---|---|---|
| Linux / WSL2 | 任意 | dev 测过 Ubuntu 22.04 / openEuler 24.03 |
| Python | **3.12+** | async + 类型注解依赖 |
| PostgreSQL | **16+** | `pg_trgm` + `btree_gin` 扩展必装 |
| Neo4j | 5.x（可选） | 仅当用 M4 拓扑视图 |
| git | 2.40+ | OLK kernel clone 需要 |
| CodeGraph MCP server | 任意稳定版 | 独立 daemon，监听 :8080 |

### Python 包（最小必需）

```
fastapi >= 0.115
uvicorn[standard] >= 0.30
jinja2
httpx
openai >= 1.30
sqlalchemy >= 2.0
psycopg[binary] >= 3.1     # 或 psycopg2-binary
alembic >= 1.13
pydantic >= 2.0
langgraph >= 0.2
langchain-core >= 0.3      # 仅类型/接口
neo4j >= 5.0               # 可选（M4 拓扑）
click >= 8.0               # CLI
tenacity                   # 重试
```

**不需要**：sentence-transformers / faiss / chromadb / qdrant-client（ADR-001 拒绝 embedding）

---

## 2. 启动顺序

```
1. PostgreSQL daemon            ── 必备
2. Neo4j daemon                  ── 可选
3. CodeGraph MCP server :8080    ── M5 code/docs 路 + M7 lookup_symbol 等依赖
4. M3 ingest jobs（cron 或手动） ── 周度
5. M4 linker（M3 完成后立即跑）  ── 周度
6. uvicorn web.server:app :8000  ── M9 web UI
```

---

## 3. 配置文件结构

```
configs/
├── default.yaml                 ✓ 提交入库（公开默认值）
└── local.yaml                   ✗ gitignored（含 API key / token / 主机路径）
```

**契约**：`get_config()` 先读 `default.yaml`，再 deep-merge `local.yaml`（local 优先）。**`local.yaml` 永不进 git**。

### 3.1 `default.yaml` 完整结构

```yaml
app:
  name: linux-diag-agent
  env: production
  langgraph_checkpointer: pg              # pg | inmemory | none

llm:
  chat:
    backend: openai_compat                # claude_code | openai_compat | vllm
    model: deepseek-chat
    concurrency: 2
    self_consistency_k: 1
    timeout_seconds: 180
    rate_limit:
      rate_limit_rpm: 600
    retry:
      max_attempts: 4
      backoff_initial: 10.0
      backoff_max: 60.0
  navigator:
    backend: openai_compat
    model: deepseek-chat
    concurrency: 2
    timeout_seconds: 90
    rate_limit:
      rate_limit_rpm: 600
    retry:
      max_attempts: 4
      backoff_initial: 10.0
      backoff_max: 60.0
  endpoints:
    openai_compat: https://api.deepseek.com/v1
    api_key: ""                           # local.yaml 覆盖
    claude_code: ""                        # CLI 路径，默认 `claude`
    vllm: http://localhost:8000/v1

database:
  pg:
    dsn: postgresql://diag:diag@localhost:5432/diag_agent
  neo4j:
    enabled: false
    uri: bolt://localhost:7687
    user: neo4j
    password: ""                          # local.yaml 覆盖
    rebuild_cron: "0 3 * * 0"             # 每周日 3 AM

codegraph:
  endpoint: http://localhost:8080/mcp
  repo_mapping:
    "OLK-6.6": olk-kernel
    "OLK-5.10": olk-kernel
    "v6.6": olk-kernel
    "v5.10": olk-kernel
  timeout_s: 30

ingestion:
  lkml:
    lists:
      - linux-mm
      - stable
    lookback_days: 180
    batch_size: 200
  bugzilla:
    lookback_days: 60
    batch_size: 100
  gitee:
    token: ""                              # local.yaml 覆盖
    lookback_days: 30
  atomgit:
    token: ""                              # local.yaml 覆盖
  syzbot:
    lookback_days: 30
    batch_size: 50
  nvd:
    lookback_days: 180
    batch_size: 500
  kernel_commit:
    olk_repos:
      - name: OLK-6.6
        remote: https://atomgit.com/openeuler/kernel.git
        branch: OLK-6.6
        local_path: /data1/codes/OLK-6.6/kernel
      - name: OLK-5.10
        remote: https://atomgit.com/openeuler/kernel.git
        branch: OLK-5.10
        local_path: /data1/codes/OLK-5.10/kernel
    mainline_repo:
      remote: https://mirrors.bfsu.edu.cn/git/linux-stable.git
      local_path: /data1/codes/linux-stable
      fetch_tags: false

retrieval:
  limit_per_route: 10
  rerank_threshold: 1                       # 总数 > 此值触发 rerank（∞ = 关 rerank）
  rerank_top_k: 60                          # rerank 输入候选数

knowledge:
  mode: online                              # ADR-025 后默认 online
  offline_fallback: true

observability:
  log_dir: data/logs/
  jsonl_log_per_call: true

web:
  host: 127.0.0.1
  port: 8000

eval:
  judge_model: deepseek-chat                # navigator 模型即可
  concurrency: 1
```

### 3.2 `local.yaml` 模板（gitignored）

```yaml
# configs/local.yaml — 本地敏感配置，不进 git
llm:
  endpoints:
    api_key: sk-YOUR_DEEPSEEK_KEY            # https://platform.deepseek.com/api_keys

database:
  pg:
    dsn: postgresql://USER:PASS@HOST:5432/diag_agent

  neo4j:
    enabled: true
    password: YOUR_NEO4J_PASSWORD

ingestion:
  gitee:
    token: YOUR_GITEE_PAT                    # https://gitee.com/profile/personal_access_tokens
  atomgit:
    token: YOUR_ATOMGIT_PAT
  kernel_commit:
    olk_repos:
      - name: OLK-6.6
        local_path: /YOUR/PATH/OLK-6.6/kernel
      - name: OLK-5.10
        local_path: /YOUR/PATH/OLK-5.10/kernel
    mainline_repo:
      local_path: /YOUR/PATH/linux-stable
```

---

## 4. 网络 / 代理策略

| 端点 | 走代理？ |
|---|---|
| `api.deepseek.com` / `openrouter.ai` / `api.openai.com` | **不走** —— httpx 强制 `trust_env=False`（M1 § 6 #2） |
| `gitee.com` / `atomgit.com` | **不走**（用户明确：直连，gitee/atomgit 拒绝代理流量） |
| `services.nvd.nist.gov` | 看网络环境，默认不走 |
| `lore.kernel.org` | **不走**（限流敏感） |
| `mirrors.bfsu.edu.cn` | 清华镜像，**不走**代理 |
| 本地 `localhost:5432` / `:7687` / `:8080` | 自动 NO_PROXY |

环境变量推荐：

```bash
export NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
# 然后 httpx 代码层面 trust_env=False 兜底
```

---

## 5. 部署形态（3 种）

### 5.1 单机 dev（推荐起步）

```bash
# 1. PG
sudo -u postgres createdb diag_agent
sudo -u postgres psql diag_agent -c "
    CREATE EXTENSION pg_trgm;
    CREATE EXTENSION btree_gin;"

# 2. migrate
alembic -c storage/pg/alembic.ini upgrade head

# 3. CodeGraph
# 参考 CodeGraph 独立项目；约 `codegraph serve --port 8080`

# 4. ingest 一次（按需）
./scripts/weekly_sync.sh

# 5. link
python -c "from graph.linker import run_linker, link_nvd_commits, link_syzbot_commits; \
           from storage.pg.engine import get_engine; e = get_engine(); \
           print(run_linker(e), link_nvd_commits(e), link_syzbot_commits(e))"

# 6. web UI
python -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

### 5.2 内网 docker-compose

骨架（重建者按需补全）：

```yaml
services:
  pg:
    image: postgres:16
    environment: [POSTGRES_DB=diag_agent, POSTGRES_PASSWORD=...]
    volumes: [pgdata:/var/lib/postgresql/data]
  neo4j:
    image: neo4j:5
    environment: [NEO4J_AUTH=neo4j/...]
  codegraph:
    image: <codegraph-img>
    ports: ["8080:8080"]
    volumes: ["/opt/kernel-repos:/repos"]
  web:
    build: .
    ports: ["8000:8000"]
    depends_on: [pg, codegraph]
    environment:
      DIAG_AGENT_CONFIG: /app/configs/local.yaml
    volumes: ["./configs/local.yaml:/app/configs/local.yaml:ro"]

volumes:
  pgdata:
```

### 5.3 多用户（公开 / SaaS）

不在本交付范围 —— 至少需要补：认证（OAuth2 / SSO）/ 速率限制 / per-user usage quotas / TLS reverse proxy / 审计日志。详见 `80_KNOWN_LIMITS.md`。

---

## 6. 关键 cron / 定时任务

```cron
# 周度 ingest + linker
0 2 * * 1   cd /opt/diag-agent && ./scripts/weekly_sync.sh >> /var/log/diag-agent/sync.log 2>&1

# 周度 Neo4j rebuild（可选）
0 3 * * 0   cd /opt/diag-agent && python -m graph.neo4j_rebuild >> /var/log/diag-agent/neo4j.log 2>&1

# 月度 PG cache GC
0 4 1 * *   psql $DSN -c "DELETE FROM llm_response_cache WHERE ttl_until < now();"
```

---

## 7. 日志

| 日志 | 路径 | 内容 |
|---|---|---|
| LLM 调用 audit | `data/logs/<ts>.jsonl` | 每次 ChatRequest/Response 一行 JSON |
| ingester | `data/logs/ingest_<name>_<date>.log` | 每个 ingester 一份 |
| web server | uvicorn stdout | 推荐 systemd / docker logs |
| Neo4j rebuild | `data/logs/neo4j_rebuild_<date>.log` | |
| quarantine | `data/quarantine/<source>/<run_id>/<id>.txt` | 解析失败的原始数据 |

---

## 8. 监控关键指标

| 指标 | 阈值 | 来源 |
|---|---|---|
| ingest_runs 上次 success 距今 | < 8 天 | `SELECT max(finished_at) FROM ingest_runs WHERE status='success'` |
| LLM cache 命中率 | > 30 % | 自记 audit log 比例 |
| Web `/api/diagnose-stream` P95 latency | < 5 min | uvicorn access log |
| PG `kernel_commit` 行数月增长 | > 10K | 跟 OLK 周度合入对齐 |
| `llm_response_cache.cost_usd` 累计 | < 月预算 | `SELECT sum(cost_usd) FROM ... WHERE created_at > ...` |

无 Prometheus / ES 集成（M17 未启动）。

---

## 9. 重建后第一次跑通的最短路径

```bash
# 假设 PG / CodeGraph 已起，仓库已 clone
cd <repo>
pip install -r requirements.txt
cp configs/local.yaml.template configs/local.yaml
$EDITOR configs/local.yaml                    # 填 DeepSeek key + PG DSN
alembic upgrade head                          # PG schema
python -c "from clients.codegraph.client import get_codegraph_client; \
            print(get_codegraph_client().health_check())"
                                              # CodeGraph 连通

python -m eval.runner_v2 \
    --dataset delivery/50_TEST_FIXTURES/cases_smoke.json \
    --output /tmp/smoke/ --no-judge
                                              # 不需要 ingest 的 smoke 跑通就行

# 看到 verdict in {diagnosed, insufficient_evidence}, 0 errors → SHIP
```

---

> AI agent 重建后跑 § 9 最短路径成功 = 系统骨架达标；跑 ACCEPTANCE_CRITERIA § H 完整 22 case = 数据 + 行为达标。

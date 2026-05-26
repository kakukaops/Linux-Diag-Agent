"""Configuration loader — Pydantic v2 models + YAML merge."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Sub-models ───────────────────────────────────────────────────────────

class RateLimitConfig(BaseModel):
    window_seconds: int = 18000
    max_messages: int = 40
    rate_limit_rpm: int = 0  # per-minute cap for remote APIs (0 = disabled)


class RetryConfig(BaseModel):
    max_attempts: int = 3
    backoff_initial: float = 2.0
    backoff_max: float = 60.0


class LLMRoleConfig(BaseModel):
    backend: str = "claude_code"
    adapter: str = "subprocess"
    model: str = "claude-sonnet-4-6"
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    concurrency: int = 4
    retry: RetryConfig = Field(default_factory=RetryConfig)
    self_consistency_k: int = 1
    timeout_seconds: int = 120


class LLMCacheConfig(BaseModel):
    enabled: bool = True
    ttl_days: int = 30
    bypass_when_metadata_has: list[str] = Field(default_factory=lambda: ["eval", "force_fresh"])


class LLMEndpointsConfig(BaseModel):
    openai_compat: str = ""
    vllm: str = "http://localhost:8000/v1"
    ollama: str = "http://localhost:11434/v1"
    anthropic: str = "https://api.anthropic.com"
    api_key: str = ""  # fallback when env var not set; gitignored via local.yaml


class LLMConfig(BaseModel):
    chat: LLMRoleConfig = Field(default_factory=LLMRoleConfig)
    navigator: LLMRoleConfig = Field(default_factory=lambda: LLMRoleConfig(
        model="claude-haiku-4-5-20251001",
        timeout_seconds=60,
    ))
    cache: LLMCacheConfig = Field(default_factory=LLMCacheConfig)
    endpoints: LLMEndpointsConfig = Field(default_factory=LLMEndpointsConfig)


class PostgresConfig(BaseModel):
    dsn: str = "postgresql+psycopg://diag:diag_dev@localhost:5432/diag_agent"
    pool_size: int = 10
    max_overflow: int = 20
    echo: bool = False


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "diag_dev"
    database: str = "neo4j"


class DatabaseConfig(BaseModel):
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)


class CodeGraphConfig(BaseModel):
    endpoint: str = "http://localhost:8080/mcp"
    timeout_seconds: int = 30
    repo_map: dict[str, str] = Field(default_factory=lambda: {
        "v6.6": "olk-kernel-v6.6",
        "OLK-6.6": "olk-kernel-v6.6",
        "v5.10": "olk-kernel-v5.10",
        "OLK-5.10": "olk-kernel-v5.10",
    })


class OlkRepoConfig(BaseModel):
    name: str
    remote: str
    branch: str
    local_path: str = ""


class MainlineRepoConfig(BaseModel):
    remote: str = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
    local_path: str = ""
    fetch_tags: bool = True


class KernelCommitIngestionConfig(BaseModel):
    olk_repos: list[OlkRepoConfig] = Field(default_factory=list)
    mainline_repo: MainlineRepoConfig = Field(default_factory=MainlineRepoConfig)


class LkmlIngestionConfig(BaseModel):
    base_url: str = "https://lore.kernel.org"
    lists: list[str] = Field(default_factory=lambda: ["linux-kernel", "stable"])
    lookback_days: int = 730
    batch_size: int = 200


class GiteeIngestionConfig(BaseModel):
    """Gitee issue ingester config (ADR-022).

    `token` raises the rate limit from anonymous 60 rpm to PAT 5000/h.
    Generate at https://gitee.com/profile/personal_access_tokens (scopes:
    user_info + projects). Put it in configs/local.yaml (gitignored).
    """
    token: str | None = None


class AtomgitIngestionConfig(BaseModel):
    """Atomgit issue ingester config (ADR-022).

    Atomgit shares the gitee/gitcode v5 API shape; anonymous access works
    but token raises quota and avoids transient WAF interventions. Generate
    at https://atomgit.com/setting/token-classic (classic personal token).
    """
    token: str | None = None


class IngestionConfig(BaseModel):
    kernel_commit: KernelCommitIngestionConfig = Field(
        default_factory=KernelCommitIngestionConfig
    )
    lkml: LkmlIngestionConfig = Field(default_factory=LkmlIngestionConfig)
    gitee: GiteeIngestionConfig = Field(default_factory=GiteeIngestionConfig)
    atomgit: AtomgitIngestionConfig = Field(default_factory=AtomgitIngestionConfig)


class RetrievalConfig(BaseModel):
    top_k: int = 10
    routes: list[str] = Field(
        default_factory=lambda: ["code", "docs", "lkml", "bug", "syzbot", "commit", "cve"]
    )


class KnowledgeConfig(BaseModel):
    """知识数据架构模式（ADR-025）。

    online : LKML 走 L2 懒加载缓存 + L3 lore live search（默认）。
    offline: 气隙部署,仅用本地 L2 缓存,禁用 lore live search。
    """
    mode: str = "online"  # "online" | "offline"


class ObservabilityConfig(BaseModel):
    log_dir: str = "data/logs"
    metrics_dir: str = "data/metrics"
    traces_dir: str = "data/traces"
    log_level: str = "INFO"
    metrics_flush_seconds: int = 60
    log_retention_days: int = 30


class AppConfig(BaseModel):
    name: str = "linux-diag-agent"
    env: str = "development"
    data_dir: str = "data"


# ── Root config ──────────────────────────────────────────────────────────

class DiagAgentConfig(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    codegraph: CodeGraphConfig = Field(default_factory=CodeGraphConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


# ── Loader ───────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins on conflict)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_config(extra_yaml: str | None = None) -> DiagAgentConfig:
    """Load config: default.yaml → local.yaml → env vars → extra_yaml override."""
    base = _load_yaml(_REPO_ROOT / "configs" / "default.yaml")
    local = _load_yaml(_REPO_ROOT / "configs" / "local.yaml")
    merged = _deep_merge(base, local)

    if extra_yaml:
        extra = yaml.safe_load(extra_yaml) or {}
        merged = _deep_merge(merged, extra)

    # Env var overrides: DIAG_AGENT__LLM__CHAT__MODEL etc.
    # (Pydantic-settings handles this automatically via __init__)
    cfg = DiagAgentConfig.model_validate(merged)

    # Resolve local_path defaults for git repos
    data_dir = Path(cfg.app.data_dir)
    for repo in cfg.ingestion.kernel_commit.olk_repos:
        if not repo.local_path:
            repo.local_path = str(data_dir / "git" / f"olk-kernel-{repo.branch.lower()}")
    if not cfg.ingestion.kernel_commit.mainline_repo.local_path:
        cfg.ingestion.kernel_commit.mainline_repo.local_path = str(data_dir / "git" / "linux-stable")

    return cfg


def reset_config() -> None:
    """Clear cached config (for testing)."""
    get_config.cache_clear()

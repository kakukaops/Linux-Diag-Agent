"""Unit tests for configs/config.py."""

import pytest
from configs.config import DiagAgentConfig, get_config, reset_config


def test_default_config_loads():
    reset_config()
    cfg = get_config()
    assert isinstance(cfg, DiagAgentConfig)
    assert cfg.llm.chat.backend == "claude_code"
    assert cfg.llm.chat.rate_limit.max_messages == 40
    assert cfg.database.postgres.pool_size == 10


def test_codegraph_repo_map():
    reset_config()
    cfg = get_config()
    assert cfg.codegraph.repo_map["v6.6"] == "olk-kernel-v6.6"
    assert cfg.codegraph.repo_map["OLK-5.10"] == "olk-kernel-v5.10"


def test_config_cached():
    reset_config()
    c1 = get_config()
    c2 = get_config()
    assert c1 is c2


def test_llm_navigator_defaults():
    reset_config()
    cfg = get_config()
    assert "haiku" in cfg.llm.navigator.model.lower()
    assert cfg.llm.navigator.concurrency == 4


def test_kernel_commit_repos_default_paths():
    reset_config()
    cfg = get_config()
    repos = cfg.ingestion.kernel_commit.olk_repos
    assert any("olk-6.6" in r.local_path.lower() for r in repos)

"""Unit tests for configs/config.py."""

import pytest
import configs.config as _cfg_mod
from configs.config import DiagAgentConfig, get_config, reset_config


@pytest.fixture()
def default_only(monkeypatch):
    """Suppress local.yaml so tests see only default.yaml values."""
    original = _cfg_mod._load_yaml
    def _no_local(path):
        if "local.yaml" in str(path):
            return {}
        return original(path)
    monkeypatch.setattr(_cfg_mod, "_load_yaml", _no_local)
    reset_config()
    yield
    reset_config()


def test_default_config_loads(default_only):
    cfg = get_config()
    assert isinstance(cfg, DiagAgentConfig)
    assert cfg.llm.chat.backend == "claude_code"
    assert cfg.llm.chat.rate_limit.max_messages == 40
    assert cfg.database.postgres.pool_size == 10


def test_codegraph_repo_map():
    reset_config()
    cfg = get_config()
    # CodeGraph 实际索引名为 olk-kernel（list_repos 核实；OLK-6.6/5.10 同源同名）
    assert cfg.codegraph.repo_map["v6.6"] == "olk-kernel"
    assert cfg.codegraph.repo_map["OLK-5.10"] == "olk-kernel"


def test_config_cached():
    reset_config()
    c1 = get_config()
    c2 = get_config()
    assert c1 is c2


def test_llm_navigator_defaults(default_only):
    cfg = get_config()
    assert "haiku" in cfg.llm.navigator.model.lower()
    assert cfg.llm.navigator.concurrency == 4


def test_kernel_commit_repos_default_paths():
    reset_config()
    cfg = get_config()
    repos = cfg.ingestion.kernel_commit.olk_repos
    assert any("olk-6.6" in r.local_path.lower() for r in repos)

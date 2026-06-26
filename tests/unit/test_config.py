"""Tests for memcp.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from memcp.config import get_config
from memcp.core.errors import ValidationError


class TestMemCPConfig:
    def test_defaults(self, isolated_data_dir: Path) -> None:
        config = get_config()
        assert config.data_dir == isolated_data_dir
        assert config.max_memory_mb == 2048
        assert config.max_insights == 10000
        assert config.max_context_size_mb == 10
        assert config.importance_decay_days == 30

    def test_env_overrides(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        custom_dir = tmp_path / "custom"
        monkeypatch.setenv("MEMCP_DATA_DIR", str(custom_dir))
        monkeypatch.setenv("MEMCP_MAX_MEMORY_MB", "512")
        monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "500")
        monkeypatch.setenv("MEMCP_MAX_CONTEXT_SIZE_MB", "5")
        monkeypatch.setenv("MEMCP_IMPORTANCE_DECAY_DAYS", "7")

        config = get_config()
        assert config.data_dir == custom_dir.resolve()
        assert config.max_memory_mb == 512
        assert config.max_insights == 500
        assert config.max_context_size_mb == 5
        assert config.importance_decay_days == 7

    def test_paths(self, isolated_data_dir: Path) -> None:
        config = get_config()
        assert config.memory_path == isolated_data_dir / "memory.json"
        assert config.contexts_dir == isolated_data_dir / "contexts"
        assert config.chunks_dir == isolated_data_dir / "chunks"
        assert config.state_path == isolated_data_dir / "state.json"
        assert config.cache_dir == isolated_data_dir / "cache"

    def test_ensure_dirs(self, isolated_data_dir: Path) -> None:
        config = get_config()
        # get_config() calls ensure_dirs automatically
        assert config.data_dir.exists()
        assert config.contexts_dir.exists()
        assert config.chunks_dir.exists()
        assert config.cache_dir.exists()

    def test_singleton(self, isolated_data_dir: Path) -> None:
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2

    def test_expanduser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", "~/.memcp")
        config = get_config()
        assert "~" not in str(config.data_dir)
        assert config.data_dir.is_absolute()


class TestConfigValidation:
    def test_max_insights_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "0")
        with pytest.raises(ValidationError, match="max_insights must be > 0"):
            get_config()

    def test_max_insights_negative(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "-5")
        with pytest.raises(ValidationError, match="max_insights must be > 0"):
            get_config()

    def test_max_memory_mb_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_MAX_MEMORY_MB", "0")
        with pytest.raises(ValidationError, match="max_memory_mb must be > 0"):
            get_config()

    def test_max_context_size_mb_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_MAX_CONTEXT_SIZE_MB", "0")
        with pytest.raises(ValidationError, match="max_context_size_mb must be > 0"):
            get_config()

    def test_importance_decay_days_negative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_IMPORTANCE_DECAY_DAYS", "-1")
        with pytest.raises(ValidationError, match="importance_decay_days must be >= 0"):
            get_config()

    def test_purge_less_than_archive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_RETENTION_ARCHIVE_DAYS", "90")
        monkeypatch.setenv("MEMCP_RETENTION_PURGE_DAYS", "30")
        with pytest.raises(
            ValidationError, match="retention_purge_days.*must be >= retention_archive_days"
        ):
            get_config()

    def test_non_numeric_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "not_a_number")
        with pytest.raises(ValidationError, match="must be an integer"):
            get_config()

    def test_valid_edge_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import memcp.config as cfg

        cfg._config = None
        monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "1")
        monkeypatch.setenv("MEMCP_MAX_MEMORY_MB", "1")
        monkeypatch.setenv("MEMCP_MAX_CONTEXT_SIZE_MB", "1")
        monkeypatch.setenv("MEMCP_IMPORTANCE_DECAY_DAYS", "0")
        monkeypatch.setenv("MEMCP_RETENTION_ARCHIVE_DAYS", "0")
        monkeypatch.setenv("MEMCP_RETENTION_PURGE_DAYS", "0")
        config = get_config()
        assert config.max_insights == 1
        assert config.importance_decay_days == 0
        assert config.retention_archive_days == 0
        assert config.retention_purge_days == 0


def test_reindex_on_session_start_default_true(monkeypatch):
    monkeypatch.delenv("MEMCP_REINDEX_ON_SESSION_START", raising=False)
    from memcp import config as cfg_mod

    cfg_mod._config = None
    cfg = cfg_mod.get_config()
    assert cfg.reindex_on_session_start is True


def test_reindex_on_session_start_env_override(monkeypatch):
    monkeypatch.setenv("MEMCP_REINDEX_ON_SESSION_START", "false")
    from memcp import config as cfg_mod

    cfg_mod._config = None
    cfg = cfg_mod.get_config()
    assert cfg.reindex_on_session_start is False


def test_reindex_latency_warn_ms_default(monkeypatch):
    monkeypatch.delenv("MEMCP_REINDEX_LATENCY_WARN_MS", raising=False)
    from memcp import config as cfg_mod

    cfg_mod._config = None
    cfg = cfg_mod.get_config()
    assert cfg.reindex_latency_warn_ms == 3000


def test_semantic_recall_default_matches_arm_f_gate(monkeypatch):
    """Phase 4 Item 4, test 7 — the shipped default of MEMCP_SEMANTIC_RECALL
    MUST equal the pre-registered Arm F gate verdict, mechanically.

    The gate (docs/eval/run_arm_f.py) writes results-arm-f.json with a
    flip_rule.FLIP_DEFAULT boolean computed from the four pre-registered
    criteria. The default-on flip is allowed to land ONLY with that verdict, so
    this test reads the artifact and asserts the config default tracks it. A
    future re-run that flips the gate forces the config default to change in
    lockstep (and vice versa) — they cannot silently diverge.
    """
    import json

    repo_root = Path(__file__).resolve().parents[2]
    arm_f = json.loads((repo_root / "docs/eval/results-arm-f.json").read_text())
    flip = arm_f["flip_rule"]["FLIP_DEFAULT"]

    monkeypatch.delenv("MEMCP_SEMANTIC_RECALL", raising=False)
    from memcp import config as cfg_mod

    cfg_mod._config = None
    cfg = cfg_mod.get_config()
    assert cfg.semantic_recall_enabled is flip, (
        f"config default semantic_recall_enabled={cfg.semantic_recall_enabled} "
        f"must equal Arm F FLIP_DEFAULT={flip}"
    )

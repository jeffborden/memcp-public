"""MemCP configuration — env vars + directory management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemCPConfig:
    """Configuration loaded from environment variables with sensible defaults."""

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MEMCP_DATA_DIR", "~/.memcp")))
    max_memory_mb: int = field(
        default_factory=lambda: int(os.getenv("MEMCP_MAX_MEMORY_MB", "2048"))
    )
    max_insights: int = field(default_factory=lambda: int(os.getenv("MEMCP_MAX_INSIGHTS", "10000")))
    max_context_size_mb: int = field(
        default_factory=lambda: int(os.getenv("MEMCP_MAX_CONTEXT_SIZE_MB", "10"))
    )
    importance_decay_days: int = field(
        default_factory=lambda: int(os.getenv("MEMCP_IMPORTANCE_DECAY_DAYS", "30"))
    )
    retention_archive_days: int = field(
        default_factory=lambda: int(os.getenv("MEMCP_RETENTION_ARCHIVE_DAYS", "30"))
    )
    retention_purge_days: int = field(
        default_factory=lambda: int(os.getenv("MEMCP_RETENTION_PURGE_DAYS", "180"))
    )

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.expanduser().resolve()

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory.json"

    @property
    def contexts_dir(self) -> Path:
        return self.data_dir / "contexts"

    @property
    def chunks_dir(self) -> Path:
        return self.data_dir / "chunks"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def graph_db_path(self) -> Path:
        return self.data_dir / "graph.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / "sessions.json"

    def ensure_dirs(self) -> None:
        """Create all required directories if they don't exist."""
        dirs = [self.data_dir, self.contexts_dir, self.chunks_dir, self.cache_dir, self.archive_dir]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# Singleton — created once, reused everywhere.
_config: MemCPConfig | None = None


def get_config() -> MemCPConfig:
    """Get or create the global config singleton."""
    global _config
    if _config is None:
        _config = MemCPConfig()
        _config.ensure_dirs()
    return _config

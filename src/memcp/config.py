"""MemCP configuration — env vars + directory management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from memcp.core.errors import ValidationError
from memcp.core.snapshot_sync import _DEFAULT_MAX_BLOBS
from memcp.core.telemetry import default_telemetry_dir
from memcp.core.telemetry import is_enabled as _telemetry_enabled


def _parse_int_env(name: str, default: int) -> int:
    """Parse an integer from an environment variable with a clear error."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValidationError(
            f"Environment variable {name} must be an integer, got {raw!r}"
        ) from None


def _parse_float_env(name: str, default: float) -> float:
    """Parse a float from an environment variable with a clear error."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValidationError(
            f"Environment variable {name} must be a number, got {raw!r}"
        ) from None


@dataclass
class MemCPConfig:
    """Configuration loaded from environment variables with sensible defaults."""

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MEMCP_DATA_DIR", "~/.memcp")))
    max_memory_mb: int = field(default_factory=lambda: _parse_int_env("MEMCP_MAX_MEMORY_MB", 2048))
    max_insights: int = field(default_factory=lambda: _parse_int_env("MEMCP_MAX_INSIGHTS", 10000))
    max_context_size_mb: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_MAX_CONTEXT_SIZE_MB", 10)
    )
    importance_decay_days: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_IMPORTANCE_DECAY_DAYS", 30)
    )
    retention_archive_days: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_RETENTION_ARCHIVE_DAYS", 30)
    )
    retention_purge_days: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_RETENTION_PURGE_DAYS", 180)
    )
    # Intent ranker — kind-weight / edge-boost decouple (Phase 2 Item 1).
    # The kind: demotion is now an independent multiplicative factor on the
    # final relevance score, no longer tied to the edge term. The edge boost is
    # gated separately: when off, the two per-node COUNT(*) FROM edges queries
    # do not execute at all (the ~8.9x p50 latency win — 92.5ms->10.4ms on the
    # 1501-node freeze). Default flipped OFF after the Item 2 eval gate (Arm D):
    # D's nDCG was not significantly worse than A's (p=0.40), contamination was
    # identical (0.0), and D's p50 (10.4ms) was within 2x of B's (10.1ms). Set
    # MEMCP_EDGE_BOOST=true to opt back into the edge boost.
    edge_boost_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMCP_EDGE_BOOST", "false").lower() == "true"
    )
    kind_weight_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMCP_KIND_WEIGHT", "true").lower() == "true"
    )
    # Semantic recall (Phase 3) — blend a query-embedding-vs-stored-node-embedding
    # cosine term into the recall ranker so abstract behavioral phrasings bridge to
    # concrete nodes that share ~no keywords. When off, recall scores are
    # bit-identical to the keyword path and zero embeddings are computed. A
    # transiently unavailable (or uninstalled) embedding provider degrades to
    # keyword-only, so this is safe to leave on even without the semantic extras.
    # Default ON: the governing pre-registered flip gate passed all three
    # criteria on the full embedding+theme stack — ON beats OFF on nDCG@10
    # (0.657 vs 0.589, two-sided sign test p=0.0041), zero contamination delta,
    # p50 latency 19.5ms (well under the 75ms cap). Set MEMCP_SEMANTIC_RECALL=false
    # to disable.
    semantic_recall_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMCP_SEMANTIC_RECALL", "true").lower() == "true"
    )
    # Blend weight on the semantic term (0 = pure keyword, 1 = pure semantic).
    semantic_recall_weight: float = field(
        default_factory=lambda: _parse_float_env("MEMCP_SEMANTIC_WEIGHT", 0.5)
    )
    # Hebbian learning
    hebbian_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMCP_HEBBIAN_ENABLED", "true").lower() == "true"
    )
    hebbian_boost: float = field(
        default_factory=lambda: float(os.getenv("MEMCP_HEBBIAN_BOOST", "0.05"))
    )
    # Edge decay
    edge_decay_half_life: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_EDGE_DECAY_HALF_LIFE", 30)
    )
    edge_min_weight: float = field(
        default_factory=lambda: float(os.getenv("MEMCP_EDGE_MIN_WEIGHT", "0.05"))
    )
    # RRF search
    rrf_k: int = field(default_factory=lambda: _parse_int_env("MEMCP_RRF_K", 60))
    # Consolidation
    consolidation_threshold: float = field(
        default_factory=lambda: float(os.getenv("MEMCP_CONSOLIDATION_THRESHOLD", "0.85"))
    )
    # Reindex / derived-index rebuild
    reindex_on_session_start: bool = field(
        default_factory=lambda: (
            os.getenv("MEMCP_REINDEX_ON_SESSION_START", "true").lower() == "true"
        )
    )
    reindex_latency_warn_ms: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_REINDEX_LATENCY_WARN_MS", 3000)
    )
    # SQLite durability on synced storage (Google Drive etc.)
    # DELETE journal mode is the default: it writes the main db file directly at
    # commit and keeps no separate -wal/-shm sidecars, which a sync daemon would
    # otherwise propagate independently and shear. Override to "WAL" for purely
    # local storage where read/write concurrency matters more than sync-safety.
    sqlite_journal_mode: str = field(
        default_factory=lambda: os.getenv("MEMCP_SQLITE_JOURNAL_MODE", "DELETE").upper()
    )
    # Write-grained cross-process + best-effort cross-machine lock around commits.
    write_lock_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMCP_WRITE_LOCK", "true").lower() == "true"
    )
    write_lock_lease_ttl: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_WRITE_LOCK_TTL", 180)
    )
    write_lock_timeout: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_WRITE_LOCK_TIMEOUT", 30)
    )
    write_lock_settle_ms: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_WRITE_LOCK_SETTLE_MS", 0)
    )
    # Cross-machine snapshot sync. When set, the live DB stays local and a static
    # snapshot is synced through this (Drive-synced) directory. Empty = local-only.
    snapshot_dir: str = field(default_factory=lambda: os.getenv("MEMCP_SNAPSHOT_DIR", ""))
    snapshot_min_interval: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_SNAPSHOT_INTERVAL", 30)
    )
    # Hard backstop on retained snapshot blob count (content-verified cap). The
    # default lives in snapshot_sync as the single source of truth. <=0 disables.
    snapshot_max_blobs: int = field(
        default_factory=lambda: _parse_int_env("MEMCP_SNAPSHOT_MAX_BLOBS", _DEFAULT_MAX_BLOBS)
    )
    # When True, run the cross-machine pull synchronously before opening the DB
    # (legacy behavior). Default False: an established machine defers the pull to
    # the background flusher so a stalled snapshot mount (Google Drive) can never
    # block startup or the first request. A fresh machine (no local DB) always
    # adopts synchronously — there is nothing to serve yet.
    snapshot_pull_blocking: bool = field(
        default_factory=lambda: (
            os.getenv("MEMCP_SNAPSHOT_PULL_BLOCKING", "").lower() in ("1", "true", "yes")
        )
    )
    # Bound on the fresh-machine (no local DB) startup pull: it must run before
    # the DB is opened (the adopt path os.replace()s the file), but a stalled
    # Drive mount must not hang startup forever. On timeout, start with an empty
    # DB and defer the pull to the flusher (pull_pending). Ignored when
    # snapshot_pull_blocking is set (legacy fully-synchronous pull).
    snapshot_pull_timeout: float = field(
        default_factory=lambda: _parse_float_env("MEMCP_SNAPSHOT_PULL_TIMEOUT", 10.0)
    )
    # Local, metadata-only event telemetry (one JSONL line per tool call + sync
    # event). On by default; MEMCP_TELEMETRY=false disables, MEMCP_TELEMETRY_DIR
    # overrides the location. The resolved dir is guaranteed-local (never the
    # Drive mount) — see telemetry.default_telemetry_dir. These fields are for
    # surfacing in memcp_status; the telemetry module reads env itself.
    telemetry_enabled: bool = field(default_factory=_telemetry_enabled)
    telemetry_dir: str = field(default_factory=lambda: str(default_telemetry_dir()))

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.expanduser().resolve()
        self._validate()

    def _validate(self) -> None:
        """Validate configuration values."""
        if self.max_insights <= 0:
            raise ValidationError(f"max_insights must be > 0, got {self.max_insights}")
        if self.importance_decay_days < 0:
            raise ValidationError(
                f"importance_decay_days must be >= 0, got {self.importance_decay_days}"
            )
        if self.max_memory_mb <= 0:
            raise ValidationError(f"max_memory_mb must be > 0, got {self.max_memory_mb}")
        if self.max_context_size_mb <= 0:
            raise ValidationError(
                f"max_context_size_mb must be > 0, got {self.max_context_size_mb}"
            )
        if self.retention_purge_days < self.retention_archive_days:
            raise ValidationError(
                f"retention_purge_days ({self.retention_purge_days}) must be >= "
                f"retention_archive_days ({self.retention_archive_days})"
            )
        # journal_mode is interpolated into a PRAGMA — restrict to a known allowlist.
        valid_modes = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
        if self.sqlite_journal_mode not in valid_modes:
            raise ValidationError(
                f"sqlite_journal_mode must be one of {sorted(valid_modes)}, "
                f"got {self.sqlite_journal_mode!r}"
            )

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

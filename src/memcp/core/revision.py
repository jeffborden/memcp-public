"""Revision counter and index metadata helpers.

Every content-mutating write to the node store bumps meta.revision by 1.
Each derived index records the revision it was built against so staleness
detection is a simple integer comparison.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def get_revision(conn: sqlite3.Connection) -> int:
    """Read the current store revision."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    if row is None:
        return 0
    return int(row["value"] if hasattr(row, "keys") else row[0])


def bump_meta_counter(conn: sqlite3.Connection, key: str) -> int:
    """Atomically increment the integer meta counter `key`. Returns the new value.

    Callers should do this inside the same transaction as the write it guards so
    a crash leaves the counter consistent with the data. Shared by the revision
    counter and the ingest_seq allocator (§3.4).
    """
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = ?",
        (key,),
    )
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row["value"] if hasattr(row, "keys") else row[0])


def bump_revision(conn: sqlite3.Connection) -> int:
    """Atomically increment meta.revision. Returns the new value."""
    return bump_meta_counter(conn, "revision")


def get_index_meta(conn: sqlite3.Connection, index_name: str) -> dict[str, Any] | None:
    """Read the metadata row for a given index. Returns None if not set."""
    row = conn.execute(
        "SELECT index_name, built_against_revision, built_at, model_version, built_against_seq "
        "FROM index_meta WHERE index_name = ?",
        (index_name,),
    ).fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return {
        "index_name": row[0],
        "built_against_revision": row[1],
        "built_at": row[2],
        "model_version": row[3],
        "built_against_seq": row[4],
    }


def set_index_meta(
    conn: sqlite3.Connection,
    index_name: str,
    built_against_revision: int,
    built_at: str,
    model_version: str,
    built_against_seq: int = -1,
) -> None:
    """Upsert the metadata row for an index.

    ``built_against_seq`` is the ingest_seq high-water-mark this build covered
    (the §3.4 reindex cut) — strictly-monotonic per machine, so merged rows
    (fresh local seq) and clock-skewed local writes are never missed.
    """
    conn.execute(
        "INSERT INTO index_meta "
        "(index_name, built_against_revision, built_at, model_version, built_against_seq) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(index_name) DO UPDATE SET "
        "built_against_revision = excluded.built_against_revision, "
        "built_at = excluded.built_at, "
        "model_version = excluded.model_version, "
        "built_against_seq = excluded.built_against_seq",
        (index_name, built_against_revision, built_at, model_version, built_against_seq),
    )


def invalidate_index(conn: sqlite3.Connection, index_name: str) -> None:
    """Force a full rebuild of an index on next run.

    Called from deletion paths (forget, prune, consolidate, purge) because
    node deletions can leave surviving nodes' semantic-edge top-K picks stale.
    Deleting the index_meta row causes the next rebuild to treat it as a
    first build and process all nodes.
    """
    conn.execute("DELETE FROM index_meta WHERE index_name = ?", (index_name,))

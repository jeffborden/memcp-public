#!/usr/bin/env python3
"""Archive aged-out kind:pointer + kind:episode insights.

Designed to be invoked weekly by launchd (or any scheduler). Reads from the
local graph.db, filters for insights matching configured kind: tags older
than the cutoff, archives each via core.retention.archive_insight, and logs
a single-line summary.

Defaults: project=my_project, kinds=[kind:pointer, kind:episode],
max_age_days=14. Override via CLI flags or env vars (MEMCP_RETENTION_PROJECT,
MEMCP_RETENTION_KINDS, MEMCP_RETENTION_MAX_AGE_DAYS).

Exit codes: 0 success, 1 unrecoverable error.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PROJECT = "my_project"
DEFAULT_KINDS = "kind:pointer,kind:episode"
DEFAULT_MAX_AGE_DAYS = 14
LOG_PATH = Path.home() / "Library" / "Logs" / "memcp-retention.log"


def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("memcp-retention")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(sh)
    return logger


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--project",
        default=os.environ.get("MEMCP_RETENTION_PROJECT", DEFAULT_PROJECT),
        help=f"MemCP project to sweep (default: {DEFAULT_PROJECT})",
    )
    p.add_argument(
        "--kinds",
        default=os.environ.get("MEMCP_RETENTION_KINDS", DEFAULT_KINDS),
        help=f"Comma-separated kind: tags to match (default: {DEFAULT_KINDS})",
    )
    p.add_argument(
        "--max-age-days",
        type=int,
        default=int(os.environ.get("MEMCP_RETENTION_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)),
        help=f"Archive insights older than this many days (default: {DEFAULT_MAX_AGE_DAYS})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List candidates but don't archive",
    )
    return p.parse_args()


def find_candidates(project: str, kinds: list[str], cutoff: datetime) -> list[dict]:
    from memcp.config import get_config
    from memcp.core.graph import GraphMemory

    g = GraphMemory(get_config().graph_db_path)
    try:
        conn = g._get_conn()
        rows = conn.execute(
            "SELECT id, content, summary, category, importance, tags, created_at "
            "FROM nodes WHERE project = ?",
            (project,),
        ).fetchall()
    finally:
        g.close()

    candidates: list[dict] = []
    for r in rows:
        try:
            tags = json.loads(r["tags"]) if r["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        if not any(t in kinds for t in tags):
            continue
        try:
            created = datetime.fromisoformat(r["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if created >= cutoff:
            continue
        candidates.append(
            {
                "id": r["id"],
                "category": r["category"],
                "importance": r["importance"],
                "tags": tags,
                "created_at": created.isoformat(),
                "summary": (r["summary"] or r["content"] or "")[:120].replace("\n", " "),
            }
        )
    return candidates


def main() -> int:
    logger = setup_logging()
    args = parse_args()
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.max_age_days)

    logger.info(
        "sweep start project=%s kinds=%s max_age_days=%d cutoff=%s dry_run=%s",
        args.project, kinds, args.max_age_days, cutoff.isoformat(), args.dry_run,
    )

    try:
        candidates = find_candidates(args.project, kinds, cutoff)
    except Exception as e:
        logger.error("find_candidates failed: %s\n%s", e, traceback.format_exc())
        return 1

    logger.info("found %d candidates", len(candidates))
    if not candidates:
        logger.info("sweep done — nothing to archive")
        return 0

    if args.dry_run:
        for c in candidates:
            logger.info("DRY [%s] %s · %s · %s", c["id"], c["category"], c["created_at"][:10], c["summary"])
        return 0

    from memcp.core.errors import InsightNotFoundError
    from memcp.core.retention import archive_insight

    archived = 0
    failed = 0
    for c in candidates:
        try:
            archive_insight(c["id"])
            archived += 1
            logger.info("archived [%s] %s · %s · %s", c["id"], c["category"], c["created_at"][:10], c["summary"])
        except InsightNotFoundError:
            logger.warning("not found (likely already archived) [%s]", c["id"])
            failed += 1
        except Exception as e:
            logger.error("archive failed [%s]: %s", c["id"], e)
            failed += 1

    logger.info("sweep done — archived=%d failed=%d total_candidates=%d", archived, failed, len(candidates))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

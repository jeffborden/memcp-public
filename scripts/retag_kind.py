#!/usr/bin/env python3
"""Batch-retag insights missing `kind:` tags in graph.db.

Rules engine (first match wins):
  R1: category=episode                          -> kind:episode
  R2: summary/content matches handoff patterns  -> kind:pointer
  R3: category=todo                             -> kind:op
  R4: summary/content matches SUPERSEDED etc.   -> kind:op
  R5: category in (decision,fact,preference)    -> kind:kb
  R6: category=finding                          -> kind:kb
  R7: category=general + scan summary/brainstorm-> kind:kb
  R8: category=general (catch-all)              -> kind:kb

Usage:
  python scripts/retag_kind.py              # dry-run (default)
  python scripts/retag_kind.py --apply      # execute changes
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# Allow imports from project root when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcp.config import get_config  # noqa: E402

# ---------------------------------------------------------------------------
# Rule patterns
# ---------------------------------------------------------------------------

_POINTER_RE = re.compile(
    r"session handoff|EOD pointer|pickup pointer|pickup handoff"
    r"|Session F laptop|Session G handoff|Session H desktop",
    re.IGNORECASE,
)

_SUPERSEDED_RE = re.compile(r"SUPERSEDED|re-iteration pending", re.IGNORECASE)

_GENERAL_KB_RE = re.compile(r"scan summary|brainstorm", re.IGNORECASE)


def _text(row: dict) -> str:
    """Combine summary + content for pattern matching."""
    return (row.get("summary") or "") + " " + (row.get("content") or "")


def classify(row: dict) -> tuple[str, str] | None:
    """Return (rule_name, kind_tag) or None if already tagged."""
    cat = (row.get("category") or "").lower()
    text = _text(row)

    # R1
    if cat == "episode":
        return ("R1", "kind:episode")
    # R2
    if _POINTER_RE.search(text):
        return ("R2", "kind:pointer")
    # R3
    if cat == "todo":
        return ("R3", "kind:op")
    # R4
    if _SUPERSEDED_RE.search(text):
        return ("R4", "kind:op")
    # R5
    if cat in ("decision", "fact", "preference"):
        return ("R5", "kind:kb")
    # R6
    if cat == "finding":
        return ("R6", "kind:kb")
    # R7
    if cat == "general" and _GENERAL_KB_RE.search(text):
        return ("R7", "kind:kb")
    # R8
    if cat == "general":
        return ("R8", "kind:kb")

    # No rule matched (shouldn't happen for known categories)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually write changes (default is dry-run)"
    )
    args = parser.parse_args()

    db_path = get_config().graph_db_path
    if not db_path.exists():
        print(f"ERROR: graph.db not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, summary, content, category, tags FROM nodes").fetchall()

    rule_counts: dict[str, int] = {}
    updates: list[tuple[str, str]] = []  # (new_tags_json, node_id)

    for row in rows:
        row_dict = dict(row)
        try:
            tags = json.loads(row_dict["tags"]) if row_dict["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []

        # Skip if already has a kind: tag
        if any(t.startswith("kind:") for t in tags):
            continue

        result = classify(row_dict)
        if result is None:
            continue

        rule_name, kind_tag = result
        rule_counts[rule_name] = rule_counts.get(rule_name, 0) + 1

        node_id = row_dict["id"]
        summary_preview = (row_dict.get("summary") or row_dict.get("content") or "").replace(
            "\n", " "
        )[:70]

        print(f"  {rule_name}  {node_id[:8]}  {kind_tag:<14}  {summary_preview}")

        new_tags = tags + [kind_tag]
        updates.append((json.dumps(new_tags), node_id))

    # Summary
    print()
    print(f"Total to tag: {len(updates)}")
    if rule_counts:
        print("Per rule:")
        for rule in sorted(rule_counts):
            print(f"  {rule}: {rule_counts[rule]}")

    if not updates:
        print("Nothing to do.")
        return 0

    if args.apply:
        print()
        print(f"Applying {len(updates)} updates...")
        conn.executemany("UPDATE nodes SET tags = ? WHERE id = ?", updates)
        conn.commit()
        print(f"Done. {len(updates)} nodes tagged.")
    else:
        print()
        print("Dry-run complete. Re-run with --apply to execute.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

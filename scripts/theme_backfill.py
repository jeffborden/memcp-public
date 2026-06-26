#!/usr/bin/env python3
"""Blind theme backfill — derive behavioral/conceptual theme lines per node.

Themes are machine-local DERIVED data (ADR-014): this script reads node content
from a graph.db and writes 1–2 theme lines per node into the local theme cache
(cache/themes.sqlite). The semantic embedding path then prepends those theme
lines to a node's embedded text so abstract behavioral queries bridge to concrete
nodes (the 2026-06-11 bake-off: bge-small + theme lines bridges 4/5 dead queries
vs 2/5 alone).

BLIND PROTOCOL (hard rule): the theme-generation prompt is built ONLY from node
content. It must never contain eval queries or query-derived vocabulary — the
bake-off's 4/5 had author bias, and the gate measures the blind version. See
PROMPT_TEMPLATE / build_prompt: the only variable input is node text.

RESUMABLE: a node whose content already has a sha-matching theme is skipped, so a
second run over unchanged content makes zero LLM calls; changed-sha nodes
re-theme (lazy invalidation via the (node_id, content_sha) key).

LIVE-DB SAFETY: pass --db explicitly. For the eval gate this points at the frozen
working copy. The post-merge live backfill (per machine, supervised) points it at
the live graph.db. This script never assumes ~/.memcp-local.

Usage:
  # Review a sample first (writes a human-readable file, NO cache writes):
  python scripts/theme_backfill.py --db <graph.db> --sample 25 --sample-out themes-sample.md
  # Full sweep:
  python scripts/theme_backfill.py --db <graph.db>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

# Make `import memcp...` work when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memcp.core.theme_cache import content_sha, get_theme_cache  # noqa: E402

# ── Eligibility ───────────────────────────────────────────────────────────────
# Demoted kinds are skipped — they're already ranked below durable kb knowledge
# and aren't the behavioral content the bridging queries target.
DEMOTED_KINDS = {"kind:pointer", "kind:op", "kind:episode"}
KB_CATEGORIES = {"decision", "finding", "preference", "general"}

# How much node text to send to the themer. Long mixed-topic dossiers are the
# hard case; the head carries enough signal for a behavioral theme line.
PROMPT_CONTENT_CHARS = 1500

# ── BLIND prompt template — built ONLY from node content ──────────────────────
# No eval query, no task name, no query-derived vocabulary appears here. The sole
# variable is {notes_block}, which build_prompt fills with node id + node text.
PROMPT_TEMPLATE = """\
You are annotating a person's private knowledge-base notes to improve semantic
search over them.

For EACH note below, write 1-2 short theme lines that capture its behavioral or
conceptual themes — the kinds of situations, skills, tradeoffs, or decisions the
note exemplifies — phrased in general, abstract terms. Derive the themes ONLY
from the note's own text. Do not invent anything not present in the note. Do not
restate the note; name the underlying themes.

Output EXACTLY one line per note, in this tab-separated form and nothing else:
<NODE_ID><TAB>theme phrase; theme phrase

Notes:
{notes_block}
"""


def build_prompt(nodes: list[tuple[str, str]]) -> str:
    """Build the blind themer prompt from (node_id, content) pairs only."""
    notes_block = "\n\n".join(
        f"[{nid}]\n{(content or '')[:PROMPT_CONTENT_CHARS]}" for nid, content in nodes
    )
    return PROMPT_TEMPLATE.format(notes_block=notes_block)


def _parse_tags(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return list(val) if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def select_eligible_nodes(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Active nodes eligible for theming: kb / kind-unset / kb-categories,
    skipping demoted pointer/op/episode. Returns (id, content) in ingest order.
    """
    rows = conn.execute(
        "SELECT id, content, tags, category FROM nodes "
        "WHERE archived_at IS NULL ORDER BY ingest_seq"
    ).fetchall()
    out: list[tuple[str, str]] = []
    for r in rows:
        tags = _parse_tags(r["tags"])
        if any(t in DEMOTED_KINDS for t in tags):
            continue
        kinds = [t for t in tags if t.startswith("kind:")]
        if "kind:kb" in kinds or not kinds or (r["category"] in KB_CATEGORIES):
            out.append((r["id"], r["content"] or ""))
    return out


def parse_response(resp: str, batch: list[tuple[str, str]]) -> dict[str, str]:
    """Parse tab-separated `<id>\\t<themes>` lines back to batch node ids."""
    ids = {nid for nid, _ in batch}
    out: dict[str, str] = {}
    for line in resp.splitlines():
        line = line.strip()
        if "\t" not in line:
            continue
        nid, themes = line.split("\t", 1)
        nid = nid.strip().strip("[]").strip()
        themes = themes.strip()
        if not themes:
            continue
        if nid in ids:
            out[nid] = themes
            continue
        matches = [x for x in ids if x.startswith(nid) or nid.startswith(x)]
        if len(matches) == 1:
            out[matches[0]] = themes
    return out


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def call_haiku(prompt: str, model: str = "haiku") -> str:
    """Invoke `claude -p --model <model>` with the prompt on stdin."""
    proc = subprocess.run(
        ["claude", "-p", "--model", model],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p --model {model} failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )
    return proc.stdout


def make_claude_llm(model: str = "haiku"):
    """Return an llm_fn(prompt, batch) -> {node_id: themes} backed by claude -p."""

    def _fn(prompt: str, batch: list[tuple[str, str]]) -> dict[str, str]:
        return parse_response(call_haiku(prompt, model), batch)

    return _fn


def run_backfill(
    conn: sqlite3.Connection,
    cache,
    llm_fn,
    batch_size: int = 20,
    model: str = "haiku",
    limit: int | None = None,
    progress=None,
) -> dict:
    """Theme every eligible node lacking a sha-matching theme. Resumable."""
    eligible = select_eligible_nodes(conn)
    todo = [(nid, c) for (nid, c) in eligible if not cache.has_valid(nid, content_sha(c))]
    skipped = len(eligible) - len(todo)
    if limit is not None:
        todo = todo[:limit]

    themed = 0
    llm_calls = 0
    for batch in _chunks(todo, batch_size):
        result = llm_fn(build_prompt(batch), batch)
        llm_calls += 1
        for nid, content in batch:
            themes = result.get(nid)
            if not themes:
                continue
            cache.put(nid, content_sha(content), themes, model)
            themed += 1
        if progress:
            progress(themed, len(todo))
    return {
        "eligible": len(eligible),
        "todo": len(todo),
        "themed": themed,
        "skipped": skipped,
        "llm_calls": llm_calls,
    }


def run_sample(conn, llm_fn, n: int = 25, out_path: str | None = None, batch_size: int = 20) -> str:
    """Theme the first N eligible nodes, write a human-readable review file, stop.

    Preview only — does NOT write to the theme cache. The runbook tells Jeff to
    eyeball this before a full sweep.
    """
    eligible = select_eligible_nodes(conn)[:n]
    lines = [
        "# Theme backfill — review sample",
        "",
        f"{len(eligible)} BLIND themes (content-only). Eyeball before the full sweep. "
        "These are NOT written to the theme cache.",
        "",
    ]
    for batch in _chunks(eligible, batch_size):
        result = llm_fn(build_prompt(batch), batch)
        for nid, content in batch:
            snippet = (content or "")[:160].replace("\n", " ")
            lines.append(f"## {nid}")
            lines.append(f"- **content:** {snippet}…")
            lines.append(f"- **themes:** {result.get(nid, '(no theme returned)')}")
            lines.append("")
    out = out_path or "theme-sample.md"
    Path(out).write_text("\n".join(lines))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Blind theme backfill for MemCP semantic recall.")
    parser.add_argument("--db", default=None, help="graph.db path (default: config graph_db_path)")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--sample",
        type=int,
        nargs="?",
        const=25,
        default=None,
        help="Review-sample mode: theme first N (default 25), write a file, STOP.",
    )
    parser.add_argument("--sample-out", default=None, help="Output path for --sample.")
    parser.add_argument("--limit", type=int, default=None, help="Cap nodes themed (full mode).")
    parser.add_argument("--model", default="haiku")
    args = parser.parse_args(argv)

    if args.db:
        db_path = args.db
    else:
        from memcp.config import get_config

        db_path = str(get_config().graph_db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    llm = make_claude_llm(args.model)

    if args.sample is not None:
        out = run_sample(
            conn, llm, n=args.sample, out_path=args.sample_out, batch_size=args.batch_size
        )
        print(f"wrote review sample → {out}")
        conn.close()
        return 0

    cache = get_theme_cache()

    def _progress(done: int, total: int) -> None:
        print(f"  themed {done}/{total}", flush=True)

    stats = run_backfill(
        conn, cache, llm, batch_size=args.batch_size, model=args.model, limit=args.limit,
        progress=_progress,
    )
    conn.close()
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

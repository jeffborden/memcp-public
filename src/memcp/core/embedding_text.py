"""Embedding-text composition — theme-enriched text for the semantic path.

A node's embedded text is ``themes + "\\n" + content[:N]`` when a valid
(sha-matching) theme exists in the machine-local theme cache, plain content
otherwise. The composition is gated by ``MEMCP_SEMANTIC_RECALL``: with the flag
OFF the function returns RAW content unchanged, bit-identical to the phase-3
embedding path (flag-off no-op preserved). With the flag ON, a node that lacks a
theme (or whose theme is stale) also embeds plain content — only a fresh,
sha-matching theme enriches the text.
"""

from __future__ import annotations

# How much node content to keep after the theme line. Matches the bake-off's
# content window — long mixed-topic dossiers dilute behavioral themes, so the
# theme line carries the bridging signal and the content tail grounds it.
EMBED_CONTENT_CHARS = 2000


def compose_embedding_text(node_id: str, content: str) -> str:
    """Return the text to embed for ``node_id``.

    - ``MEMCP_SEMANTIC_RECALL`` off → ``content`` unchanged (phase-3 noop).
    - on + valid theme → ``f"{themes}\\n{content[:N]}"``.
    - on + missing/stale theme → ``content`` (plain).
    """
    from memcp.config import get_config

    if not get_config().semantic_recall_enabled:
        return content

    themes = _valid_themes_for(node_id, content)
    if not themes:
        return content
    return f"{themes}\n{content[:EMBED_CONTENT_CHARS]}"


def _valid_themes_for(node_id: str, content: str) -> str | None:
    """Look up a sha-matching theme for ``node_id``; None if absent/stale."""
    try:
        from memcp.core.theme_cache import content_sha, get_theme_cache

        return get_theme_cache().get_valid(node_id, content_sha(content))
    except Exception:
        # A missing/locked theme cache must never break embedding — degrade to
        # plain content (the derived store is rebuildable, never load-bearing).
        return None

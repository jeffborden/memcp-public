# MAGMA Graph Memory

MemCP implements a 4-graph memory architecture inspired by the MAGMA paper (arXiv:2601.03236). Instead of flat JSON insights, insights become graph nodes connected by four types of relationships that Claude can traverse.

## Architecture

```mermaid
graph LR
    subgraph "4-Graph on Shared Node Set"
        N1((Insight A)) ---|semantic| N2((Insight B))
        N1 -->|causal| N3((Insight C))
        N2 ---|entity: SQLite| N3
        N1 ---|temporal| N4((Insight D))
        N3 ---|temporal| N4
    end
```

All four edge types share the same node set (insights) and are stored in a single SQLite database (`~/.memcp/graph.db`) with WAL mode for concurrent reads.

## SQLite Schema

```sql
-- Nodes (insights)
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    category TEXT DEFAULT 'general',
    importance TEXT DEFAULT 'medium',
    effective_importance REAL DEFAULT 0.5,
    tags TEXT DEFAULT '[]',          -- JSON array
    entities TEXT DEFAULT '[]',      -- JSON array
    project TEXT DEFAULT 'default',
    session TEXT DEFAULT '',
    token_count INTEGER DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    created_at TEXT NOT NULL
);

-- Edges (relationships)
CREATE TABLE edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL CHECK(edge_type IN ('semantic','temporal','causal','entity')),
    weight REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '{}',      -- JSON object
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, edge_type),
    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE
);
```

Indexes on `edges(source_id)`, `edges(target_id)`, `edges(edge_type)`, `nodes(project)`, `nodes(category)`, `nodes(importance)`.

## Edge Types

### Semantic Edges

Connect insights with similar content.

**Generation**: On insert, compares the new insight against all existing insights in the same project:
1. **With embeddings** (if `model2vec`/`fastembed` installed): Embeds the insight text + tags, computes cosine similarity against a `VectorStore`. Links to top-3 most similar with `score >= 0.3`.
2. **Without embeddings** (keyword fallback): Tokenizes content + tags, computes set overlap ratio: `len(overlap) / max(len(tokens_a), len(tokens_b))`. Links to top-3 with `score >= 0.1`.

**Weight**: The similarity score (0.0–1.0).

### Temporal Edges

Connect insights created close in time (within 30 minutes, same project).

**Generation**: On insert, finds the 20 most recent nodes in the same project. For each within 30 minutes, creates a temporal edge.

**Weight**: `max(0.1, 1.0 - delta_minutes / 30)` — closer in time = higher weight.

### Causal Edges

Connect cause → effect pairs, detected by keyword patterns.

**Generation**: Scans the new insight's content for causal language:
```
because, therefore, due to, caused by, as a result,
decided to, chosen because, so that, in order to,
leads to, results in
```

If found, examines the 10 most recent insights in the same project. Computes token overlap between the new insight and each candidate. If overlap of 3+ tokens with ratio >= 0.15, creates a directional edge (new insight → cause). Links to at most one cause.

**Weight**: The token overlap ratio.

### Entity Edges

Connect insights that mention the same entities.

**Generation**: On insert, extracts entities from the content via `RegexEntityExtractor`, then finds all other nodes containing the same entities (case-insensitive match on the `entities` JSON array).

**Entity extraction patterns** (`RegexEntityExtractor`):

| Type | Pattern | Examples |
|------|---------|----------|
| File | `[.\w/-]+\.\w{1,10}` | `src/memcp/server.py`, `./config.json` |
| Module | `\w+(\.\w+){2,}` | `memcp.core.graph`, `os.path.join` |
| URL | `https?://[^\s"'<>)]+` | `https://github.com/...` |
| Mention | `@\w+` | `@claude`, `@user` |
| Identifier | `[A-Z][a-z]+([A-Z][a-z]+)+` | `GraphMemory`, `EntityExtractor` |

Entities shorter than 3 characters are ignored. Duplicates (case-insensitive) are deduplicated.

**Weight**: 1.0 (binary — either shares an entity or doesn't).

**Metadata**: `{"entity": "the shared entity name"}`.

## Intent-Aware Query Traversal

When `memcp_recall(query)` is called, the graph detects the query's intent and boosts the corresponding edge type:

| Query intent | Detection | Primary edge type |
|-------------|-----------|-------------------|
| **why** | Starts with "why", contains "reason" or "cause" | Causal |
| **when** | Starts with "when", contains "timeline" or "chronolog" | Temporal |
| **who/which** | Starts with "who"/"which", contains "entity" | Entity |
| **what** (default) | Everything else | Semantic |

### Ranking Formula

```
total_score = keyword_score * 0.7 + edge_boost * 0.3
```

- **keyword_score**: `len(query_tokens & doc_tokens) / len(query_tokens)` — fraction of query tokens found in the document
- **edge_boost**: Computed from the node's edge connectivity for the primary type:

```
edge_boost = min(1.0, primary_count / max(1, total_count) + 0.1 * primary_count)
```

Where `primary_count` = edges of the intent-relevant type, `total_count` = all edges.

## Graph Traversal

`memcp_related(insight_id, edge_type, depth)` performs breadth-first traversal:

1. Start from the center node
2. For each depth level, find all edges connected to the current frontier
3. Follow edges to discover new nodes (avoiding revisits)
4. Optionally filter by edge type

Returns:
```json
{
    "center": { ... },
    "related": [ ... ],
    "edges": [ ... ],
    "depth": 1,
    "edge_type_filter": "all"
}
```

## Migration from JSON

When MemCP first encounters `graph.db` not existing but `memory.json` present, it can migrate via `GraphMemory.migrate_from_json(memory)`. This re-inserts all insights as nodes and auto-generates edges.

## LLM Entity Extraction (Phase 4)

The `EntityExtractor` base class supports pluggable implementations:

- **Phase 3** (current): `RegexEntityExtractor` — fast, pattern-based
- **Phase 4** (sub-agents): `memcp-entity-extractor` sub-agent uses Claude's NLU to extract natural language entities ("the auth system", "Mohamed's preference") that regex misses

The sub-agent outputs entities that are then fed back via `memcp_remember(entities="entity1,entity2,...")`, which triggers `GraphMemory.store()` to auto-generate entity edges.

## Graph Statistics

`memcp_graph_stats(project)` returns:

```json
{
    "node_count": 42,
    "edge_counts": {
        "semantic": 120,
        "temporal": 85,
        "causal": 15,
        "entity": 67
    },
    "total_edges": 287,
    "top_entities": [
        {"entity": "graph.db", "count": 12},
        {"entity": "GraphMemory", "count": 8},
        ...
    ]
}
```

# Benchmark Report: Claude Native vs Claude with RLM

*Generated: 2026-02-18 10:37 UTC*


---


## Context Rot Resistance

*Knowledge retained after simulated compaction or session boundaries.*


| Scenario | Metric | Native | RLM | Unit |
|----------|--------|--------|-----|------|
| Context Rot: Single Compaction | Knowledge retained after /compact | 5.0% | 100.0% | % |
| Context Rot: Cascading Compactions (3x) | Knowledge retained after 3 compactions | 2.0% | 100.0% | % |
| Context Rot: Cross-Session | Session-1 knowledge from session-5 | 0.0% | 92.0% | % |
| Context Rot: Importance Decay | Critical insight retention at 60 days | 0.0% | 100.0% | % |

## Context Window Management

*How efficiently each mode uses the fixed-size context window.*


| Scenario | Metric | Native | RLM | RLM Advantage | Unit |
|----------|--------|--------|-----|---------------|------|
| Context Window: Progressive Fill | Docs before first eviction | 10 | 20 | 2.0x more | docs |
| Context Window: Progressive Fill | Final utilisation after 20 docs | 95.0% | 0.9% | 100.0x less | % |
| Context Window Capacity (32K) | Documents manageable | 3.2 | 50 | 15.6x more | docs |
| Context Window Capacity (64K) | Documents manageable | 6.4 | 50 | 7.8x more | docs |
| Context Window Capacity (128K) | Documents manageable | 12.8 | 50 | 3.9x more | docs |
| Context Window Capacity (200K) | Documents manageable | 20 | 50 | 2.5x more | docs |
| Working Set: 10 Simultaneous Docs | Context window utilisation | 93.6% | 1.0% | 93.6x less | % |
| Compaction Pressure | Turns before first eviction | 0 | 100 | +100 | turns |

## Scale Behavior

*How token costs grow as the knowledge base grows (N = number of insights).*


| N | Metric | Native (tokens) | RLM (tokens) | RLM Advantage |
|---|--------|-----------------|--------------|---------------|
| Scale: Recall from 10 | Tokens to retrieve 10 insights | 186 | 0 | zero cost |
| Scale: Recall from 100 | Tokens to retrieve 10 insights | 1,802 | 94 | 19.2x less |
| Scale: Recall from 500 | Tokens to retrieve 10 insights | 9,063 | 187 | 48.5x less |
| Scale: Recall from 1000 | Tokens to retrieve 10 insights | 17,996 | 187 | 96.2x less |
| Scale: Recall from 5000 | Tokens to retrieve 10 insights | 91,705 | 187 | 490.4x less |
| Scale: Overhead for 10 insights | Tokens to keep knowledge available | 186 | 13 | 14.3x less |
| Scale: Overhead for 100 insights | Tokens to keep knowledge available | 1,802 | 13 | 138.6x less |
| Scale: Overhead for 500 insights | Tokens to keep knowledge available | 9,063 | 13 | 697.2x less |
| Scale: Overhead for 1000 insights | Tokens to keep knowledge available | 17,996 | 14 | 1285.4x less |
| Scale: Overhead for 5000 insights | Tokens to keep knowledge available | 91,705 | 14 | 6550.4x less |
| Scale: Search across 10 | Tokens to search and return top-10 | 186 | 83 | 2.2x less |
| Scale: Search across 100 | Tokens to search and return top-10 | 1,802 | 183 | 9.8x less |
| Scale: Search across 500 | Tokens to search and return top-10 | 9,063 | 193 | 47.0x less |
| Scale: Search across 1000 | Tokens to search and return top-10 | 17,996 | 191 | 94.2x less |
| Scale: Search across 5000 | Tokens to search and return top-10 | 91,705 | 192 | 477.6x less |

---

## Methodology Notes


### What this benchmark measures


This benchmark compares two modes of operating Claude Code:


- **Native mode**: Models the worst-case scenario where all prior knowledge must be loaded into the context window as raw text to be searchable. This represents sessions where accumulated conversation history, documents, and decisions consume context window capacity.

- **RLM mode**: Uses MemCP's persistent storage (SQLite + disk) to keep knowledge outside the context window, loading only targeted results (via `recall()`, `inspect()`, `filter_context()`) on demand.


### Caveats and limitations


1. **Native baseline is a worst-case model.** Real Claude Code doesn't preload all prior knowledge — it uses built-in tools (Read, Grep, Glob) for on-demand retrieval. The native numbers represent the cost *if* all knowledge needed to be in the active context window simultaneously.

2. **MCP tool overhead is underestimated.** The benchmark counts ~15 tokens per `remember()` call. Real MCP tool calls include JSON request/response serialization that costs ~130-230 tokens per round-trip. This means RLM's actual token cost is higher than reported here.

3. **Token estimation uses a 4-char heuristic** (`len(text) // 4`), not a real tokenizer. This is directionally accurate but can be off by 20-30% depending on content type.

4. **Context rot retention percentages**: The native retention values for compaction tests use a FIFO eviction model (keep newest 5%). Claude's real `/compact` creates semantic summaries that preserve more information than raw FIFO would suggest.

5. **Cross-session native = 0%** is hardcoded by definition (new sessions start with no prior context window content). In practice, Claude Code's CLAUDE.md and project files provide some cross-session continuity.

6. **Scale ratios are theoretical bounds.** The O(N) vs O(1) scaling is mathematically correct for the retrieval model but the absolute ratios depend on corpus size — any bounded-result retrieval system shows similar scaling.

7. **Graph traversal RLM = 0 tokens** occurs because the JSON backend is used (GraphMemory DB not initialized), so `get_related()` raises FileNotFoundError and no content is returned. These results should be interpreted with caution.


### What IS valid


- **Directional claims are sound**: Offloading knowledge to persistent storage genuinely reduces context window pressure.

- **RLM-side measurements are real**: The actual tokens returned by `recall()`, `inspect()`, `filter_context()` are measured against the real MemCP implementation.

- **Context rot resistance is the strongest claim**: After `/compact`, native mode loses context window content while RLM's SQLite/disk storage is completely unaffected. This is architecturally guaranteed, not a benchmark artifact.

- **The working set efficiency** comparison is realistic: loading full documents into the context window vs. inspecting metadata + selective chunks is a genuine architectural difference.


**Total comparisons:** 27

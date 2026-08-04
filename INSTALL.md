# Installing MemCP (team adoption guide)

This is the supported install path for adopters who need to **pull bug fixes**
as they land. Install editable from this repo, pinned to a release tag —
`git pull` (plus a server restart) is the fix channel. Do **not** vendor a
copy of this code into another repo: vendored copies drift, and a re-vendor
can silently delete local additions.

## ⚠ The package name trap

The distribution name is **`claude-memory-mcp`**. There is a **foreign,
unrelated package named `memcp` on PyPI** — **`pip install memcp` installs
someone else's code** while appearing to succeed. Never install by that name;
install from this repo as below.

## Install

```bash
# 1. Clone and pin to the release tag
git clone <this-repo-url> ~/projects/memcp
cd ~/projects/memcp
git checkout v0.4.0   # pin to the newest release tag

# 2. Dedicated venv + editable install WITH the semantic/search extras
python3 -m venv ~/venvs/memcp
~/venvs/memcp/bin/pip install -e ".[dev,semantic,search]"
```

The `[semantic,search]` extras are **not optional in practice**: without them
the test suite fails (6 semantic-recall tests fail rather than skip) and
recall silently degrades to keyword-only scoring.

**Upgrading / picking up a fix:**

```bash
cd ~/projects/memcp && git fetch --tags && git checkout <new-tag>
# then restart your MCP client (or the memcp server process) —
# an editable install picks up code at process start, not live.
```

`memcp_ping` reports the running server's version; compare it with
`git -C ~/projects/memcp describe --tags` to confirm the restart took.

## Configuration

One environment variable decision matters up front:

- **`MEMCP_DATA_DIR`** — the store location, and the real **isolation
  boundary**. One data dir = one domain of knowledge (e.g. one for work, one
  for personal). Within a store, `project` labels are filters, not walls —
  reads default to the whole store (see `docs/design/default-scope.md`).
- **`MEMCP_PROJECT`** — optional pin for the project label the server stamps
  on writes. Without it, the project is auto-detected from the git repo /
  directory name at server start. Set it when the directory name is not the
  label you want.

Both go in the MCP server's `env` block in your client config.

## Hard constraints (learned in production — read before wiring into agents)

- **Do not call memcp tools from background subagents.** MCP calls from
  background subagents can hang indefinitely (client-side transport issue,
  observed repeatedly). Have subagents return findings in their final
  report; the parent session writes to memcp.
- **One server per session is the designed shape.** Concurrent servers on
  one store are safe for attribution (each pins its own identity at
  startup), but writes contend for the same SQLite store.
- **`memcp_consolidate` is destructive.** Use `memcp_consolidation_preview`
  and merge only its `mergeable` subsets; members listed as `unjustified`
  are similarity artifacts, not duplicates.
- **Snapshot `.bak` files are recovery points.** Never clean them up as part
  of routine maintenance.

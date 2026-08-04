# Sketch: SessionStart version-check hook (NOT registered)

Deliberately a sketch, not a registered hook — whether adopters want a
startup warning is a per-machine choice, and this repo must not install
automation into anyone's Claude config.

`memcp_ping` already reports the running server's version. The gap it
leaves: an editable install picks up code at **process start**, so after a
`git pull` the running server silently serves the old code until restarted.

A SessionStart hook can close that gap by comparing the repo's tag with the
installed package version:

```bash
#!/usr/bin/env bash
# memcp-version-check.sh — SessionStart hook (additionalContext on drift)
REPO="$HOME/projects/memcp"
VENV="$HOME/venvs/memcp"

repo_ver=$(git -C "$REPO" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')
run_ver=$("$VENV/bin/python" -c 'import memcp; print(memcp.__version__)' 2>/dev/null)

if [ -n "$repo_ver" ] && [ -n "$run_ver" ] && [ "$repo_ver" != "$run_ver" ]; then
  echo "memcp: repo is at v$repo_ver but the installed package reports v$run_ver — restart your MCP client to pick up the new code."
fi
```

Registration (adopter's choice, `.claude/settings.json`):

```json
{"hooks": {"SessionStart": [{"hooks": [{"type": "command",
  "command": "~/.claude/hooks/memcp-version-check.sh"}]}]}}
```

Caveats for whoever adopts this:

- It compares the *installed import* against the *repo tag* — that catches
  both "pulled but not restarted" and "checked out a tag but never
  reinstalled". It does not catch a server process older than the current
  interpreter state; `memcp_ping` inside the session is the check for that.
- Keep it fail-open: every path above already degrades to silence.

## Policy: shared skills repos point here, never vendor

A shared repo (e.g. a team skills repo) carries a **pointer + provisioning
doc only** — the clone/checkout/venv commands from INSTALL.md — never a
copy of this code. Observed failure with a vendored copy: it drifted
immediately, and the next re-vendor deleted a first-party script that lived
in the vendored tree (`memcp-gdrive-backup.sh`). Fixes must flow through
`git pull`, which vendoring breaks by construction.

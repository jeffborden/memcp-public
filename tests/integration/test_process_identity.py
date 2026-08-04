"""Cross-process identity attribution — the last-server-to-start-wins bug.

state.json's current_project/current_session are machine-global: every server
startup overwrites them, and write paths that read them at write time stamp
insights with whichever server registered last. Observed in production: writes
from two different Claude sessions both attributed to one session id.

These tests run two REAL server processes (subprocess, not threads — module
globals must not be shared) against one shared data dir, interleaved via
marker files:

    1. A starts, registers (project alpha, session A)
    2. B starts, registers (project beta, session B)  → state.json now says B
    3. A writes an insight with no explicit project/session

The insight written by A must be stamped with A's identity, not B's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Child processes mirror the real server lifecycle: _init_session() at startup
# (exactly what main() runs), then remember() — the same call the
# memcp_remember tool handler makes. Identity is captured from state.json
# immediately after own registration, which is race-free because the peer
# blocks on a marker file until this process has signalled.
_CHILD_SCRIPT = """
import json, sys, time
from pathlib import Path

role = sys.argv[1]           # "a" or "b"
rendezvous = Path(sys.argv[2])

def wait_for(marker: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    path = rendezvous / marker
    while not path.exists():
        if time.monotonic() > deadline:
            raise SystemExit(f"timeout waiting for {marker}")
        time.sleep(0.05)

def signal(marker: str) -> None:
    (rendezvous / marker).touch()

from memcp.server import _init_session
from memcp.core.memory import remember
from memcp.core.project import _get_state

if role == "b":
    wait_for("a_registered")

_init_session()
state = _get_state()
my_session = state.get("current_session", "")
my_project = state.get("current_project", "")

if role == "a":
    signal("a_registered")
    wait_for("b_registered")
else:
    signal("b_registered")

result = remember(f"insight written by process {role.upper()}")

print(json.dumps({
    "role": role,
    "my_session": my_session,
    "my_project": my_project,
    "stored_session": result["session"],
    "stored_project": result["project"],
}))
"""


def _spawn(role: str, project: str, data_dir: Path, rendezvous: Path) -> subprocess.Popen:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(data_dir.parent),
        "MEMCP_DATA_DIR": str(data_dir),
        "MEMCP_PROJECT": project,
        "MEMCP_TELEMETRY": "false",
        "MEMCP_SEMANTIC_RECALL": "false",
        "MEMCP_REINDEX_ON_SESSION_START": "false",
    }
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, role, str(rendezvous)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_both(data_dir: Path, rendezvous: Path) -> dict[str, dict]:
    rendezvous.mkdir(parents=True, exist_ok=True)
    proc_a = _spawn("a", "proj-alpha", data_dir, rendezvous)
    proc_b = _spawn("b", "proj-beta", data_dir, rendezvous)

    out_a, err_a = proc_a.communicate(timeout=60)
    out_b, err_b = proc_b.communicate(timeout=60)

    assert proc_a.returncode == 0, f"process A failed:\n{err_a}"
    assert proc_b.returncode == 0, f"process B failed:\n{err_b}"

    report_a = json.loads(out_a.strip().splitlines()[-1])
    report_b = json.loads(out_b.strip().splitlines()[-1])
    return {"a": report_a, "b": report_b}


class TestConcurrentServerIdentity:
    def test_write_after_peer_registration_keeps_own_identity(
        self, isolated_data_dir, tmp_path
    ):
        reports = _run_both(isolated_data_dir, tmp_path / "rendezvous")
        a, b = reports["a"], reports["b"]

        # Sanity: the two servers genuinely registered distinct identities,
        # so the assertions below are capable of failing.
        assert a["my_session"], "process A captured no session id"
        assert b["my_session"], "process B captured no session id"
        assert a["my_session"] != b["my_session"]
        assert a["my_project"] == "proj-alpha"
        assert b["my_project"] == "proj-beta"

        # The bug: A writes AFTER B registered, so a write path that reads
        # state.json at write time stamps A's insight with B's identity.
        assert a["stored_session"] == a["my_session"], (
            f"cross-attribution: A's insight stamped session "
            f"{a['stored_session']!r}, expected A's own {a['my_session']!r}"
        )
        assert a["stored_project"] == a["my_project"], (
            f"cross-attribution: A's insight stamped project "
            f"{a['stored_project']!r}, expected A's own {a['my_project']!r}"
        )

        # B registered last, so B is correct even on broken code — kept as a
        # control that correct attribution is representable at all.
        assert b["stored_session"] == b["my_session"]
        assert b["stored_project"] == b["my_project"]

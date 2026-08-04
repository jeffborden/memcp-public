"""Item 5 — P7: locked state.json read-modify-write (TOCTOU).

12. Concurrent register_session calls must not lose each other's updates. The
    read-modify-write in _set_state (and the sibling sessions.json RMW that
    register_session performs) ran read → mutate → write with no lock spanning
    the three steps, so two interleaved writers could each read the old file
    and clobber the other's update. A single LOCK_EX critical section
    (locked_update_json) closes the window.
"""

from __future__ import annotations

import threading

from memcp.core import project as project_mod
from memcp.core.fileutil import locked_read_json
from memcp.core.project import _get_state, _set_state, register_session


def test_set_state_concurrent_distinct_keys_all_survive() -> None:
    """The named helper: concurrent _set_state with distinct keys never loses
    a key (the project.py:403-408 lost-update bug)."""
    n_threads = 8
    writes_per_thread = 40
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()  # release together to force read/write interleaving
        for i in range(writes_per_thread):
            _set_state({f"k_{tid}_{i}": tid * 1000 + i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = _get_state()
    expected = {f"k_{t}_{i}" for t in range(n_threads) for i in range(writes_per_thread)}
    missing = expected - set(state)
    assert not missing, f"{len(missing)} _set_state updates were lost to TOCTOU"


def test_concurrent_register_session_all_survive() -> None:
    """End-to-end: concurrent register_session for distinct sessions — every
    session survives in sessions.json and state.json stays consistent."""
    n = 8
    barrier = threading.Barrier(n)
    session_ids = [f"sess-{i}" for i in range(n)]

    def worker(sid: str, proj: str) -> None:
        barrier.wait()
        register_session(sid, proj)

    threads = [
        threading.Thread(target=worker, args=(sid, f"proj-{i}"))
        for i, sid in enumerate(session_ids)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    config = project_mod.get_config()
    sessions = locked_read_json(config.sessions_path)
    registered = set(sessions.get("sessions", {}))
    missing = set(session_ids) - registered
    assert not missing, f"register_session lost sessions to TOCTOU: {missing}"

    # state.json is consistent: current_session is one of the registered ids and
    # the companion current_project field survived alongside it.
    state = _get_state()
    assert state.get("current_session") in session_ids
    assert state.get("current_project", "").startswith("proj-")

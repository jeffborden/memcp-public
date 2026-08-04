"""Setup matrix — one test per project-setup failure mode observed in the field.

The scheme must work regardless of how an adopter's machine is laid out.
Every case below corresponds to a real failure on the originating machine:

- git WORKTREE: a worktree's .git is a FILE, not a directory — a worktree
  registered itself as its own project via its dirname instead of
  resolving to anything stable. Decision here: a worktree IS its own
  directory name (predictable), but detection must not crash and must not
  fall through to "default".
- bare directory (no git): dirname is the project.
- blocked basename (`projects`, `src`, $USER...): must NOT become a ghost
  project.
- MEMCP_PROJECT unset vs set: env var always wins, blocklist bypassed.
- two concurrent servers: cross-attribution (covered in depth by
  test_process_identity.py; the smoke here is that two _init_session calls
  in one store never share a session id).
- background-subagent MCP hang: NOT reproducible in-process (it is a
  Claude-side transport issue, observed 3x on 07-31) — documented as a hard
  constraint in README/INSTALL instead; test_docs_carry_the_subagent_warning
  pins the documentation so the warning cannot silently vanish.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from memcp.core.project import (
    detect_project,
    generate_session_id,
    register_session,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


class TestProjectDetectionMatrix:
    def test_plain_git_repo_uses_repo_dirname(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MEMCP_PROJECT", raising=False)
        repo = tmp_path / "acme-api"
        (repo / "sub").mkdir(parents=True)
        _git("init", "-q", cwd=repo)
        assert detect_project(cwd=str(repo / "sub")) == "acme-api"

    def test_git_worktree_detects_worktree_dirname(self, tmp_path, monkeypatch):
        """A worktree's .git is a FILE. Detection must treat it as a repo
        root (its own name), never crash, never fall through to 'default'."""
        monkeypatch.delenv("MEMCP_PROJECT", raising=False)
        main = tmp_path / "acme-api"
        main.mkdir()
        _git("init", "-q", cwd=main)
        (main / "f.txt").write_text("x")
        _git("add", "f.txt", cwd=main)
        _git("commit", "-q", "-m", "init", cwd=main)
        worktree = tmp_path / "acme-api-feature"
        _git("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=main)

        assert (worktree / ".git").is_file(), "precondition: worktree .git is a file"
        assert detect_project(cwd=str(worktree)) == "acme-api-feature"

    def test_bare_directory_uses_dirname(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MEMCP_PROJECT", raising=False)
        bare = tmp_path / "scratch-notes"
        bare.mkdir()
        assert detect_project(cwd=str(bare)) == "scratch-notes"

    @pytest.mark.parametrize("blocked", ["projects", "src", "tmp", "Users", "documents"])
    def test_blocked_basename_never_becomes_project(self, tmp_path, monkeypatch, blocked):
        monkeypatch.delenv("MEMCP_PROJECT", raising=False)
        d = tmp_path / blocked
        d.mkdir()
        assert detect_project(cwd=str(d)) == "default"

    def test_username_basename_never_becomes_project(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MEMCP_PROJECT", raising=False)
        monkeypatch.setenv("USER", "jdoe")
        d = tmp_path / "jdoe"
        d.mkdir()
        assert detect_project(cwd=str(d)) == "default"

    def test_env_var_wins_everywhere(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMCP_PROJECT", "pinned-domain")
        repo = tmp_path / "acme-api"
        repo.mkdir()
        _git("init", "-q", cwd=repo)
        assert detect_project(cwd=str(repo)) == "pinned-domain"
        # ...even for a blocklisted dirname
        blocked = tmp_path / "projects"
        blocked.mkdir()
        assert detect_project(cwd=str(blocked)) == "pinned-domain"

    def test_env_var_unset_still_yields_a_project(self, tmp_path, monkeypatch):
        """MEMCP_PROJECT unset is a supported setup, not an error."""
        monkeypatch.delenv("MEMCP_PROJECT", raising=False)
        name = detect_project(cwd=str(tmp_path / "nonexistent-yet"))
        assert isinstance(name, str) and name


class TestConcurrentRegistration:
    def test_two_registrations_never_share_a_session_id(self, isolated_data_dir):
        sid_a = generate_session_id("proj-x")
        register_session(sid_a, "proj-x")
        sid_b = generate_session_id("proj-x")
        register_session(sid_b, "proj-x")
        assert sid_a != sid_b


class TestDocsCarryTheHardConstraints:
    def test_docs_carry_the_subagent_warning(self):
        """memcp MCP calls hang in background subagents (Claude-side, hit 3x
        on 2026-07-31). Not reproducible in-process, so the constraint lives
        in the docs — and this test pins it there."""
        install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")
        assert "background subagent" in install.lower()

    def test_install_doc_warns_about_foreign_pypi_package(self):
        """`pip install memcp` grabs a FOREIGN PyPI package — the dist name
        is claude-memory-mcp. The warning must exist and be bold."""
        install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")
        assert "**" in install and "pip install memcp" in install

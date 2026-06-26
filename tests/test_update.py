"""Phase 1: the update-check substrate (advisory only; never raises/blocks)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cohort.update import (
    SourceUnresolved,
    advisory_message,
    do_update_check,
    resolve_upstream,
    update_status,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {name}")


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repo (with a canonical/ dir so it reads as a source root) and a
    fresh clone whose ``origin`` points at it. Local remotes need no network."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@e.st")
    _git(up, "config", "user.name", "T")
    (up / "canonical").mkdir()
    _commit(up, "canonical/x.md", "x\n")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(up), str(src))
    _git(src, "config", "user.email", "t@e.st")
    _git(src, "config", "user.name", "T")
    return up, src


def test_update_status_detects_behind(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    _commit(up, "b.txt", "2\n")
    st = update_status(src, tmp_path / "home")
    assert st["available"] and st["behind"] == 2 and st["diverged"] is False


def test_update_status_up_to_date(tmp_path):
    _, src = _make_upstream_and_clone(tmp_path)
    st = update_status(src, tmp_path / "home")
    assert st["available"] and st["behind"] == 0


def test_update_status_diverged_suppresses_advisory(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "up.txt", "u\n")     # upstream advances
    _commit(src, "local.txt", "l\n")  # and we have our own commit
    st = update_status(src, tmp_path / "home")
    assert st["available"] and st["diverged"] is True
    assert advisory_message(st) is None  # diverged → never advise a plain /update


def test_update_status_no_remote_is_unavailable(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "f.txt", "1\n")
    st = update_status(repo, tmp_path / "home")
    assert st["available"] is False  # fetch fails (no origin) → no false alarm


def test_update_status_detached_head_is_unavailable(tmp_path):
    _, src = _make_upstream_and_clone(tmp_path)
    _git(src, "checkout", "-q", "--detach", "HEAD")
    st = update_status(src, tmp_path / "home")
    assert st["available"] is False  # detached → don't guess a behind-count


def test_advisory_message_wording():
    base = {"available": True, "diverged": False, "upstream": "origin/main"}
    assert "1 commit behind" in advisory_message({**base, "behind": 1})
    assert "2 commits behind" in advisory_message({**base, "behind": 2})
    assert advisory_message({**base, "behind": 0}) is None
    assert advisory_message({"available": False}) is None


def test_resolve_upstream_default_and_config(tmp_path):
    _, src = _make_upstream_and_clone(tmp_path)
    home = tmp_path / "home"
    # default: origin + the clone's detected default branch
    assert resolve_upstream(src, home) == ("origin", "main")
    # [update] config override
    (home / ".cohort").mkdir(parents=True)
    (home / ".cohort" / "cohort.toml").write_text(
        '[update]\nupstream_remote = "up"\nupstream_branch = "release"\n', encoding="utf-8"
    )
    assert resolve_upstream(src, home) == ("up", "release")


def test_do_update_check_advisory_then_throttled_then_next_day(tmp_path, monkeypatch):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    home = tmp_path / "home"
    monkeypatch.setenv("COHORT_SOURCE", str(src))
    day1 = datetime(2026, 6, 26, tzinfo=timezone.utc)
    msg = do_update_check(home, now=day1)
    assert msg and "1 commit behind" in msg
    # same UTC day → throttled, no repeat nag
    assert do_update_check(home, now=day1) is None
    # next day → re-checks and re-advises while still behind
    assert "behind" in (do_update_check(home, now=day1 + timedelta(days=1)) or "")


def test_do_update_check_creates_missing_state_dir(tmp_path, monkeypatch):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    home = tmp_path / "home"  # no ~/.cohort/state pre-existing
    monkeypatch.setenv("COHORT_SOURCE", str(src))
    do_update_check(home)
    assert (home / ".cohort" / "state" / ".update-checked").exists()


def test_do_update_check_unresolved_source_returns_none(tmp_path, monkeypatch):
    import cohort.update as u

    def boom(*a, **k):
        raise SourceUnresolved("no clone")

    monkeypatch.setattr(u, "resolve_source", boom)
    assert do_update_check(tmp_path / "home") is None  # site-packages install → silent


def test_update_check_command_always_exits_0(tmp_path):
    """The hook target exits 0 even with a local (offline) upstream."""
    _, src = _make_upstream_and_clone(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["COHORT_SOURCE"] = str(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cohort", "update-check"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0


def test_update_check_hook_compiles_into_session_start():
    """The new hook reaches every IDE's session_start fragment (no golden locks
    the Claude path, so assert it directly from the real canonical)."""
    from cohort.compile import compile_ide

    for ide in ("claude", "codex", "cursor"):
        blob = b"".join(sf.content for sf in compile_ide(REPO_ROOT, ide).staged)
        assert b"cohort update-check" in blob, ide

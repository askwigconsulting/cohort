"""`cohort my-office sync` (#66) — back the personal layer (~/.cohort/my) with a Git remote.

The personal layer is otherwise a plain directory, so personal agents/skills don't
follow the user across machines. `my-office sync` makes it a Git repo, reconciles with
a configured remote (fast-forward only), and pushes. The load-bearing behaviour is the
*second machine*: a fresh ~/.cohort/my must adopt the shared history, not collide with it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cohort.install_model import CohortPaths
from cohort.myoffice import MySyncError, _redact_url, do_my_sync, my_remote


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _bare_remote(tmp_path) -> Path:
    """A bare git repo usable as a file-path remote — no network needed."""
    r = tmp_path / "remote.git"
    # -b main so the bare repo's HEAD tracks the branch sync pushes (else a
    # verification clone would find HEAD dangling and check out nothing).
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(r)], check=True)
    return r


def _my(home: Path) -> Path:
    return CohortPaths.for_global(home).my


def _write_personal(home: Path, name: str, text: str = "personal advisor\n") -> Path:
    """Drop a personal agent into ~/.cohort/my as a user would."""
    d = _my(home) / "canonical" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _clone_files(remote: Path, tmp_path: Path, tag: str) -> set[str]:
    """The tracked file set on the remote's default branch (via a throwaway clone)."""
    dest = tmp_path / f"verify-{tag}"
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], check=True)
    return {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts}


# === guard rails =============================================================


def test_my_remote_is_none_without_a_repo(home):
    assert my_remote(home) is None


def test_dry_run_reports_plan_and_creates_nothing(home):
    report = do_my_sync(home, remote="git@example.com:me/office.git", dry_run=True)
    assert report["dry_run"] is True
    assert report["remote"] == "git@example.com:me/office.git"
    assert "fetch + ff-pull" in report["plan"]
    # dry-run must not initialise the repo
    assert not (_my(home) / ".git").exists()


def test_sync_without_a_remote_is_refused(home):
    with pytest.raises(MySyncError, match="no sync remote configured"):
        do_my_sync(home)


def test_unreachable_remote_is_fatal_not_a_false_success(home, tmp_path):
    # A failed fetch must raise — never fall through to a local commit that would
    # orphan a fresh machine's branch and wedge it into "diverged" forever.
    missing = tmp_path / "does-not-exist.git"
    _write_personal(home, "solo")
    with pytest.raises(MySyncError, match="could not reach sync remote"):
        do_my_sync(home, remote=str(missing))


def test_unborn_file_conflict_reports_a_distinct_message(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "clash", "office copy\n")
    do_my_sync(home, remote=str(remote))

    # A fresh machine whose local file collides (same path, different content)
    # with one already in the synced office can't fast-forward-adopt it.
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    _write_personal(home_b, "clash", "my divergent copy\n")
    with pytest.raises(MySyncError, match="conflicts with one already in your synced"):
        do_my_sync(home_b, remote=str(remote))


# === first machine ===========================================================


def test_first_sync_configures_remote_and_pushes_personal_files(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "trading-compliance")

    report = do_my_sync(home, remote=str(remote))

    assert my_remote(home) == str(remote)
    assert report["pushed"] is True
    pushed = _clone_files(remote, tmp_path, "A")
    assert "canonical/agents/trading-compliance.md" in pushed


# === second machine (the bug this feature exists to serve) ===================


def test_second_machine_adopts_shared_history_and_keeps_its_own_files(home, tmp_path):
    remote = _bare_remote(tmp_path)

    # Machine A seeds the remote.
    _write_personal(home, "agent-from-a")
    do_my_sync(home, remote=str(remote))

    # Machine B: a *fresh* ~/.cohort/my with its own local personal file.
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    _write_personal(home_b, "agent-from-b")

    report = do_my_sync(home_b, remote=str(remote))

    # B pulled A's history AND kept its own file — both are now on the remote.
    assert report["pulled"] is True
    assert report["pushed"] is True
    both = _clone_files(remote, tmp_path, "B")
    assert "canonical/agents/agent-from-a.md" in both  # adopted from A
    assert "canonical/agents/agent-from-b.md" in both  # B's own, preserved


def test_second_sync_no_local_changes_is_up_to_date(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "solo")
    do_my_sync(home, remote=str(remote))

    # Re-sync with nothing new: reconciles cleanly, reports no pull.
    report = do_my_sync(home)
    assert report["pulled"] is False
    assert report["pushed"] is True


# === genuine divergence is still refused =====================================


def test_report_redacts_an_embedded_url_password(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "solo")
    do_my_sync(home, remote=str(remote))
    # Re-point at an https URL carrying a token; the report must not echo it.
    report = do_my_sync(home, remote="https://user:ghp_secrettoken@example.com/o.git",
                        dry_run=True)
    assert "ghp_secrettoken" not in report["remote"]
    assert "user:***@" in report["remote"]


def test_redact_url_leaves_scp_and_plain_urls_untouched():
    assert _redact_url("git@example.com:me/office.git") == "git@example.com:me/office.git"
    assert _redact_url("ssh://git@host/o.git") == "ssh://git@host/o.git"
    assert _redact_url("/tmp/local/remote.git") == "/tmp/local/remote.git"
    assert _redact_url(None) is None


def test_default_gitignore_excludes_secret_files_from_the_sync(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "solo")
    # A user drops a credential file into the personal layer.
    (_my(home) / ".env").write_text("API_KEY=sk-live-do-not-sync\n", encoding="utf-8")
    do_my_sync(home, remote=str(remote))
    pushed = _clone_files(remote, tmp_path, "secrets")
    assert ".env" not in pushed
    assert ".gitignore" in pushed  # the exclusion list itself is synced


def test_diverged_history_is_refused_for_the_user_to_reconcile(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "base")
    do_my_sync(home, remote=str(remote))

    # A second machine forks the history: it commits locally, then someone else
    # advances the remote so the two can no longer fast-forward.
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    _write_personal(home_b, "b-only")
    do_my_sync(home_b, remote=str(remote))  # B now shares history

    # Machine A adds a commit and pushes; machine B adds its own, unaware.
    _write_personal(home, "a-second")
    do_my_sync(home)  # advances remote
    _write_personal(home_b, "b-second")
    my_b = _my(home_b)
    subprocess.run(["git", "-C", str(my_b), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(my_b), "commit", "-q", "-m", "local"], check=True)

    with pytest.raises(MySyncError, match="diverged"):
        do_my_sync(home_b)

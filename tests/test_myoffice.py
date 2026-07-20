"""`cohort my-office sync` (#66) — back the personal layer (~/.cohort/my) with a Git remote.

The personal layer is otherwise a plain directory, so personal agents/skills don't
follow the user across machines. `my-office sync` makes it a Git repo, reconciles with
a configured remote (fast-forward only), and pushes. The load-bearing behaviour is the
*second machine*: a fresh ~/.cohort/my must adopt the shared history, not collide with it.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort import quarantine
from cohort.install import do_install
from cohort.install_model import CohortPaths
from cohort.myoffice import (
    MySyncError,
    _record_pulled_gated,
    _recompile_if_installed,
    _redact_url,
    do_my_sync,
    my_remote,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_HOOK = (
    "---\nname: {name}\nkind: hook\nscope: global\n"
    "description: A pulled hook.\ntargets: [claude]\n"
    "event: session_start\naction: cohort {name}\n---\nHook body.\n"
)


def _write_hook(home: Path, name: str) -> Path:
    d = CohortPaths.for_global(home).my / "canonical" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(_HOOK.format(name=name), encoding="utf-8")
    return p


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


_VALID_AGENT = (
    "---\nname: {name}\nkind: agent\nscope: global\n"
    "description: A pulled personal advisor.\ntargets: [all]\n"
    "department: MyDesk\ntopology: specialist\nadvisory: true\ntools: [read]\n"
    "display_name: {name}\n---\nPersonal advisor body.\n"
)


def _write_valid_personal_agent(home: Path, name: str) -> Path:
    """Drop a *schema-valid* personal agent — needed by any test whose sync
    actually recompiles (an installed manifest is present), since a plain
    placeholder body fails compile validation the moment it is rendered."""
    d = _my(home) / "canonical" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(_VALID_AGENT.format(name=name), encoding="utf-8")
    return p


def _clone_files(remote: Path, tmp_path: Path, tag: str) -> set[str]:
    """The tracked file set on the remote's default branch (via a throwaway clone)."""
    dest = tmp_path / f"verify-{tag}"
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], check=True)
    # as_posix() so assertions use "/" separators on Windows too (the files land
    # fine there; only str(Path) would spell them with backslashes).
    return {p.relative_to(dest).as_posix()
            for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts}


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


# === quarantine of pulled hooks/memories (#107) ==============================


def _pending_names(home: Path) -> set[str]:
    return {a.name for a in quarantine.load_pending(CohortPaths.for_global(home).state)}


def test_fresh_machine_quarantines_every_pulled_hook(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_hook(home, "a-hook")
    do_my_sync(home, remote=str(remote))  # machine A seeds a hook

    # Machine B: fresh adopt of the shared history → the pulled hook is unreviewed.
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    report = do_my_sync(home_b, remote=str(remote))
    assert report["pulled"] is True
    assert report["quarantined"] == ["hook a-hook"]
    assert _pending_names(home_b) == {"a-hook"}


def test_incremental_pull_quarantines_new_hook_but_not_local_authoring(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_hook(home, "base")
    do_my_sync(home, remote=str(remote))
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    do_my_sync(home_b, remote=str(remote))  # B shares history
    quarantine.approve(CohortPaths.for_global(home_b).state, approve_all=True)  # clear the adopt

    # A pushes a new hook; B authors its own hook, then syncs.
    _write_hook(home, "from-a")
    do_my_sync(home)
    _write_hook(home_b, "from-b-local")
    report = do_my_sync(home_b)

    # Only the pulled hook is quarantined; B's own authored hook is not.
    assert report["quarantined"] == ["hook from-a"]
    assert _pending_names(home_b) == {"from-a"}


def test_diff_failure_fails_closed_quarantining_every_gated(home, tmp_path):
    # If the pull-delta diff can't be computed (a git hiccup / bad ref), the recorder
    # must fall back to quarantining EVERY gated artifact present, never activate one.
    remote = _bare_remote(tmp_path)
    _write_hook(home, "a")
    _write_hook(home, "b")
    do_my_sync(home, remote=str(remote))  # makes ~/.cohort/my a real repo with commits
    my = CohortPaths.for_global(home).my
    state = CohortPaths.for_global(home).state
    quarantine.approve(state, approve_all=True)  # start clean
    # A bogus `before` SHA makes `git diff before..after` exit non-zero → fallback.
    newly = _record_pulled_gated(my, state, before="0" * 40, after="HEAD", unborn=False)
    assert {a.name for a in newly} == {"a", "b"}


def _run_cli(*args, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], capture_output=True, text=True, env=env
    )


def test_review_and_approve_cli_roundtrip(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_hook(home, "seed-hook")
    do_my_sync(home, remote=str(remote))
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    do_my_sync(home_b, remote=str(remote))  # pulls + quarantines seed-hook

    review = _run_cli("my-office", "review", "--json", home=home_b)
    assert review.returncode == 0, review.stderr
    assert [a["name"] for a in _json.loads(review.stdout)["pending"]] == ["seed-hook"]

    approve = _run_cli("my-office", "approve", "seed-hook", "--json", home=home_b)
    assert approve.returncode == 0, approve.stderr
    assert _json.loads(approve.stdout)["approved"] == ["seed-hook"]
    assert _pending_names(home_b) == set()  # cleared durably


def test_approve_requires_a_name_or_all(home):
    result = _run_cli("my-office", "approve", home=home)
    assert result.returncode == 1
    assert "name" in result.stderr.lower()


def test_pulled_agent_is_not_quarantined(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "pulled-agent")  # an agent, not a gated kind
    do_my_sync(home, remote=str(remote))
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    report = do_my_sync(home_b, remote=str(remote))
    assert report["quarantined"] == []
    assert _pending_names(home_b) == set()


def test_pulled_hook_misfiled_in_agents_dir_is_still_quarantined(home, tmp_path):
    # The bypass the review caught: a hook committed under canonical/agents/ still
    # renders as a hook, so sync must gate it by frontmatter kind, not directory.
    remote = _bare_remote(tmp_path)
    agents = CohortPaths.for_global(home).my / "canonical" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "evil.md").write_text(
        "---\nname: evil\nkind: hook\nscope: global\ndescription: rce.\n"
        "targets: [claude]\nevent: session_start\naction: cohort rce\n---\nbody\n",
        encoding="utf-8",
    )
    do_my_sync(home, remote=str(remote))
    home_b = tmp_path / "home-b"
    home_b.mkdir()
    report = do_my_sync(home_b, remote=str(remote))
    assert report["quarantined"] == ["hook evil"]
    assert _pending_names(home_b) == {"evil"}


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


# === GS2: recompile targets exactly the installed IDE set, never a hardcoded
# ["claude"] (a Codex-only machine must recompile Codex — never place Claude
# artifacts nobody asked for; a Claude+Cursor machine must recompile both) =====


def test_recompile_if_installed_recompiles_codex_only_and_never_places_claude(home):
    # A Codex-only install: Claude was never selected.
    do_install(home=home, selection=["codex"], mode="copy", force=False,
               source=REPO_ROOT, dry_run=False)

    recompiled = _recompile_if_installed(home)

    assert recompiled == ["codex"]
    assert (home / ".codex" / "agents" / "chief-of-staff.toml").exists()
    # The bug this guards: a Codex-only manifest must never place a Claude
    # artifact the user never asked for.
    assert not (home / ".claude").exists()


def test_recompile_if_installed_recompiles_every_installed_ide(home):
    # A Claude+Cursor install: both must be recompiled, not just Claude.
    do_install(home=home, selection=["claude", "cursor"], mode="copy", force=False,
               source=REPO_ROOT, dry_run=False)

    recompiled = _recompile_if_installed(home)

    assert recompiled == ["claude", "cursor"]
    assert (home / ".claude" / "agents" / "chief-of-staff.md").exists()
    assert (home / ".cursor" / "agents" / "chief-of-staff.md").exists()
    assert not (home / ".codex").exists()  # never installed, never placed


def test_recompile_if_installed_is_a_noop_without_an_install(home):
    # A fresh machine (no manifest yet) must not try to recompile anything —
    # in particular it must never fall through to a hardcoded Claude install.
    assert _recompile_if_installed(home) == []
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()
    assert not (home / ".cursor").exists()


def test_sync_on_a_codex_only_machine_recompiles_codex_not_claude(home, tmp_path):
    # End-to-end through `cohort my-office sync`: the "recompiled" field in the
    # sync report must reflect the actually-installed IDE, and the sync must not
    # place a Claude artifact on a Codex-only machine.
    remote = _bare_remote(tmp_path)
    do_install(home=home, selection=["codex"], mode="copy", force=False,
               source=REPO_ROOT, dry_run=False)
    _write_valid_personal_agent(home, "solo")

    report = do_my_sync(home, remote=str(remote))

    assert report["recompiled"] == ["codex"]
    assert (home / ".codex" / "agents" / "chief-of-staff.toml").exists()
    # The pulled personal artifact was actually placed for the installed IDE...
    assert (home / ".codex" / "agents" / "solo.toml").exists()
    # ...and never for Claude, which was never installed on this machine.
    assert not (home / ".claude").exists()


def test_sync_on_a_fresh_machine_does_not_attempt_a_recompile(home, tmp_path):
    remote = _bare_remote(tmp_path)
    _write_personal(home, "solo")

    report = do_my_sync(home, remote=str(remote))

    assert report["recompiled"] == []
    assert not (home / ".claude").exists()


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

"""Tests for the external-CLI worktree doer (cohort.engines.cli_doer).

The ``codex`` subprocess is mocked so no real CLI runs — but the mock writes into the
real worktree the doer created, and the real ``git`` diff-capture then runs, so the
worktree lifecycle, the confinement flags, the gates, and the diff/footprint reporting
are all exercised.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from cohort.engines import cli_doer, gates


def _init_git_repo(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.co", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=root, check=True, capture_output=True,
    )


def _worktree_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout
    return sum(1 for line in out.splitlines() if line.startswith("worktree "))


def _fake_codex(edit: dict[str, str], returncode: int = 0):
    """A subprocess.run stand-in: for the codex call, write ``edit`` into the worktree
    (named by ``-C``); for git calls, run the real git."""
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[:2] == ["codex", "exec"]:
            wt = Path(cmd[cmd.index("-C") + 1])
            for rel, content in edit.items():
                (wt / rel).parent.mkdir(parents=True, exist_ok=True)
                (wt / rel).write_text(content, encoding="utf-8")
            return types.SimpleNamespace(returncode=returncode, stdout="edited", stderr="")
        return real_run(cmd, **kwargs)

    return run


@pytest.fixture
def _codex_installed(monkeypatch):
    monkeypatch.setattr("cohort.engines.cli_doer.shutil.which", lambda _n: "/usr/bin/codex")


def test_codex_doer_edits_the_worktree_leaving_source_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _codex_installed
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    monkeypatch.setattr(
        "cohort.engines.cli_doer.subprocess.run",
        _fake_codex({"src/app.py": "value = 2\n"}),
    )

    result = cli_doer.run_doer("gpt", "bump the value", repo_root=tmp_path)

    assert result.changed_files == ["src/app.py"]
    assert "value = 2" in result.diff
    assert (result.worktree / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"  # untouched
    assert _worktree_count(tmp_path) == 2  # left for review


def test_codex_doer_command_is_sandbox_confined_to_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _codex_installed
) -> None:
    _init_git_repo(tmp_path, {"a.py": "x=1\n"})
    seen = {}
    real_run = subprocess.run

    def capture(cmd, **kwargs):
        if cmd[:2] == ["codex", "exec"]:
            seen["cmd"] = cmd
            wt = Path(cmd[cmd.index("-C") + 1])
            (wt / "a.py").write_text("x=2\n", encoding="utf-8")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", capture)
    result = cli_doer.run_doer("gpt", "t", repo_root=tmp_path, model="gpt-5.6-sol")

    cmd = seen["cmd"]
    assert "--sandbox" in cmd and cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("-C") + 1] == str(result.worktree)  # confined to the worktree
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"


def test_codex_doer_reports_footprint_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _codex_installed
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "1\n"})
    monkeypatch.setattr(
        "cohort.engines.cli_doer.subprocess.run",
        _fake_codex({"src/app.py": "2\n", "other/sneaky.py": "3\n"}),
    )
    result = cli_doer.run_doer("gpt", "t", repo_root=tmp_path, footprint=["src"])
    assert any("other/sneaky.py" in v for v in result.footprint_violations)


def test_egress_optout_blocks_before_spawning_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _codex_installed
) -> None:
    _init_git_repo(tmp_path, {"a.py": "1\n"})
    spawned = {"called": False}
    real_run = subprocess.run  # capture before patching (the patch is module-global)

    def must_not_spawn(cmd, **kwargs):
        if cmd[:2] == ["codex", "exec"]:
            spawned["called"] = True
        return real_run(cmd, **kwargs)  # git falls through to the real run

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", must_not_spawn)
    with pytest.raises(gates.EgressBlockedError):
        cli_doer.run_doer(
            "gpt", "t", repo_root=tmp_path,
            project_context_text="## Egress\n\ncohort:egress=deny\n",
        )
    assert spawned["called"] is False
    assert _worktree_count(tmp_path) == 1  # no worktree created


def test_secret_in_task_is_refused(tmp_path: Path, _codex_installed) -> None:
    _init_git_repo(tmp_path, {"a.py": "1\n"})
    with pytest.raises(gates.SecretFoundError):
        cli_doer.run_doer(
            "gpt",
            "use AWS_SECRET_ACCESS_KEY = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY here",
            repo_root=tmp_path,
        )


def test_grok_falls_back_to_agentic_propose_when_bwrap_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # grok now has a sandboxed CLI doer; only when bwrap is missing does it refuse and
    # point at the gated agentic-propose path (never runs grok unconfined).
    monkeypatch.setattr(cli_doer.shutil, "which",
                        lambda name: "/usr/bin/grok" if name == "grok" else None)
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    with pytest.raises(cli_doer.DoerUnavailableError, match="propose grok --agentic"):
        cli_doer.run_doer("grok", "t", repo_root=tmp_path)


def test_missing_codex_cli_raises_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"a.py": "1\n"})
    monkeypatch.setattr("cohort.engines.cli_doer.shutil.which", lambda _n: None)
    with pytest.raises(cli_doer.DoerUnavailableError, match="not installed"):
        cli_doer.run_doer("gpt", "t", repo_root=tmp_path)


# === grok doer: bubblewrap-imposed confinement ==============================


def test_grok_sandbox_argv_confines_to_the_worktree(tmp_path):
    """The sandbox argv makes the worktree the only writable bind, keeps system dirs
    read-only, isolates namespaces while keeping the network, and never puts a key on
    argv or bind-mounts the real home read-write."""
    wt = tmp_path / "wt"
    argv = cli_doer._grok_sandbox_argv(wt, tmp_path / "home", ["grok", "-p", "x"])
    joined = " ".join(argv)
    assert f"--bind {wt} {wt}" in joined            # worktree is writable
    assert "--ro-bind /usr /usr" in joined          # system is read-only
    assert "--unshare-all" in joined and "--share-net" in joined  # isolated but networked
    assert f"--bind {Path.home()} " not in joined   # the real home is never writable
    assert "GROK_API_KEY" not in joined             # the key rides the env, not argv


@pytest.mark.skipif(cli_doer._bwrap() is None, reason="bwrap not installed")
def test_grok_sandbox_actually_blocks_writes_outside_the_worktree(tmp_path):
    """Live kernel-level check: a command in the sandbox can write the worktree but not
    /etc or the real home."""
    wt = tmp_path / "wt"
    wt.mkdir()
    inner = [
        "sh", "-c",
        f"touch {wt}/inside.txt; touch /etc/COHORT_HACK 2>/dev/null; "
        f"touch {Path.home()}/COHORT_HACK 2>/dev/null; true",
    ]
    argv = cli_doer._grok_sandbox_argv(wt, tmp_path / "home", inner)
    subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert (wt / "inside.txt").exists()               # inside the worktree: allowed
    assert not Path("/etc/COHORT_HACK").exists()      # system dir: blocked
    assert not (Path.home() / "COHORT_HACK").exists() # real home: blocked


def test_grok_doer_refuses_without_bwrap(tmp_path, monkeypatch):
    """With grok present but bwrap missing, the doer refuses rather than run grok
    unconfined."""
    monkeypatch.setattr(cli_doer.shutil, "which",
                        lambda name: "/usr/bin/grok" if name == "grok" else None)
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    with pytest.raises(cli_doer.DoerUnavailableError, match="bubblewrap"):
        cli_doer.run_grok_in_worktree(tmp_path, "do a thing")


def test_run_doer_routes_grok_to_the_grok_doer(monkeypatch):
    """run_doer dispatches 'grok'/'xai' to the grok doer instead of refusing."""
    called = {}

    def _fake(task, **kw):
        called["task"] = task
        return "ok"

    monkeypatch.setattr(cli_doer, "run_grok_doer", _fake)
    assert cli_doer.run_doer("grok", "t", repo_root=Path(".")) == "ok"
    assert cli_doer.run_doer("xai", "t", repo_root=Path(".")) == "ok"
    assert called["task"] == "t"

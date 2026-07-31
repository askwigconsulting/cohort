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

from cohort.engines import cli_doer, gates, patch_proposal


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


def test_egress_optout_derived_from_repo_when_kwarg_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _codex_installed
) -> None:
    """#229: a caller that omits project_context_text can't ship an opted-out repo — the
    codex doer reads the repo's .cohort/project_context.md and the deny marker refuses
    BEFORE the CLI is spawned."""
    _init_git_repo(
        tmp_path,
        {
            "a.py": "1\n",
            ".cohort/project_context.md": "## Egress\n\ncohort:egress=deny\n",
        },
    )
    spawned = {"called": False}
    real_run = subprocess.run

    def must_not_spawn(cmd, **kwargs):
        if cmd[:2] == ["codex", "exec"]:
            spawned["called"] = True
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", must_not_spawn)
    with pytest.raises(gates.EgressBlockedError):
        cli_doer.run_doer("gpt", "t", repo_root=tmp_path)  # no project_context_text kwarg

    assert spawned["called"] is False
    assert _worktree_count(tmp_path) == 1  # refused before any worktree


def test_grok_egress_optout_derived_from_repo_when_kwarg_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _grok_installed
) -> None:
    """#229: the grok doer's shared gate also derives the egress context from repo state
    when the kwarg is omitted — an opted-out repo refuses before grok runs."""
    _init_git_repo(
        tmp_path,
        {
            "a.py": "1\n",
            ".cohort/project_context.md": "## Egress\n\ncohort:egress=deny\n",
        },
    )
    calls, spy = _spy_grok()
    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", spy)

    with pytest.raises(gates.EgressBlockedError):
        cli_doer.run_grok_review("review", repo_root=tmp_path)  # no kwarg

    assert calls["n"] == 0
    assert _worktree_count(tmp_path) == 1


def test_explicit_project_context_kwarg_overrides_repo_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _codex_installed
) -> None:
    """A non-empty project_context_text kwarg is kept verbatim (the override still works):
    an allow-marked context passes even if it differs from the on-disk file."""
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    monkeypatch.setattr(
        "cohort.engines.cli_doer.subprocess.run",
        _fake_codex({"src/app.py": "value = 2\n"}),
    )
    result = cli_doer.run_doer(
        "gpt", "bump", repo_root=tmp_path,
        project_context_text="## Egress\n\ncohort:egress=allow\n",
    )
    assert result.returncode == 0


def test_default_wire_cap_is_50mb(tmp_path: Path) -> None:
    """#233: the default total-wire-byte cap is 50MB — clears normal-to-large source
    repos while still catching a runaway data/binary blob."""
    assert cli_doer._DEFAULT_MAX_WIRE_BYTES == 50_000_000


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


def test_grok_sandbox_argv_ro_binds_etc_and_never_writable_home(tmp_path):
    """The /etc subset is read-only bound (never the whole of /etc), and NO writable
    (--bind) target is the real home or an ancestor of it — the only persistent writable
    path is the worktree. Pure argv check, runs without bwrap installed."""
    wt = tmp_path / "wt"
    argv = cli_doer._grok_sandbox_argv(wt, tmp_path / "home", ["grok", "-p", "x"])
    joined = " ".join(argv)
    assert "--ro-bind /etc /etc" not in joined  # the whole-/etc bind is gone (#227)

    # Every writable bind must be the worktree, and never the real home or an ancestor.
    writable_targets = [
        argv[i + 2] for i, tok in enumerate(argv) if tok == "--bind"
    ]
    assert writable_targets == [str(wt)]  # the worktree is the sole writable bind
    home = Path.home()
    for dst in writable_targets:
        assert Path(dst) != home                       # not the real home
        assert not home.is_relative_to(Path(dst))      # not an ancestor of the real home


def test_grok_sandbox_argv_narrows_etc_to_tls_and_resolver(tmp_path):
    """#227: /etc is no longer bound wholesale; only the TLS + name-resolution subset is
    bound read-only, each path only when it exists on the host. Bare `/etc /etc` is gone,
    /etc/ssl (present on essentially every Linux host) is bound, and every /etc path bound
    is from the allowed subset."""
    wt = tmp_path / "wt"
    argv = cli_doer._grok_sandbox_argv(wt, tmp_path / "home", ["grok", "-p", "x"])
    joined = " ".join(argv)

    assert "--ro-bind /etc /etc" not in joined  # the whole-/etc bind is removed
    assert "--ro-bind /usr /usr" in joined       # /usr still read-only

    allowed = {
        "/etc/ssl", "/etc/resolv.conf", "/etc/hosts",
        "/etc/nsswitch.conf", "/etc/ca-certificates", "/etc/pki",
    }
    etc_binds = [
        argv[i + 1]
        for i, tok in enumerate(argv)
        if tok == "--ro-bind" and argv[i + 1].startswith("/etc")
    ]
    assert etc_binds, "expected at least the cert store to be bound"
    assert all(p in allowed for p in etc_binds)  # only the TLS/resolver subset
    if Path("/etc/ssl").exists():                # the cert store, on any real Linux host
        assert "--ro-bind /etc/ssl /etc/ssl" in joined


def test_grok_sandbox_argv_has_new_session_for_tiocsti(tmp_path):
    """#225: the jail runs in its own session (--new-session) so a TTY can't be used to
    inject keystrokes (TIOCSTI) back into Cohort, alongside the existing isolation flags."""
    argv = cli_doer._grok_sandbox_argv(
        tmp_path / "wt", tmp_path / "home", ["grok", "-p", "x"]
    )
    assert "--new-session" in argv
    assert "--unshare-all" in argv and "--die-with-parent" in argv


def test_grok_env_is_scrubbed_of_host_secrets(monkeypatch, tmp_path):
    """The grok child gets PATH/HOME and only the grok allow-list vars; a planted host
    secret (FAKE_SECRET, AWS_SECRET_ACCESS_KEY) is dropped. Pure logic, no bwrap."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("GROK_API_KEY", "xai-secret-value")
    monkeypatch.setenv("FAKE_SECRET", "leak-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-leak-me")

    env = cli_doer._scrubbed_env(
        home=tmp_path / "home", passthrough=cli_doer._GROK_ENV_PASSTHROUGH
    )

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == str(tmp_path / "home")
    assert env["GROK_API_KEY"] == "xai-secret-value"  # the key rides the env
    assert "FAKE_SECRET" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_codex_env_is_scrubbed_of_host_secrets(monkeypatch, tmp_path):
    """The codex child gets PATH/HOME and only the codex allow-list vars; a planted host
    secret is dropped."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-codex-value")
    monkeypatch.setenv("FAKE_SECRET", "leak-me")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_ride_along")

    env = cli_doer._scrubbed_env(
        home=tmp_path / "home", passthrough=cli_doer._CODEX_ENV_PASSTHROUGH
    )

    assert env["OPENAI_API_KEY"] == "sk-codex-value"
    assert "FAKE_SECRET" not in env
    assert "GITHUB_TOKEN" not in env


def test_run_grok_in_worktree_hands_grok_a_scrubbed_env_not_the_host(
    tmp_path, monkeypatch
):
    """run_grok_in_worktree passes subprocess.run an explicit env= that carries
    GROK_API_KEY but drops a planted host secret, and never puts the key on argv. Runs
    without bwrap installed (both grok and bwrap are monkeypatched)."""
    monkeypatch.setattr(cli_doer.shutil, "which",
                        lambda name: "/usr/bin/grok" if name == "grok" else None)
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    monkeypatch.setenv("GROK_API_KEY", "xai-secret-value")
    monkeypatch.setenv("FAKE_SECRET", "leak-me")

    seen = {}

    def capture(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", capture)
    cli_doer.run_grok_in_worktree(tmp_path / "wt", "do a thing")

    env = seen["env"]
    assert env is not None                              # explicit env, not inherited
    assert env["GROK_API_KEY"] == "xai-secret-value"    # key rides the (scrubbed) env
    assert "FAKE_SECRET" not in env                     # host secret dropped
    assert "xai-secret-value" not in " ".join(seen["cmd"])  # and never on argv


def test_run_grok_in_worktree_is_non_interactive_and_own_session(tmp_path, monkeypatch):
    """#225: the grok subprocess gets stdin=DEVNULL (no TTY to inject keystrokes into) and
    start_new_session=True (a timeout kill reaps grandchildren)."""
    monkeypatch.setattr(cli_doer.shutil, "which",
                        lambda name: "/usr/bin/grok" if name == "grok" else None)
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    seen = {}

    def capture(cmd, **kwargs):
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", capture)
    cli_doer.run_grok_in_worktree(tmp_path / "wt", "do a thing")

    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["start_new_session"] is True


def test_run_codex_in_worktree_is_non_interactive_and_own_session(tmp_path, monkeypatch):
    """#225: the codex subprocess gets stdin=DEVNULL and start_new_session=True too."""
    monkeypatch.setattr("cohort.engines.cli_doer.shutil.which", lambda _n: "/usr/bin/codex")
    seen = {}

    def capture(cmd, **kwargs):
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", capture)
    cli_doer.run_codex_in_worktree(tmp_path / "wt", "do a thing")

    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["start_new_session"] is True


def test_worktree_secret_scan_refuses_committed_secret_before_dispatch(
    tmp_path, monkeypatch, _codex_installed
):
    """A committed file carrying a credential is refused (SecretFoundError) BEFORE the
    vendor CLI is spawned — the CLI would otherwise read and egress it — and the
    throwaway worktree is cleaned up."""
    _init_git_repo(tmp_path, {"config.py": 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'})
    spawned = {"called": False}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[:2] == ["codex", "exec"]:
            spawned["called"] = True
        return real_run(cmd, **kwargs)  # git ls-files/etc fall through to the real run

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", spy)
    with pytest.raises(gates.SecretFoundError):
        cli_doer.run_doer("gpt", "tidy the config", repo_root=tmp_path)

    assert spawned["called"] is False          # CLI never saw the secret
    assert _worktree_count(tmp_path) == 1       # worktree cleaned up on refusal


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


# === doer total wire-byte cap (bounds task + tracked worktree files) =========


def test_doer_refuses_when_total_wire_bytes_exceeds_cap(
    tmp_path, monkeypatch, _codex_installed
):
    """A worktree whose task + committed files exceed the wire cap is refused BEFORE the
    vendor CLI is spawned (it would otherwise read and egress those files), and the
    throwaway worktree is cleaned up. 100 file bytes + 1 task byte = 101 > 100."""
    _init_git_repo(tmp_path, {"data.txt": "x" * 100})
    spawned = {"called": False}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[:2] == ["codex", "exec"]:
            spawned["called"] = True
        return real_run(cmd, **kwargs)  # git falls through to the real run

    monkeypatch.setattr("cohort.engines.cli_doer.subprocess.run", spy)
    with pytest.raises(gates.PayloadTooLargeError):
        cli_doer.run_doer("gpt", "t", repo_root=tmp_path, max_wire_bytes=100)

    assert spawned["called"] is False        # CLI never spawned over the cap
    assert _worktree_count(tmp_path) == 1     # worktree cleaned up on refusal


def test_doer_allows_when_total_wire_bytes_within_cap(
    tmp_path, monkeypatch, _codex_installed
):
    """The boundary is inclusive: 100 file bytes + 1 task byte = 101 exactly at a
    101-byte cap runs. Paired with the over-cap test, one byte of cap decides it."""
    _init_git_repo(tmp_path, {"data.txt": "x" * 100})
    monkeypatch.setattr(
        "cohort.engines.cli_doer.subprocess.run", _fake_codex({"data.txt": "y\n"})
    )
    result = cli_doer.run_doer("gpt", "t", repo_root=tmp_path, max_wire_bytes=101)
    assert result.returncode == 0
    assert result.changed_files == ["data.txt"]


def test_assert_worktree_within_wire_budget_bites_at_the_boundary(tmp_path):
    """Direct check that the cap refuses just over and passes exactly at the ceiling —
    exercises the same helper both doers call."""
    _init_git_repo(tmp_path, {"data.txt": "x" * 100})
    worktree = patch_proposal._create_worktree(tmp_path)
    try:
        with pytest.raises(gates.PayloadTooLargeError):
            cli_doer._assert_worktree_within_wire_budget(
                worktree, "t", max_wire_bytes=100  # 101 > 100
            )
        cli_doer._assert_worktree_within_wire_budget(
            worktree, "t", max_wire_bytes=101  # 101 == 101, allowed
        )
    finally:
        patch_proposal.cleanup_worktree(tmp_path, worktree)


def test_worktree_byte_count_fails_closed_on_unmeasurable_file(tmp_path):
    """A tracked file whose size cannot be measured refuses the dispatch (fail closed)
    rather than dropping it from the sum and undercounting the exposed payload."""
    _init_git_repo(tmp_path, {"a.txt": "hello", "b.txt": "world"})
    worktree = patch_proposal._create_worktree(tmp_path)
    try:
        (worktree / "a.txt").unlink()  # still tracked by git, but unstattable now
        with pytest.raises(gates.PayloadTooLargeError, match="could not be measured"):
            cli_doer._worktree_exposed_byte_count(worktree)
    finally:
        patch_proposal.cleanup_worktree(tmp_path, worktree)


# === grok read-only reviewer (run_grok_review) ==============================


@pytest.fixture
def _grok_installed(monkeypatch):
    """Make both grok-cli and bwrap look installed so the sandbox assertion passes and
    the gate sequence (not the availability check) is what a test exercises."""
    monkeypatch.setattr(
        cli_doer.shutil, "which",
        lambda name: "/usr/bin/grok" if name == "grok" else None,
    )
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")


def _spy_grok(stdout: str = "grok's analysis", returncode: int = 0):
    """A run_grok_in_worktree stand-in that records it ran and returns fixed stdout."""
    calls = {"n": 0}

    def run(worktree, task, **kwargs):
        calls["n"] += 1
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return calls, run


def test_grok_cli_available_requires_both_grok_and_bwrap(monkeypatch):
    """_grok_cli_available is True only when grok AND bwrap are both present — either
    missing means the CLI path can't run confined, so callers fall back to the API."""
    monkeypatch.setattr(cli_doer.shutil, "which",
                        lambda name: "/usr/bin/grok" if name == "grok" else None)
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    assert cli_doer._grok_cli_available() is True

    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    assert cli_doer._grok_cli_available() is False  # bwrap missing

    monkeypatch.setattr(cli_doer.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    assert cli_doer._grok_cli_available() is False  # grok missing


def test_run_grok_review_returns_stdout_and_discards_the_worktree(
    tmp_path, monkeypatch, _grok_installed
):
    """The read path returns grok's stdout as the analysis and ALWAYS discards the
    worktree (unlike the doer, which leaves it for review) — grok's confined edits go
    with it."""
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    calls, spy = _spy_grok(stdout="the code looks fine")
    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", spy)

    result = cli_doer.run_grok_review("review this", repo_root=tmp_path)

    assert calls["n"] == 1
    assert result.analysis == "the code looks fine"
    assert result.transcript == "the code looks fine"
    assert result.returncode == 0
    assert _worktree_count(tmp_path) == 1  # worktree discarded (read path keeps no diff)


def test_run_grok_review_secret_in_task_refuses_before_grok(
    tmp_path, monkeypatch, _grok_installed
):
    """A secret in the task refuses at the task secret-scan — BEFORE the worktree is even
    created and BEFORE grok runs (gate order mirrors run_grok_doer)."""
    _init_git_repo(tmp_path, {"a.py": "1\n"})
    calls, spy = _spy_grok()
    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", spy)

    with pytest.raises(gates.SecretFoundError):
        cli_doer.run_grok_review(
            "use AWS_SECRET_ACCESS_KEY = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY here",
            repo_root=tmp_path,
        )

    assert calls["n"] == 0                 # grok never reached
    assert _worktree_count(tmp_path) == 1  # no worktree leaked (refused before creation)


def test_run_grok_review_egress_optout_refuses_before_grok(
    tmp_path, monkeypatch, _grok_installed
):
    """The repo egress opt-out refuses before grok and before any worktree is created."""
    _init_git_repo(tmp_path, {"a.py": "1\n"})
    calls, spy = _spy_grok()
    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", spy)

    with pytest.raises(gates.EgressBlockedError):
        cli_doer.run_grok_review(
            "review", repo_root=tmp_path,
            project_context_text="## Egress\n\ncohort:egress=deny\n",
        )

    assert calls["n"] == 0
    assert _worktree_count(tmp_path) == 1


def test_run_grok_review_over_cap_refuses_before_grok_and_cleans_up(
    tmp_path, monkeypatch, _grok_installed
):
    """An over-cap worktree refuses at the wire-byte gate — grok is never reached and the
    throwaway worktree is cleaned up (101 bytes > 100-byte cap)."""
    _init_git_repo(tmp_path, {"data.txt": "x" * 100})
    calls, spy = _spy_grok()
    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", spy)

    with pytest.raises(gates.PayloadTooLargeError):
        cli_doer.run_grok_review("t", repo_root=tmp_path, max_wire_bytes=100)

    assert calls["n"] == 0                 # gate fired before grok
    assert _worktree_count(tmp_path) == 1  # worktree cleaned up on refusal


def test_run_grok_review_worktree_secret_scan_refuses_before_grok(
    tmp_path, monkeypatch, _grok_installed
):
    """A committed worktree file carrying a credential refuses at the worktree secret
    scan — grok never reads it, and the worktree is cleaned up."""
    _init_git_repo(tmp_path, {"config.py": 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'})
    calls, spy = _spy_grok()
    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", spy)

    with pytest.raises(gates.SecretFoundError):
        cli_doer.run_grok_review("tidy the config", repo_root=tmp_path)

    assert calls["n"] == 0
    assert _worktree_count(tmp_path) == 1


def test_run_grok_review_cleans_up_worktree_on_grok_failure(
    tmp_path, monkeypatch, _grok_installed
):
    """If grok itself fails (times out / raises), the read path still discards the
    worktree — the finally cleanup runs on the failure path too."""
    _init_git_repo(tmp_path, {"a.py": "1\n"})

    def boom(worktree, task, **kwargs):
        raise subprocess.TimeoutExpired(cmd="grok", timeout=1.0)

    monkeypatch.setattr(cli_doer, "run_grok_in_worktree", boom)

    with pytest.raises(subprocess.TimeoutExpired):
        cli_doer.run_grok_review("review", repo_root=tmp_path)

    assert _worktree_count(tmp_path) == 1  # cleaned up despite grok failing


def test_run_grok_review_empty_task_refused(tmp_path, _grok_installed):
    """An empty task is refused, matching run_grok_doer."""
    with pytest.raises(cli_doer.DoerError, match="empty"):
        cli_doer.run_grok_review("   ", repo_root=tmp_path)


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

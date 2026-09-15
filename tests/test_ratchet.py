"""Tests for the ratchet loop (cohort.engines.ratchet).

The proposing doer is mocked to write a metric value into the worktree; everything else
is real - the evaluator command runs, the metric is parsed, and git keep/revert actually
commits or resets - so the ratchet's core (climb, keep-only-gains, revert) is exercised
end to end without any external engine or network.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from cohort import gitutil
from cohort.engines import cli_doer, gates, ratchet


@pytest.fixture(autouse=True)
def _reclaim_leaked_worktrees():
    """Remove any proposal worktree a test leaves behind.

    `run_ratchet` deliberately leaves its worktree in place on success so a human can
    review the diff — correct for a real run, and a leak in a suite that calls it ten
    times. Nothing reclaimed them afterwards, so every full-suite run stranded up to ten
    `cohort-proposal-*` directories under the system temp dir; 1,592 had accumulated over
    nine days and eventually exhausted the tmpfs quota mid-run.

    Autouse and snapshot-based rather than per-call cleanup, so a test added later cannot
    forget: only directories that appear *during* a test are removed, never one that was
    already there.
    """
    tmp = Path(tempfile.gettempdir())
    before = set(tmp.glob("cohort-proposal-*"))
    yield
    for leaked in set(tmp.glob("cohort-proposal-*")) - before:
        shutil.rmtree(leaked, ignore_errors=True)

# Cross-platform evaluator (no Unix `cat`): a committed script prints metric.txt's number.
_EVAL = "python read.py"
_READ_PY = 'import os\nprint(open("metric.txt").read() if os.path.exists("metric.txt") else "")\n'


def _init_git_repo(root: Path, files: dict[str, str]) -> None:
    files = {**files, "read.py": _READ_PY}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.co", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=root, check=True, capture_output=True,
    )


def _doer_writes(values):
    """A _propose_into_worktree stand-in: each call writes the next value to metric.txt."""
    seq = iter(values)

    def propose(engine, task, worktree, **kwargs):
        (Path(worktree) / "metric.txt").write_text(str(next(seq)), encoding="utf-8")

    return propose


def test_ratchet_keeps_only_improvements_when_minimizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    # i1: 9 (better, keep) - i2: 12 (worse, revert) - i3: 7 (better, keep)
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([9, 12, 7]))

    result = ratchet.run_ratchet(
        "gpt", "lower the number", repo_root=tmp_path,
        evaluator_cmd=_EVAL, goal="minimize", budget=3,
    )

    assert result.baseline == 10.0
    assert result.best == 7.0
    assert [s.iteration for s in result.steps if s.kept] == [1, 3]
    assert not result.steps[1].kept  # the worse attempt was reverted
    assert (result.worktree / "metric.txt").read_text(encoding="utf-8").strip() == "7"
    assert result.improved


def test_ratchet_maximizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "5\n"})
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([8, 3, 9]))
    result = ratchet.run_ratchet(
        "gpt", "raise it", repo_root=tmp_path,
        evaluator_cmd=_EVAL, goal="maximize", budget=3,
    )
    assert result.best == 9.0
    assert [s.iteration for s in result.steps if s.kept] == [1, 3]  # 3 was worse, reverted


def test_ratchet_reverts_a_tie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([10]))  # no change
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1,
    )
    assert result.best == 10.0
    assert not any(s.kept for s in result.steps)  # a tie is not a gain


def test_ratchet_writes_the_staircase_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([9, 8]))
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=2,
    )
    ledger = result.ledger_path.read_text(encoding="utf-8")
    assert "baseline" in ledger
    assert ledger.count("kept") >= 2  # header word + two kept rows


def test_ratchet_a_failed_proposal_is_reverted_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    calls = {"n": 0}

    def flaky(engine, task, worktree, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("doer crashed")
        (Path(worktree) / "metric.txt").write_text("6", encoding="utf-8")

    monkeypatch.setattr(ratchet, "_propose_into_worktree", flaky)
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=2,
    )
    assert result.steps[0].kept is False and "failed" in result.steps[0].note
    assert result.best == 6.0  # the second iteration still improved


def test_ratchet_refuses_when_baseline_metric_is_unreadable(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, {"a.txt": "x\n"})
    with pytest.raises(ratchet.RatchetError, match="baseline"):
        ratchet.run_ratchet(
            "gpt", "t", repo_root=tmp_path, evaluator_cmd="echo no-number-here", budget=1,
        )


def test_ratchet_honors_egress_optout_before_any_doer_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    reached = {"doer": False}

    def must_not_run(*a, **k):
        reached["doer"] = True

    monkeypatch.setattr(ratchet, "_propose_into_worktree", must_not_run)
    with pytest.raises(gates.EgressBlockedError):
        ratchet.run_ratchet(
            "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1,
            project_context_text="## Egress\n\ncohort:egress=deny\n",
        )
    assert reached["doer"] is False


def test_ratchet_rejects_empty_task_and_evaluator(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    with pytest.raises(ratchet.RatchetError):
        ratchet.run_ratchet("gpt", "   ", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1)
    with pytest.raises(ratchet.RatchetError):
        ratchet.run_ratchet("gpt", "t", repo_root=tmp_path, evaluator_cmd="  ", budget=1)


def test_ratchet_metric_regex_extracts_the_right_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "val_bpb=1.5 tokens=999\n"})

    def propose(engine, task, worktree, **kwargs):
        (Path(worktree) / "metric.txt").write_text("val_bpb=1.2 tokens=111", encoding="utf-8")

    monkeypatch.setattr(ratchet, "_propose_into_worktree", propose)
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL,
        metric_regex=r"val_bpb=([0-9.]+)", goal="minimize", budget=1,
    )
    assert result.baseline == 1.5 and result.best == 1.2  # not fooled by the token count


# === #286: the evaluator executes engine-written code and is confined accordingly ====
#
# The evaluator is the one path where an external engine's output is *executed* rather
# than reviewed: it runs in the worktree the engine just wrote to, so a `conftest.py` the
# engine planted runs as the user. These pin the confinement: a scrubbed environment with
# an ephemeral HOME everywhere, and on Linux with bubblewrap the grok jail with the
# network unshared.

_PROBE_PY = (
    "import os, socket, sys\n"
    "print('probe-ran')\n"
    "print('host-var:' + os.environ.get('COHORT_FAKE_HOST_SECRET', 'absent'))\n"
    "print('HOME=' + os.environ.get('HOME', ''))\n"
    "port = int(sys.argv[1]) if len(sys.argv) > 1 else 0\n"
    "if port:\n"
    "    try:\n"
    "        socket.create_connection(('127.0.0.1', port), timeout=3).close()\n"
    "        print('NET REACHED')\n"
    "    except OSError as exc:\n"
    "        print('net blocked: ' + type(exc).__name__)\n"
)


def _bwrap_unusable_reason() -> str | None:
    """Why the evaluator jail cannot run here, or ``None`` if it can.

    Present is not the same as usable: a host may have ``bwrap`` installed but forbid
    unprivileged user namespaces (Ubuntu 24.04's AppArmor default), in which case every
    sandbox creation fails at run time.
    """
    exe = cli_doer._bwrap()
    if exe is None:
        return "bwrap is not installed"
    try:
        proc = subprocess.run(
            [exe, "--unshare-all", "--unshare-net", "--ro-bind", "/", "/", "--", "true"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"bwrap cannot sandbox here ({type(exc).__name__})"
    if proc.returncode != 0:
        return f"bwrap cannot sandbox here ({(proc.stderr or '').strip()[:200]})"
    return None


def _require_bwrap_or_skip() -> None:
    """Skip a live-jail test where bwrap cannot sandbox — unless ``COHORT_REQUIRE_BWRAP=1``.

    CI's Linux job sets that variable (it installs bubblewrap for exactly this), so there
    an unusable jail is a red test, never a silent skip that keeps confinement regressions
    green. Locally the skip stays a skip.
    """
    reason = _bwrap_unusable_reason()
    if reason is None:
        return
    if os.environ.get("COHORT_REQUIRE_BWRAP") == "1":
        pytest.fail(
            f"COHORT_REQUIRE_BWRAP=1 but {reason}; the evaluator confinement test must "
            "run on this host (on Ubuntu 24.04 runners: "
            "`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`)"
        )
    pytest.skip(reason)


def test_require_bwrap_gate_fails_rather_than_skips_when_ci_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) With COHORT_REQUIRE_BWRAP=1 an absent bwrap is a FAILURE, not a skip; without
    the variable the same absence skips as before."""
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    monkeypatch.setenv("COHORT_REQUIRE_BWRAP", "1")
    with pytest.raises(pytest.fail.Exception, match="COHORT_REQUIRE_BWRAP=1 but bwrap"):
        _require_bwrap_or_skip()
    monkeypatch.delenv("COHORT_REQUIRE_BWRAP")
    with pytest.raises(pytest.skip.Exception, match="not installed"):
        _require_bwrap_or_skip()


def test_evaluator_runs_under_a_scrubbed_env_with_an_ephemeral_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) Even without a jail, the evaluator never sees the host environment: a planted
    host variable is absent and HOME is a throwaway directory beside the worktree, not
    the real home. Runs the unjailed path on every platform."""
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    monkeypatch.setenv("COHORT_FAKE_HOST_SECRET", "leak-me")
    _init_git_repo(tmp_path, {"probe.py": _PROBE_PY})
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt)],
                   cwd=tmp_path, check=True, capture_output=True)

    metric, output = ratchet._evaluate(wt, "python probe.py", None, 60.0)

    assert "probe-ran" in output
    assert "leak-me" not in output and "host-var:absent" in output
    home_line = next(line for line in output.splitlines() if line.startswith("HOME="))
    reported_home = Path(home_line[len("HOME="):])
    assert reported_home != Path.home()
    assert reported_home.parent == wt.parent          # ephemeral, beside the worktree


def test_confined_evaluator_argv_unshares_the_network_and_binds_nothing_from_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) The jail argv is the grok jail with the network UNSHARED (grok's own argv keeps
    it open for the xAI API), running the command through /bin/sh in the worktree — and
    without grok's read-only binds from the real home (``~/.grok`` holds grok's API key;
    engine-written code must not be able to copy it into the worktree for the next
    iteration's doer to egress). Pure argv check; no bwrap needed."""
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(cli_doer, "_vendor_binary_binds",
                        lambda name: ["--ro-bind", str(Path.home() / ".local"), "/x"])
    grok_cfg = tmp_path / "fakehome" / ".grok"
    grok_cfg.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fakehome"))
    wt = tmp_path / "wt"

    argv = ratchet._confined_evaluator_argv(wt, tmp_path / "home", "pytest -q")

    assert argv is not None
    assert "--unshare-net" in argv and "--share-net" not in argv
    assert "--unshare-all" in argv and "--die-with-parent" in argv
    assert argv[-3:] == ["/bin/sh", "-c", "pytest -q"]
    assert "--chdir" in argv and argv[argv.index("--chdir") + 1] == str(wt)
    sources = [argv[i + 1] for i, tok in enumerate(argv) if tok in ("--ro-bind", "--bind")]
    assert str(wt) in sources                              # the worktree is bound
    home = tmp_path / "fakehome"
    assert not any(Path(s).is_relative_to(home) for s in sources)  # nothing from home
    assert "--tmpfs" in argv and str(tmp_path / "home") in argv     # HOME is a tmpfs


def test_confined_evaluator_argv_is_none_without_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    assert ratchet._confined_evaluator_argv(tmp_path / "wt", tmp_path / "h", "true") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap is a Linux jail")
def test_evaluator_in_the_jail_cannot_read_host_env_or_reach_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a)(e) Live kernel-level check: an evaluator that prints a host variable or dials a
    listener the test opened on the host's loopback gets nothing — the variable is
    scrubbed and the jail's network namespace has no route to the host. Skip-gated only
    where bwrap genuinely cannot sandbox, and NOT skip-gated at all under
    COHORT_REQUIRE_BWRAP=1 (CI Linux)."""
    _require_bwrap_or_skip()
    monkeypatch.setenv("COHORT_FAKE_HOST_SECRET", "leak-me")
    _init_git_repo(tmp_path, {"probe.py": _PROBE_PY})
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt)],
                   cwd=tmp_path, check=True, capture_output=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        metric, output = ratchet._evaluate(wt, f"python3 probe.py {port}", None, 60.0)
    finally:
        listener.close()

    assert "probe-ran" in output, output
    assert "leak-me" not in output and "host-var:absent" in output
    assert "NET REACHED" not in output and "net blocked" in output
    assert str(Path.home()) not in output          # HOME is the ephemeral tmpfs


def test_evaluator_is_spawned_in_its_own_session_with_stdin_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) The evaluator gets start_new_session=True (so a timeout can kill its whole
    process group) and stdin=DEVNULL (no TTY to inject keystrokes through)."""
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    seen: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            self.returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, timeout=None):
            return "42\n", ""

    monkeypatch.setattr(ratchet.subprocess, "Popen", FakePopen)
    wt = tmp_path / "wt"
    wt.mkdir()
    metric, _ = ratchet._evaluate(wt, "echo 42", None, 5.0)

    assert metric == 42.0
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["cwd"] == str(wt)
    assert "COHORT_FAKE_HOST_SECRET" not in seen["kwargs"]["env"]
    assert seen["kwargs"]["env"]["HOME"] != str(Path.home())


@pytest.mark.skipif(os.name == "nt", reason="process groups and /bin/sh are POSIX-only")
def test_a_timed_out_evaluator_takes_its_whole_process_group_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) A grandchild the evaluator backgrounded must not outlive the timeout: it would
    otherwise keep writing into the worktree while `_revert` runs. Unjailed path (the
    jailed one is reaped by bwrap's --die-with-parent on top of the same group kill)."""
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)
    wt = tmp_path / "wt"
    wt.mkdir()
    marker = wt / "grandchild-still-alive"
    script = f"(while true; do touch {marker}; sleep 0.05; done) & sleep 30"

    metric, output = ratchet._evaluate(wt, script, None, 1.0)

    assert metric is None and "timed out" in output
    marker.unlink(missing_ok=True)
    time.sleep(0.6)
    assert not marker.exists(), "a grandchild outlived the evaluator timeout"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap is a Linux jail")
def test_a_timed_out_jailed_evaluator_leaves_no_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) The same grandchild check through the live jail: killing the bwrap leader must
    take the jailed tree with it (its own session inside the jail is not in our group)."""
    _require_bwrap_or_skip()
    wt = tmp_path / "wt"
    wt.mkdir()
    marker = wt / "grandchild-still-alive"
    script = f"(while true; do touch {marker}; sleep 0.05; done) & sleep 30"

    metric, output = ratchet._evaluate(wt, script, None, 1.0)

    assert metric is None and "timed out" in output
    marker.unlink(missing_ok=True)
    time.sleep(0.6)
    assert not marker.exists(), "a jailed grandchild outlived the evaluator timeout"


# === (b) git in the loop is non-interactive, bounded, and never signs ============


def test_ratchet_git_calls_are_non_interactive_bounded_and_unsigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        seen.append({"cmd": cmd, **kwargs})
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(ratchet.subprocess, "run", spy)
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    wt = tmp_path / "wt"
    real_run(["git", "worktree", "add", "-q", "--detach", str(wt)],
             cwd=tmp_path, check=True, capture_output=True)
    seen.clear()
    (wt / "metric.txt").write_text("9", encoding="utf-8")
    ratchet._keep(wt, "ratchet i=1")
    ratchet._revert(wt)

    assert len(seen) == 4  # add, commit, reset, clean
    for call in seen:
        assert call["cmd"][:3] == ["git", "-C", str(wt)]
        assert "commit.gpgsign=false" in call["cmd"]
        assert call["timeout"] == cli_doer._GIT_TIMEOUT_SECONDS
        for key, value in gitutil.GIT_ENV.items():
            assert call["env"][key] == value


def cli_doer_git_timeout() -> float:
    return cli_doer._GIT_TIMEOUT_SECONDS


def test_ratchet_keeps_a_gain_in_a_repo_configured_to_sign_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) `commit.gpgsign=true` with an unusable signer would hang or fail every keep; the
    loop's own commits are unsigned by construction (the human signs the real PR)."""
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "gpg.program", str(tmp_path / "no-such-gpg")],
                   cwd=tmp_path, check=True)
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([9]))

    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1,
    )

    assert result.best == 9.0 and result.steps[0].kept


@pytest.mark.skipif(os.name == "nt", reason="the hanging hook is a POSIX shell script")
def test_a_hung_git_records_a_failed_step_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A git that hangs (here: a pre-commit hook that sleeps, once) hits the timeout,
    the step is recorded as failed, and the next iteration still runs and can keep."""
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    fired = tmp_path / "hook-fired"
    hook = hooks / "pre-commit"
    hook.write_text(
        f"#!/bin/sh\nif [ ! -e {fired} ]; then touch {fired}; sleep 30; fi\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=tmp_path, check=True)
    monkeypatch.setattr(cli_doer, "_GIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([9, 8]))

    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=2,
    )

    assert not result.steps[0].kept and "TimeoutExpired" in result.steps[0].note
    assert result.steps[1].kept and result.best == 8.0
    assert fired.exists()


# === (d) #237: egress derived from the repo; codex path wire-caps + secret-scans =====


def test_ratchet_derives_the_egress_optout_from_the_repo_when_the_kwarg_is_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    (tmp_path / ".cohort").mkdir()
    (tmp_path / ".cohort" / "project_context.md").write_text(
        "## Egress\n\ncohort:egress=deny\n", encoding="utf-8"
    )
    reached = {"doer": False}

    def must_not_run(*a, **k):
        reached["doer"] = True

    monkeypatch.setattr(ratchet, "_propose_into_worktree", must_not_run)
    with pytest.raises(gates.EgressBlockedError):
        ratchet.run_ratchet("gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1)
    assert reached["doer"] is False


def test_ratchet_codex_path_refuses_a_committed_secret_before_codex_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {
        "metric.txt": "10\n",
        "config.py": 'AWS_KEY = "AKIA' + "ABCDEFGHIJKLMNOP" + '"\n',
    })
    spawned = {"codex": False}

    def fake_codex(worktree, task, **kwargs):
        spawned["codex"] = True

    monkeypatch.setattr(cli_doer, "run_codex_in_worktree", fake_codex)
    with pytest.raises(gates.SecretFoundError):
        ratchet.run_ratchet("gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1)
    assert spawned["codex"] is False


def test_ratchet_codex_path_refuses_over_the_wire_cap_before_codex_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n", "data.txt": "x" * 100})
    spawned = {"codex": False}

    def fake_codex(worktree, task, **kwargs):
        spawned["codex"] = True

    monkeypatch.setattr(cli_doer, "run_codex_in_worktree", fake_codex)
    with pytest.raises(gates.PayloadTooLargeError):
        ratchet.run_ratchet(
            "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1, max_wire_bytes=50,
        )
    assert spawned["codex"] is False


def test_ratchet_codex_path_runs_codex_when_the_worktree_clears_both_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})

    def fake_codex(worktree, task, **kwargs):
        (Path(worktree) / "metric.txt").write_text("9", encoding="utf-8")

    monkeypatch.setattr(cli_doer, "run_codex_in_worktree", fake_codex)
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd=_EVAL, budget=1,
    )
    assert result.best == 9.0

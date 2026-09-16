"""The ratchet loop — a metric-gated autonomous optimization loop.

Inspired by Karpathy's AutoResearch (propose a change, run a fixed-budget evaluator,
keep the commit if a number improved, ``git reset`` if not, repeat) but adapted to
Cohort's human-gate posture: the whole climb runs **inside a throwaway git worktree**,
bounded by an iteration budget, and the result is a *staircase* the human reviews and
merges via PR. The autonomy is the inner loop; the merge stays gated.

The safety-critical mechanics are enforced here in **code**, not left to a prose
protocol: the loop only ever mutates the worktree (never ``repo_root``), the keep/revert
is a real ``git commit`` / ``git reset --hard``, the budget is a hard cap, and each
iteration's *proposal* is produced by a gated doer — Codex under its own sandbox, or
Grok's egress-gated agentic patch.

The **evaluator command is user-supplied, but what it executes is not.** It runs inside
the worktree the engine just wrote to — a ``conftest.py`` or an import the engine planted
runs as the user the moment the evaluator does. This is the one path where an external
engine's output is *executed* rather than reviewed, so the evaluator is confined like a
doer (#286): a scrubbed environment with an ephemeral ``HOME`` (never the host's secrets
or dotfiles), its own session with a process-group kill on timeout, and — on Linux with
bubblewrap — the same kernel jail the grok doer gets, with the network **unshared**. Where
``bwrap`` is absent (macOS, Windows) only the environment scrub and the group kill apply,
and the evaluator can still write outside the worktree and reach the network; the docs say
so rather than claim otherwise.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from cohort import gitutil
from cohort.engines import UnknownEngineError, describe_registered_engines, gates, get_engine, patch
from cohort.engines import cli_doer, codex_cli, patch_proposal, xai_agentic

# A change is "kept" only if the metric strictly moved the right way; ties revert, so the
# lineage only advances on a real gain (Karpathy's ratchet).
_DEFAULT_EVAL_TIMEOUT = 300.0  # seconds per evaluator run
# Windows cannot start a python (or most anything) without these; they are paths and
# switches, never secrets. POSIX needs nothing beyond what ``_scrubbed_env`` already keeps.
_WINDOWS_PROCESS_ENV = ("SYSTEMROOT", "COMSPEC", "PATHEXT")


class RatchetError(Exception):
    """The ratchet could not run (bad engine, empty task/evaluator, baseline failure)."""


@dataclass
class RatchetStep:
    """One iteration: the metric the proposal produced and whether it was kept."""

    iteration: int
    metric: float | None
    kept: bool
    note: str


@dataclass
class RatchetResult:
    """The climb: the worktree holding the accumulated improvements, the baseline->best
    move, and the full ledger of steps (the staircase) for human review."""

    worktree: Path
    engine: str
    goal: str
    baseline: float | None
    best: float | None
    steps: list[RatchetStep] = field(default_factory=list)
    ledger_path: Path | None = None

    @property
    def improved(self) -> bool:
        return (
            self.baseline is not None
            and self.best is not None
            and self.best != self.baseline
        )


def _parse_metric(output: str, metric_regex: str | None) -> float | None:
    """Extract the objective number from evaluator output.

    With ``metric_regex`` the first capture group is used; otherwise the *last* number in
    the output is taken (evaluators conventionally print the score last). ``None`` if no
    number is found - treated as a failed run (reverted)."""
    if metric_regex:
        m = re.search(metric_regex, output)
        if not m:
            return None
        raw = m.group(1) if m.groups() else m.group(0)
        try:
            return float(raw)
        except ValueError:
            return None
    numbers = re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", output)
    return float(numbers[-1]) if numbers else None


def _confined_evaluator_argv(
    worktree: Path, sandbox_home: Path, evaluator_cmd: str
) -> list[str] | None:
    """The bubblewrap argv that runs ``evaluator_cmd`` jailed, or ``None`` without bwrap.

    Reuses the grok doer's jail (:func:`cli_doer._grok_sandbox_argv`: ``/usr`` read-only,
    a minimal ``/etc`` subset, a tmpfs ``HOME``, the worktree as the only writable path,
    own session) with two deliberate differences for code that is *executed*, not merely
    run by a vendor CLI:

    * the network is **unshared** — grok's argv keeps it open for the xAI API, but the
      evaluator has no business on the network at all, and closing it is what stops
      engine-written code from exfiltrating whatever it can read inside the jail;
    * nothing from the real home is bound — grok's argv binds ``~/.grok`` (which holds its
      API key) and the user-local grok install read-only; engine-written code must not be
      able to copy those into the worktree for the *next* iteration's doer to egress.

    The command runs through ``/bin/sh -c`` inside the jail, the equivalent of the
    ``shell=True`` the unjailed path uses, with ``--chdir`` into the worktree.
    """
    if cli_doer._bwrap() is None:
        return None
    jailed = cli_doer._grok_sandbox_argv(
        worktree, sandbox_home, ["/bin/sh", "-c", evaluator_cmd]
    )
    home = Path.home()
    argv: list[str] = []
    i = 0
    while i < len(jailed):
        tok = jailed[i]
        if tok == "--share-net":
            i += 1
            continue
        if tok == "--ro-bind" and Path(jailed[i + 1]).is_relative_to(home):
            i += 3
            continue
        argv.append(tok)
        i += 1
    argv.insert(1, "--unshare-net")
    return argv


def _evaluator_env(sandbox_home: Path) -> dict[str, str]:
    """The evaluator's environment: the doers' scrub with nothing passed through, and
    ``HOME`` pointed at an ephemeral directory, so engine-written code the evaluator runs
    can neither read a host secret out of the environment nor the user's dotfiles."""
    env = cli_doer._scrubbed_env(home=sandbox_home, passthrough=())
    if os.name == "nt":
        for name in _WINDOWS_PROCESS_ENV:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        # Keep the evaluator's temp files out of the worktree (tempfile falls back to the
        # cwd without these, and a keep's ``git add -A`` would then track them).
        env["TEMP"] = env["TMP"] = str(sandbox_home)
    return env


def _run_evaluator(
    cmd: str | list[str], *, shell: bool, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess:
    """Run the evaluator non-interactively and, on timeout, kill its whole process group.

    The same discipline as :func:`cli_doer._launch_vendor_cli` (which cannot be reused
    directly: the unjailed evaluator needs ``shell=True`` and a ``cwd``). A plain
    ``subprocess.run(timeout=…)`` kills only the direct child, so an evaluator's helpers
    survived the cap and raced ``_revert`` while still writing into the worktree;
    ``start_new_session`` makes the group kill possible and
    :func:`cli_doer._kill_process_group` performs it.
    In the jail the leader is bwrap, whose ``--die-with-parent`` takes the jailed tree
    (its own session, not in our group) down with it. stdin is ``DEVNULL``: an inherited
    TTY could otherwise be used to inject keystrokes back into Cohort (TIOCSTI).
    """
    with subprocess.Popen(
        cmd,
        shell=shell,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            cli_doer._kill_process_group(proc)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _evaluate(
    worktree: Path, evaluator_cmd: str, metric_regex: str | None, timeout: float
) -> tuple[float | None, str]:
    """Run the user's evaluator, confined, in the worktree and parse the metric.

    A crash or timeout yields ``None`` (a failed experiment, reverted like any
    non-improvement). The ephemeral ``HOME`` lives beside the worktree in the throwaway
    parent directory (reaped with it); inside the jail it is a tmpfs instead."""
    sandbox_home = worktree.parent / "evaluator-home"
    sandbox_home.mkdir(exist_ok=True)
    env = _evaluator_env(sandbox_home)
    argv = _confined_evaluator_argv(worktree, sandbox_home, evaluator_cmd)
    try:
        proc = _run_evaluator(
            argv if argv is not None else evaluator_cmd, shell=argv is None,
            cwd=worktree, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"evaluator timed out after {timeout:.0f}s"
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return _parse_metric(combined, metric_regex), combined


def _is_improvement(metric: float | None, best: float | None, goal: str) -> bool:
    if metric is None:
        return False
    if best is None:
        return True  # first successful measurement establishes the ratchet
    return metric < best if goal == "minimize" else metric > best


def _propose_into_worktree(
    engine: str,
    task: str,
    worktree: Path,
    *,
    repo_root: Path,
    model: str | None,
    footprint: list[str] | None,
    project_context_text: str,
    timeout: float,
    max_wire_bytes: int,
) -> None:
    """Have the engine propose+apply one change to the *existing* worktree.

    Codex edits it directly under its sandbox; Grok explores read-only then Cohort applies
    its gated patch. Either way the change lands only in the worktree.

    Codex reads the worktree's tracked files and sends them to the vendor, so — exactly as
    :func:`cli_doer.run_codex_doer` does — the total exposed bytes are capped and those
    files are secret-scanned before every dispatch (#237). Every iteration, not just the
    first: a kept proposal changes what the next dispatch exposes."""
    try:
        spec = get_engine(engine)
    except UnknownEngineError:
        raise RatchetError(
            f"engine {engine!r} has no ratchet doer; registered engines: "
            f"{describe_registered_engines()}"
        ) from None
    if spec.transport == codex_cli.TRANSPORT:
        cli_doer._assert_worktree_within_wire_budget(
            worktree, task, max_wire_bytes=max_wire_bytes
        )
        cli_doer._assert_worktree_files_have_no_secrets(worktree, repo_root)
        cli_doer.run_codex_in_worktree(worktree, task, model=model, timeout=timeout)
        return
    if spec.transport == "xai_chat_completions":
        instruction = patch_proposal._assemble_agentic_task(
            task, footprint or ["."], project_context_text
        )
        result = xai_agentic.run_agentic(
            instruction, root=worktree, model=model, engine_name="grok",
        )
        if result.stopped_reason != "final":
            raise RatchetError(f"grok did not produce a patch ({result.stopped_reason})")
        proposal = patch.parse_patch(result.text)
        paths = [e.path for e in proposal.edits] + [f.path for f in proposal.new_files]
        if footprint:
            gates.assert_paths_allowed(paths, allowed_footprint=footprint)
        gates.assert_no_secrets(
            "\n".join([e.replace for e in proposal.edits] + [f.content for f in proposal.new_files])
        )
        patch.apply_patch(proposal, worktree)
        return
    raise RatchetError(
        f"engine {spec.name!r} (transport {spec.transport!r}) has no ratchet doer"
    )


def _git(worktree: Path, *args: str) -> None:
    """One hardened git call in the worktree: non-interactive, bounded, never signing.

    :data:`gitutil.GIT_ENV` stops git prompting for credentials or host keys, and
    ``commit.gpgsign=false`` stops a user's signing config (an expired agent, a pinentry)
    from hanging the loop's own commits — the human signs the real PR, not the staircase.
    A git that still hangs hits the timeout and raises ``TimeoutExpired``; the loop
    records that iteration as failed and moves on rather than stalling forever.
    """
    subprocess.run(
        ["git", "-C", str(worktree), "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
        env={**os.environ, **gitutil.GIT_ENV},
        timeout=cli_doer._GIT_TIMEOUT_SECONDS,
    )


def _keep(worktree: Path, message: str) -> None:
    _git(worktree, "add", "-A")
    _git(
        worktree, "-c", "user.email=ratchet@cohort", "-c", "user.name=cohort-ratchet",
        "commit", "-q", "-m", message,
    )


def _revert(worktree: Path) -> None:
    _git(worktree, "reset", "-q", "--hard", "HEAD")
    _git(worktree, "clean", "-qfd")


def run_ratchet(
    engine: str,
    task: str,
    *,
    repo_root: Path,
    evaluator_cmd: str,
    metric_regex: str | None = None,
    goal: str = "minimize",
    budget: int = 10,
    footprint: list[str] | None = None,
    model: str | None = None,
    eval_timeout: float = _DEFAULT_EVAL_TIMEOUT,
    doer_timeout: float = cli_doer._DOER_TIMEOUT_SECONDS,
    max_wire_bytes: int = cli_doer._DEFAULT_MAX_WIRE_BYTES,
    project_context_text: str = "",
    ledger_path: Path | None = None,
) -> RatchetResult:
    """Climb ``evaluator_cmd``'s metric autonomously in a worktree, keeping only gains.

    Args:
        engine: the proposing doer - ``"gpt"`` (Codex) or ``"grok"`` (agentic patch).
        task: what to optimize, in natural language.
        evaluator_cmd: a shell command run in the worktree that prints the objective
            number (e.g. ``"pytest -q 2>&1 | tail -1"`` or a benchmark script). It
            executes whatever the engine wrote into the worktree, so it runs confined
            (see the module docstring): scrubbed environment, ephemeral ``HOME``, and
            under bwrap no network and nothing readable from the real home — tools it
            needs must be system-installed or inside the worktree.
        metric_regex: capture the metric with group 1; default takes the last number.
        goal: ``"minimize"`` (default) or ``"maximize"``.
        budget: hard cap on iterations.
        footprint: advisory/enforced scope for Grok's patch; Codex is sandbox-bounded.
        max_wire_bytes: cap on task + tracked worktree bytes the Codex doer may expose
            per iteration (as :func:`cli_doer.run_codex_doer`).
        project_context_text: the egress-gate context; when empty it is derived from
            ``repo_root/.cohort/project_context.md`` so an opted-out repo cannot be
            shipped by omitting the kwarg (#237).
        ledger_path: where to write the append-only staircase (TSV); defaults under the
            worktree.

    Raises:
        RatchetError: empty task/evaluator, unknown engine, or the baseline could not be
            measured (a metric you cannot even read is not one you can climb).
        EgressBlockedError / SecretFoundError / PayloadTooLargeError: gated before any
            engine call (the latter two also before every Codex dispatch).
    """
    if not task.strip():
        raise RatchetError("task is empty")
    if not evaluator_cmd.strip():
        raise RatchetError("evaluator command is empty (there is no metric to climb)")
    if goal not in ("minimize", "maximize"):
        raise RatchetError("goal must be 'minimize' or 'maximize'")
    if budget < 1:
        raise RatchetError("budget must be at least 1 iteration")

    project_context_text = cli_doer._egress_gate_text(repo_root, project_context_text)
    gates.require_egress_allowed(project_context_text)
    gates.assert_no_secrets(task)

    worktree = patch_proposal._create_worktree(repo_root)
    try:
        baseline, _ = _evaluate(worktree, evaluator_cmd, metric_regex, eval_timeout)
        if baseline is None:
            raise RatchetError(
                "could not read a baseline metric from the evaluator - check that "
                f"{evaluator_cmd!r} prints a number (or pass --metric-regex)"
            )
        best = baseline
        steps: list[RatchetStep] = []
        # The ledger lives OUTSIDE the git worktree (in its parent temp dir): a keep's
        # `git add -A` must not track it, and a revert's `git reset --hard`/`git clean`
        # must not touch it — the file is held open across the whole loop, and on Windows
        # git cannot modify an open file. Keeping it out also leaves the reviewed worktree
        # diff to the actual code change alone.
        ledger = ledger_path or (worktree.parent / "ratchet-results.tsv")
        ledger.write_text("iteration\tmetric\tkept\tnote\n", encoding="utf-8")
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(f"0\t{baseline}\tbaseline\tbaseline\n")

            for i in range(1, budget + 1):
                recent = "; ".join(
                    f"i{s.iteration}={s.metric}({'kept' if s.kept else 'reverted'})"
                    for s in steps[-5:]
                )
                iter_task = (
                    f"{task.strip()}\n\n"
                    f"This is an optimization loop. The objective is to {goal} the metric "
                    f"reported by the evaluator. Current best: {best}. Recent attempts: "
                    f"{recent or 'none'}. Propose ONE focused, surgical change likely to "
                    f"improve the metric - keep it minimal, do not rewrite broadly."
                )
                try:
                    _propose_into_worktree(
                        engine, iter_task, worktree, repo_root=repo_root, model=model,
                        footprint=footprint, project_context_text=project_context_text,
                        timeout=doer_timeout, max_wire_bytes=max_wire_bytes,
                    )
                except (gates.GateError, RatchetError):
                    raise
                except BaseException as exc:  # a doer failure is a failed experiment
                    _revert(worktree)
                    note = f"proposal failed: {type(exc).__name__}"
                    steps.append(RatchetStep(i, None, False, note))
                    fh.write(f"{i}\t\treverted\t{note}\n")
                    fh.flush()
                    continue

                metric, _ = _evaluate(worktree, evaluator_cmd, metric_regex, eval_timeout)
                improved = _is_improvement(metric, best, goal)
                try:
                    if improved:
                        _keep(worktree, f"ratchet i={i} metric={metric}")
                    else:
                        _revert(worktree)
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                    # A hung or failed git is a failed iteration, not a stalled loop: put
                    # the worktree back (best effort — the same git may fail again) and
                    # carry on; ``best`` is untouched, so the ratchet still only advances
                    # on a measured gain over it.
                    improved = False
                    note = f"git failed: {type(exc).__name__}"
                    try:
                        _revert(worktree)
                    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                        pass
                else:
                    note = (
                        "kept (improved)" if improved
                        else "no metric" if metric is None else "no improvement"
                    )
                if improved:
                    best = metric
                    fh.write(f"{i}\t{metric}\tkept\timproved\n")
                else:
                    fh.write(f"{i}\t{metric if metric is not None else ''}\treverted\t{note}\n")
                steps.append(RatchetStep(i, metric, improved, note))
                fh.flush()

        return RatchetResult(
            worktree=worktree, engine=engine, goal=goal, baseline=baseline,
            best=best, steps=steps, ledger_path=ledger,
        )
    except BaseException:
        # Setup/baseline failure or interrupt before any keep - never leak the worktree.
        # (On success the worktree is left in place for review.)
        if not any(getattr(s, "kept", False) for s in locals().get("steps", [])):
            patch_proposal.cleanup_worktree(repo_root, worktree)
        raise

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
Grok's egress-gated agentic patch. The **evaluator command is user-supplied and trusted**
(their own test/benchmark, like AutoResearch's immutable ``prepare.py``); it runs in the
worktree, and the doer can never edit it into lying because the doer only ever touches the
worktree's tracked code, which the human reviews.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from cohort.engines import gates, patch
from cohort.engines import cli_doer, patch_proposal, xai_agentic

# A change is "kept" only if the metric strictly moved the right way; ties revert, so the
# lineage only advances on a real gain (Karpathy's ratchet).
_CODEX_ENGINES = frozenset({"gpt", "chatgpt", "codex", "openai"})
_DEFAULT_EVAL_TIMEOUT = 300.0  # seconds per evaluator run


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


def _evaluate(
    worktree: Path, evaluator_cmd: str, metric_regex: str | None, timeout: float
) -> tuple[float | None, str]:
    """Run the user's evaluator in the worktree and parse the metric. A crash or timeout
    yields ``None`` (a failed experiment, reverted like any non-improvement)."""
    try:
        proc = subprocess.run(
            evaluator_cmd, shell=True, cwd=str(worktree),
            capture_output=True, text=True, timeout=timeout,
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
    model: str | None,
    footprint: list[str] | None,
    project_context_text: str,
    timeout: float,
) -> None:
    """Have the engine propose+apply one change to the *existing* worktree.

    Codex edits it directly under its sandbox; Grok explores read-only then Cohort applies
    its gated patch. Either way the change lands only in the worktree."""
    name = engine.strip().lower()
    if name in _CODEX_ENGINES:
        cli_doer.run_codex_in_worktree(worktree, task, model=model, timeout=timeout)
        return
    if name == "grok":
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
        f"engine {engine!r} has no ratchet doer (use 'gpt' or 'grok')"
    )


def _git(worktree: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True)


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
    project_context_text: str = "",
    ledger_path: Path | None = None,
) -> RatchetResult:
    """Climb ``evaluator_cmd``'s metric autonomously in a worktree, keeping only gains.

    Args:
        engine: the proposing doer - ``"gpt"`` (Codex) or ``"grok"`` (agentic patch).
        task: what to optimize, in natural language.
        evaluator_cmd: a shell command run in the worktree that prints the objective
            number (e.g. ``"pytest -q 2>&1 | tail -1"`` or a benchmark script). Trusted.
        metric_regex: capture the metric with group 1; default takes the last number.
        goal: ``"minimize"`` (default) or ``"maximize"``.
        budget: hard cap on iterations.
        footprint: advisory/enforced scope for Grok's patch; Codex is sandbox-bounded.
        ledger_path: where to write the append-only staircase (TSV); defaults under the
            worktree.

    Raises:
        RatchetError: empty task/evaluator, unknown engine, or the baseline could not be
            measured (a metric you cannot even read is not one you can climb).
        EgressBlockedError / SecretFoundError: gated before any engine call.
    """
    if not task.strip():
        raise RatchetError("task is empty")
    if not evaluator_cmd.strip():
        raise RatchetError("evaluator command is empty (there is no metric to climb)")
    if goal not in ("minimize", "maximize"):
        raise RatchetError("goal must be 'minimize' or 'maximize'")
    if budget < 1:
        raise RatchetError("budget must be at least 1 iteration")

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
                        engine, iter_task, worktree, model=model, footprint=footprint,
                        project_context_text=project_context_text, timeout=doer_timeout,
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
                if _is_improvement(metric, best, goal):
                    _keep(worktree, f"ratchet i={i} metric={metric}")
                    best = metric
                    steps.append(RatchetStep(i, metric, True, "kept (improved)"))
                    fh.write(f"{i}\t{metric}\tkept\timproved\n")
                else:
                    _revert(worktree)
                    note = "no metric" if metric is None else "no improvement"
                    steps.append(RatchetStep(i, metric, False, note))
                    fh.write(f"{i}\t{metric if metric is not None else ''}\treverted\t{note}\n")
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

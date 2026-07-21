"""External-CLI doers — dispatch a vendor's own agentic CLI as a *write* doer, confined
to an isolated git worktree.

Where :mod:`cohort.engines.patch_proposal` has the engine return a diff that Cohort
applies, a CLI doer lets the vendor's own agentic loop edit files directly — it can
iterate and run its own tests. Safety rests on OS-level confinement plus a throwaway
worktree, never on trusting the engine:

* **Codex (ChatGPT)** runs under its own sandbox — ``codex exec --sandbox
  workspace-write -C <worktree>`` — so every model-generated write and shell command is
  confined to the worktree by the OS (Landlock/seccomp on Linux, Seatbelt on macOS).
* The worktree is a **detached checkout off HEAD** — committed files only, so no
  untracked secret (a git-ignored ``.env``) is ever exposed — and it is discarded if the
  run is rejected.
* The task **egresses to the vendor**, so the repo's egress opt-out and a secret scan on
  the task text gate the dispatch *before* the CLI is invoked.
* Nothing is committed or merged here. The coordinator reviews the diff and integrates;
  the human PR-reviews. A change that lands outside a declared footprint is surfaced.

**grok-cli is deliberately not wired.** The installed v1.0.1 is broken against the live
xAI API (HTTP 410, a removed "live search" endpoint) and, unlike Codex, has no sandbox,
read-only, or approval mode. Grok's contained write path is the gated agentic
patch_proposal (``cohort engine propose grok --agentic``).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from cohort.engines import gates
from cohort.engines import patch_proposal

# A hard wall-clock cap on an external doer run — an agentic CLI that has not finished
# in this long is stuck or looping; kill it (the worktree is cleaned up on timeout).
_DOER_TIMEOUT_SECONDS: float = 900.0

# Engine names that resolve to the Codex-sandboxed doer.
_CODEX_ENGINE_ALIASES = frozenset({"gpt", "chatgpt", "codex", "openai"})


class DoerError(Exception):
    """Base class for CLI-doer failures."""


class DoerUnavailableError(DoerError):
    """The requested vendor CLI is not installed, or the engine has no CLI doer.

    Grok raises this on purpose: its CLI is non-functional, so callers are pointed at
    the agentic patch_proposal instead."""


@dataclass
class DoerResult:
    """The outcome of a CLI-doer run. The worktree is left in place for the coordinator
    to review and integrate; ``changed_files``/``diff`` are what the engine wrote."""

    engine: str
    worktree: Path
    changed_files: list[str]
    diff: str
    returncode: int
    stdout_tail: str
    footprint_violations: list[str] = field(default_factory=list)


def _git(worktree: Path, *args: str) -> str:
    """Run a read-only-ish git query inside the worktree and return stdout."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _footprint_violations(changed: list[str], footprint: list[str] | None) -> list[str]:
    """Report changed paths outside a declared footprint (or in a sensitive class).

    Advisory, not enforcing: the OS sandbox already confines writes to the worktree, so
    this surfaces "the engine edited something you didn't scope it to" for the human
    reviewer, rather than blocking (the engine already ran)."""
    if not footprint:
        return []
    return gates.check_changed_paths(changed, allowed_footprint=footprint)


def run_codex_doer(
    task: str,
    *,
    repo_root: Path,
    model: str | None = None,
    footprint: list[str] | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
    project_context_text: str = "",
) -> DoerResult:
    """Dispatch Codex (ChatGPT) as a write doer confined to a fresh worktree.

    Raises:
        DoerError: empty task.
        DoerUnavailableError: the ``codex`` CLI is not installed.
        EgressBlockedError / SecretFoundError: the repo opted out of egress, or the task
            contains credential-shaped content (gated before the CLI is invoked).
    """
    if not task.strip():
        raise DoerError("task is empty")

    # Gate the outbound task BEFORE spawning the CLI: honor the repo egress opt-out and
    # refuse a task that carries a secret. (The CLI then reads only committed worktree
    # files, and its writes are OS-confined to the worktree.)
    gates.require_egress_allowed(project_context_text)
    gates.assert_no_secrets(task)

    if shutil.which("codex") is None:
        raise DoerUnavailableError(
            "the 'codex' CLI is not installed; install it and run 'codex login' to use "
            "the Codex worktree doer"
        )

    worktree = patch_proposal._create_worktree(repo_root)
    try:
        cmd = [
            "codex", "exec",
            "--sandbox", "workspace-write",  # writes/commands confined to the worktree
            "-C", str(worktree),
            "--skip-git-repo-check",
        ]
        if model:
            cmd += ["-m", model]
        cmd.append(task)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # Capture what the engine wrote as a reviewable diff.
        _git(worktree, "add", "-A")
        diff = _git(worktree, "diff", "--cached")
        changed = [n for n in _git(worktree, "diff", "--cached", "--name-only").splitlines() if n]
        return DoerResult(
            engine="gpt",
            worktree=worktree,
            changed_files=changed,
            diff=diff,
            returncode=proc.returncode,
            stdout_tail=(proc.stdout or "")[-2000:],
            footprint_violations=_footprint_violations(changed, footprint),
        )
    except BaseException:
        # TimeoutExpired, a git failure, or Ctrl-C: never leak the worktree.
        patch_proposal.cleanup_worktree(repo_root, worktree)
        raise


def run_doer(engine: str, task: str, **kwargs) -> DoerResult:
    """Dispatch ``engine``'s CLI doer. Codex-family engines are sandboxed; Grok is
    refused (its CLI is broken/unsandboxed) with a pointer to the agentic proposer."""
    name = engine.strip().lower()
    if name in _CODEX_ENGINE_ALIASES:
        return run_codex_doer(task, **kwargs)
    if name == "grok":
        raise DoerUnavailableError(
            "grok has no sandboxed CLI doer (grok-cli is broken against the live API and "
            "unsandboxed); use 'cohort engine propose grok --agentic' for Grok's gated, "
            "worktree-confined write path"
        )
    raise DoerUnavailableError(f"no CLI doer for engine {engine!r}")

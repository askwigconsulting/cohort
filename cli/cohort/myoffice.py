"""`cohort my-office sync` — back the personal layer with a Git repo (#101).

The office tier already points back to a shared repo (the `[update]` upstream)
and a project's settings travel with its consuming repo, but *my office*
(`~/.cohort/my`) is a plain directory — so personal agents/skills/settings don't
follow you across machines. This makes it a Git repo with a configured remote
and syncs it (commit → fast-forward pull → push), then recompiles so anything
pulled is placed. All git is non-interactive and hard-timeout'd (gitutil).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from .gitutil import GIT_ENV, GIT_TIMEOUT
from .install_model import CohortPaths

_BRANCH = "main"


class MySyncError(Exception):
    """A refused ``cohort my-office sync`` (no remote, diverged, git failure)."""


def _git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[Optional[int], str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "-c", "credential.helper=", *args],
            capture_output=True, text=True, timeout=timeout, env={**os.environ, **GIT_ENV},
        )
        return proc.returncode, proc.stdout.strip()
    except Exception:  # noqa: BLE001 - offline, timeout, git absent
        return None, ""


def my_remote(home: Path) -> Optional[str]:
    """The configured personal-layer sync remote URL, or None."""
    my = CohortPaths.for_global(home).my
    if not (my / ".git").exists():
        return None
    rc, url = _git(my, "remote", "get-url", "origin")
    return url or None if rc == 0 else None


def _ensure_repo(my: Path) -> None:
    my.mkdir(parents=True, exist_ok=True)
    if not (my / ".git").exists():
        _git(my, "init", "-q", "-b", _BRANCH)
        # Author identity: fall back to a Cohort identity if the user has none.
        if _git(my, "config", "user.email")[1] == "":
            _git(my, "config", "user.email", "cohort@localhost")
        if _git(my, "config", "user.name")[1] == "":
            _git(my, "config", "user.name", "Cohort")
        # NB: no bootstrap commit here. A local commit made before the first
        # fetch orphans the branch from the remote's history, so a second
        # machine could never fast-forward-adopt the shared office. The
        # .gitignore is written in do_my_sync *after* reconciling with origin.


def do_my_sync(
    home: Path, *, remote: Optional[str] = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Sync ``~/.cohort/my`` with its Git remote and recompile.

    With ``remote``, (re)configures the sync remote first. Commits local changes,
    fetches, fast-forwards from the remote (refuses a diverged history — reconcile
    by hand), pushes, then recompiles the office so anything pulled is placed."""
    my = CohortPaths.for_global(home).my
    if dry_run:
        return {"action": "my-sync", "dry_run": True, "remote": remote or my_remote(home),
                "plan": ["ensure git repo", "set remote" if remote else "use remote",
                         "fetch + ff-pull", "commit local", "push", "recompile"]}
    _ensure_repo(my)
    if remote:
        _git(my, "remote", "remove", "origin")  # idempotent: ignore "no such remote"
        rc, _ = _git(my, "remote", "add", "origin", "--", remote)
        if rc != 0:
            raise MySyncError(f"could not set remote to {remote!r}")
    url = my_remote(home)
    if not url:
        raise MySyncError("no sync remote configured — run `cohort my-office sync --remote <url>`")

    # Reconcile with the remote BEFORE committing anything local, so a fresh
    # machine (whose branch is still unborn) fast-forward-adopts the shared
    # history instead of colliding with it. Local personal files stay untracked
    # across the merge and are committed on top afterwards.
    fetched = _git(my, "fetch", "--quiet", "--", "origin", timeout=30)[0] == 0
    pulled = False
    if fetched:
        # Only fast-forward: a diverged personal history is the user's to reconcile.
        has_remote_branch = _git(my, "rev-parse", "--verify", f"origin/{_BRANCH}")[0] == 0
        if has_remote_branch:
            unborn = _git(my, "rev-parse", "--verify", "HEAD")[0] != 0
            before = _git(my, "rev-parse", "HEAD")[1]
            rc, _ = _git(my, "merge", "--ff-only", "--", f"origin/{_BRANCH}", timeout=30)
            if rc != 0:
                if unborn:
                    raise MySyncError(
                        "a personal file conflicts with one already in your synced "
                        f"office — move it aside in {my} and re-run sync"
                    )
                raise MySyncError(
                    "my office has diverged from its remote — reconcile "
                    f"{my} by hand (git pull --rebase), then re-run sync"
                )
            pulled = before != _git(my, "rev-parse", "HEAD")[1]

    # A .gitignore only if the (now reconciled) office doesn't already carry one.
    gitignore = my / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# personal layer\n", encoding="utf-8")

    # Commit local changes on top of the reconciled history (no-op commit is skipped).
    _git(my, "add", "-A")
    _git(my, "commit", "-q", "-m", "cohort: sync my office")  # ignores "nothing to commit"

    pushed = _git(my, "push", "--quiet", "-u", "origin", _BRANCH, timeout=30)[0] == 0

    recompiled = _recompile_if_installed(home)
    return {"action": "my-sync", "dry_run": False, "remote": url,
            "fetched": fetched, "pulled": pulled, "pushed": pushed, "recompiled": recompiled}


def _recompile_if_installed(home: Path) -> bool:
    """Recompile the global Claude tier so a pulled personal artifact is placed.
    A no-op (returns False) when nothing is installed."""
    from .manifest import load_manifest
    from .roster import recompile_global_claude
    from .source import resolve_source_lenient

    paths = CohortPaths.for_global(home)
    if load_manifest(paths.manifest) is None:
        return False
    source = resolve_source_lenient(home)
    if source is None:
        return False
    try:
        recompile_global_claude(home, source)
        return True
    except Exception:  # noqa: BLE001 - sync succeeded; a recompile hiccup isn't fatal
        return False

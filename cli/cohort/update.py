"""Cohort self-update substrate: detect when the local clone is behind upstream.

Advisory only — this module never pulls, merges, or recompiles (that is ``/update``,
Phase 2). The session-start hook calls :func:`do_update_check`; it MUST never raise,
never block the session, and the command wrapper exits 0 always.

We use ``git fetch`` + ``rev-list --count`` rather than ``git ls-remote`` because an
exact "N commits behind" needs the upstream objects locally (ls-remote returns only
the remote tip SHA). ``fetch`` updates only remote-tracking refs — never the working
tree, never a merge — and is run fully non-interactively with a hard timeout so it
cannot prompt for credentials or hang a session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .install_model import CohortPaths
from .source import SourceUnresolved, resolve_source

# Force git fully non-interactive: never prompt for credentials/host keys; fail
# fast when offline. (``--quiet`` only silences progress — it does NOT stop prompts.)
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=5",
}
_FETCH_TIMEOUT = 8  # seconds; a flaky network must not stall session startup
_GIT_TIMEOUT = 10
_PULL_TIMEOUT = 30  # a local ff-only merge is fast, but allow headroom for a big one
_DEFAULT_BRANCH = "main"


def _git(source: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[Optional[int], str]:
    """Run a git command in ``source``; return ``(returncode, stdout)`` or
    ``(None, "")`` on timeout/missing-git/any failure. Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(source), "-c", "credential.helper=", *args],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **_GIT_ENV},
        )
        return proc.returncode, proc.stdout.strip()
    except Exception:  # noqa: BLE001 - offline, timeout, git absent, …
        return None, ""


def _read_update_config(home: Path) -> tuple[Optional[str], Optional[str]]:
    """``(upstream_remote, upstream_branch)`` from the global ``cohort.toml``
    ``[update]`` table, or ``(None, None)``. Never raises."""
    cfg = CohortPaths(home).cohort_home / "cohort.toml"
    try:
        import tomllib  # 3.11+; absent on 3.10 → fall back to defaults

        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        upd = data.get("update", {}) if isinstance(data, dict) else {}
        return upd.get("upstream_remote"), upd.get("upstream_branch")
    except Exception:  # noqa: BLE001 - missing file, parse error, old python
        return None, None


def resolve_upstream(source: Path, home: Path) -> tuple[str, str]:
    """The ``(remote, branch)`` to compare against. Config overrides win; else
    ``origin`` + the remote's default branch (only when ``symbolic-ref`` resolves
    it cleanly), else ``main``."""
    remote_cfg, branch_cfg = _read_update_config(home)
    remote = remote_cfg or "origin"
    if branch_cfg:
        return remote, branch_cfg
    # symbolic-ref --quiet exits non-zero with empty stdout when origin/HEAD is
    # unset (unlike rev-parse --abbrev-ref, which prints "origin/HEAD" on failure).
    rc, out = _git(source, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD")
    prefix = f"refs/remotes/{remote}/"
    if rc == 0 and out.startswith(prefix):
        branch = out[len(prefix):]
        if branch and branch != "HEAD":
            return remote, branch
    return remote, _DEFAULT_BRANCH


def update_status(source: Path, home: Path) -> dict:
    """Best-effort: how far the clone is behind upstream. Never raises.

    Returns ``{available, behind, diverged, current, upstream}``. ``available`` is
    False whenever the answer is indeterminate (offline, no remote, detached HEAD,
    bad/shallow ref) — callers must not advise in that case.
    """
    remote, branch = resolve_upstream(source, home)
    upstream = f"{remote}/{branch}"
    unavailable = {"available": False, "upstream": upstream}

    # Detached HEAD (CI checkout, bisect, viewing a tag) → no branch to compare.
    rc, head = _git(source, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0 or not head:
        return unavailable

    # ``--`` terminates option parsing: a config-supplied remote that looks like
    # an option (e.g. ``--upload-pack=<cmd>``) must be treated as a remote name,
    # never a git flag — otherwise a tampered global cohort.toml yields RCE.
    rc, _ = _git(source, "fetch", "--quiet", "--", remote, timeout=_FETCH_TIMEOUT)
    if rc != 0:
        return unavailable  # offline / auth-required / no such remote

    rc, count = _git(source, "rev-list", "--count", f"HEAD..{upstream}")
    if rc != 0 or not count.isdigit():
        return unavailable  # bad ref, shallow clone, etc. — don't int("") -> raise
    behind = int(count)

    # Only a clean fast-forward (HEAD is an ancestor of upstream) is a plain
    # "you're behind"; a diverged history must not advise a simple /update.
    rc, _ = _git(source, "merge-base", "--is-ancestor", "HEAD", upstream)
    diverged = rc != 0
    rc2, current = _git(source, "rev-parse", "--short", "HEAD")
    return {
        "available": True,
        "behind": behind,
        "diverged": diverged,
        "current": current if rc2 == 0 else "",
        "upstream": upstream,
    }


def advisory_message(status: dict) -> Optional[str]:
    """The one-line advisory when a clean upgrade is available, else None."""
    if not status.get("available") or status.get("diverged"):
        return None
    behind = status.get("behind", 0)
    if behind <= 0:
        return None
    plural = "s" if behind != 1 else ""
    return (
        f"cohort: {behind} commit{plural} behind {status['upstream']} — "
        f"run /update to upgrade."
    )


def _throttle_marker(home: Path) -> Path:
    return CohortPaths(home).state / ".update-checked"


def _throttled_today(home: Path, today: str) -> bool:
    marker = _throttle_marker(home)
    try:
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == today
    except Exception:  # noqa: BLE001 - corrupt/unreadable marker → re-check
        return False


def _mark_checked(home: Path, today: str) -> None:
    marker = _throttle_marker(home)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)  # state/ may not exist yet
        marker.write_text(today, encoding="utf-8")
    except Exception:  # noqa: BLE001 - best-effort throttle; never block
        pass


def do_update_check(home: Path, *, now: Optional[datetime] = None) -> Optional[str]:
    """Advisory string if a newer Cohort is available, else None. Never raises.

    Throttles the *network check* to once per UTC day per machine (one fetch/day),
    accepting that an upstream bump later the same day surfaces the next day. The
    marker is written only after a successful (available) check, so offline
    sessions retry rather than burning the daily slot.
    """
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    try:
        source = resolve_source()  # may raise SourceUnresolved (site-packages, no clone)
    except SourceUnresolved:
        return None
    if _throttled_today(home, today):
        return None
    status = update_status(source, home)
    if not status.get("available"):
        return None
    _mark_checked(home, today)
    return advisory_message(status)


# --- Phase 2: the explicit `cohort update` command -------------------------
#
# Advisory-only updates are Phase 1; this applies one. The contract: never touch a
# dirty or diverged tree, only ever fast-forward (never synthesize a merge commit),
# reinstall the package only when its deps change, and recompile exactly the IDEs
# the install manifest records. Nothing changes on a dry run.

PipRunner = Callable[[list], int]


@dataclass
class UpdateResult:
    """Outcome of :func:`do_update`. ``status`` drives the CLI exit code.

    Statuses: ``up_to_date`` / ``dry_run`` / ``updated`` (success, exit 0);
    ``unavailable`` / ``diverged`` / ``dirty`` / ``pull_failed`` / ``pip_failed`` /
    ``recompile_refused`` (refused/failed, exit 1).
    """

    status: str
    upstream: str = ""
    behind: int = 0
    current: str = ""
    target: str = ""
    commits: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)
    pip_reinstalled: bool = False
    recompiled_ides: list = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("up_to_date", "dry_run", "updated")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "upstream": self.upstream,
            "behind": self.behind,
            "current": self.current,
            "target": self.target,
            "commits": list(self.commits),
            "changed_files": list(self.changed_files),
            "pip_reinstalled": self.pip_reinstalled,
            "recompiled_ides": list(self.recompiled_ides),
            "detail": self.detail,
        }


def _is_dirty(source: Path) -> bool:
    """True if the working tree has any uncommitted change — modified tracked files
    OR untracked files (``--porcelain`` lists both; it only omits ``.gitignore``-d
    paths, so Cohort's own .cohort/.claude artifacts are excluded). A failed status
    read counts as dirty — refuse rather than risk it."""
    rc, out = _git(source, "status", "--porcelain")
    return rc != 0 or bool(out.strip())


def _incoming_commits(source: Path, upstream: str) -> list:
    """One-line summaries of the commits ``HEAD..upstream`` would bring in. Includes
    merges so the count matches ``behind`` from ``update_status``. ``upstream`` is
    embedded mid-token (``HEAD..<ref>``) so it can never be read as a git option."""
    rc, out = _git(source, "log", "--oneline", f"HEAD..{upstream}")
    return out.splitlines() if rc == 0 and out else []


def _changed_files(source: Path, upstream: str) -> list:
    """Repo-relative paths changed between HEAD and the upstream tip."""
    rc, out = _git(source, "diff", "--name-only", f"HEAD..{upstream}")
    return out.splitlines() if rc == 0 and out else []


def _default_pip_run(args: list) -> int:
    """Run a pip command, returning its exit code. Isolated so tests fake it."""
    return subprocess.run(args, capture_output=True, text=True).returncode


def _recompile_installed(source: Path, home: Path) -> tuple:
    """Recompile + reinstall every IDE the manifest records, in its install mode.

    Returns ``(recompiled_ides, refused_detail)``. ``refused_detail`` is set when
    ``do_install`` hits a foreign file at a managed path — we never auto-``--force``
    during an update the user didn't opt into. A missing manifest (no install yet)
    recompiles nothing.
    """
    from .compile import CompileError, compile_ide, write_staging
    from .executor import ClobberRefused
    from .install import do_install
    from .install_model import resolve_mode
    from .manifest import load_manifest

    paths = CohortPaths(home)
    ides: list = []
    # Everything here runs AFTER the fast-forward has applied, so it must never
    # raise: a corrupt manifest (load_manifest), a malformed pulled tree
    # (CompileError), a foreign file (ClobberRefused), or an OS error
    # (read-only FS, full disk) all degrade to a refused_detail.
    try:
        manifest = load_manifest(paths.manifest)
        ides = list(manifest.ides) if manifest else []
        if not ides:
            return [], None
        mode = manifest.mode if (manifest and manifest.mode) else resolve_mode(copy=False)
        for ide in ides:
            write_staging(paths, compile_ide(source, ide))
        do_install(home=home, selection=ides, mode=mode, force=False, source=source, dry_run=False)
    except ClobberRefused as exc:
        return ides, str(exc)
    except CompileError as exc:
        return ides, f"the updated canonical tree failed to compile: {exc}"
    except Exception as exc:  # noqa: BLE001 - do_update must not raise once the merge applied
        return ides, f"recompile failed after the update applied: {exc}"
    return ides, None


def do_update(
    source: Path,
    home: Path,
    *,
    dry_run: bool = False,
    pip_run: Optional[PipRunner] = None,
) -> UpdateResult:
    """Apply a pending Cohort update: fast-forward the clone, reinstall the package
    if its deps changed, and recompile installed IDEs. Refuses on a dirty/diverged
    tree; previews only on ``dry_run``. ``pip_run`` is injectable for tests.
    """
    pip_run = pip_run or _default_pip_run
    status = update_status(source, home)  # fetches; yields behind/diverged/upstream
    upstream = status.get("upstream", "")
    if not status.get("available"):
        return UpdateResult(
            status="unavailable", upstream=upstream,
            detail="Upstream is unreachable or the checkout can't be compared "
            "(offline, detached HEAD, no remote, or shallow clone).",
        )
    current = status.get("current", "")
    if status.get("diverged"):
        return UpdateResult(
            status="diverged", upstream=upstream, current=current,
            detail="Local history has diverged from upstream; reconcile (rebase or "
            "merge) before updating.",
        )
    behind = int(status.get("behind", 0))
    if behind <= 0:
        return UpdateResult(status="up_to_date", upstream=upstream, current=current, behind=0)

    if _is_dirty(source):
        return UpdateResult(
            status="dirty", upstream=upstream, current=current, behind=behind,
            detail="Working tree has uncommitted changes; commit or stash them "
            "before running cohort update.",
        )

    # Summarize against the just-fetched tracking ref — the exact ref merged below.
    commits = _incoming_commits(source, upstream)
    changed_files = _changed_files(source, upstream)
    _, target = _git(source, "rev-parse", "--short", upstream)

    if dry_run:
        return UpdateResult(
            status="dry_run", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
        )

    # Fast-forward only: refuses (rc != 0) rather than ever creating a merge commit.
    # ``--`` keeps this call self-defending — an option-like upstream ref can never
    # be read as a git flag here, independent of the fetch gate in update_status.
    rc, _ = _git(source, "merge", "--ff-only", "--", upstream, timeout=_PULL_TIMEOUT)
    if rc != 0:
        return UpdateResult(
            status="pull_failed", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
            detail="git merge --ff-only failed (the tree changed under us). "
            "Run `git status` in the Cohort source and retry.",
        )

    pip_reinstalled = False
    if "pyproject.toml" in changed_files:
        if pip_run([sys.executable, "-m", "pip", "install", "-e", str(source)]) != 0:
            return UpdateResult(
                status="pip_failed", upstream=upstream, behind=behind, current=current,
                target=target, commits=commits, changed_files=changed_files,
                detail="The fast-forward landed but `pip install -e` failed; run it "
                "manually from the Cohort source, then `cohort recompile`.",
            )
        pip_reinstalled = True

    recompiled, refused = _recompile_installed(source, home)
    if refused is not None:
        return UpdateResult(
            status="recompile_refused", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
            pip_reinstalled=pip_reinstalled, recompiled_ides=recompiled,
            detail="Updated the clone, but recompile found foreign files at a managed "
            "path. Run `cohort recompile --force` to back up and replace them. " + refused,
        )

    return UpdateResult(
        status="updated", upstream=upstream, behind=behind, current=current, target=target,
        commits=commits, changed_files=changed_files, pip_reinstalled=pip_reinstalled,
        recompiled_ides=recompiled,
    )

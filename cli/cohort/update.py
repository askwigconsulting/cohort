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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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

    rc, _ = _git(source, "fetch", "--quiet", remote, timeout=_FETCH_TIMEOUT)
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

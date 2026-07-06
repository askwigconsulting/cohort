"""`cohort my-office sync` — back the personal layer with a Git repo (#101).

The office tier already points back to a shared repo (the `[update]` upstream)
and a project's settings travel with its consuming repo, but *my office*
(`~/.cohort/my`) is a plain directory — so personal agents/skills/settings don't
follow you across machines. This makes it a Git repo with a configured remote
and syncs it (fast-forward pull → commit local → push — reconcile *before*
committing so a fresh machine adopts the shared history), then recompiles so
anything pulled is placed. All git is non-interactive and hard-timeout'd (gitutil).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import quarantine
from .gitutil import GIT_ENV, GIT_TIMEOUT
from .install_model import CohortPaths
from .manifest import now_iso
from .schema import KIND_DIRS

_BRANCH = "main"

# Passwords embedded in a remote URL (https://user:token@host) must never be
# echoed back to the terminal or a --json blob. Redact the password, keep the
# username. scp-style git@host:path has no "://" and is left untouched.
_URL_PASSWORD = re.compile(r"(://[^/@:]+):[^/@]*@")

# A real personal-layer .gitignore: the layer holds settings/hooks, so exclude
# the common credential-bearing files rather than sweeping them to the remote.
_GITIGNORE = (
    "# Cohort personal layer — synced across your machines via `cohort my-office sync`.\n"
    "# Never sync secrets: exclude common credential-bearing files.\n"
    ".env\n.env.*\n*.pem\n*.key\n*.p12\nid_rsa*\nid_ed25519*\n.netrc\n"
    "credentials.json\n*.credentials\n*_token\n*.token\n"
)


def _redact_url(url: Optional[str]) -> Optional[str]:
    """A remote URL safe to display: any embedded password is masked."""
    return _URL_PASSWORD.sub(r"\1:***@", url) if url else url


class MySyncError(Exception):
    """A refused ``cohort my-office sync`` (no remote, diverged, git failure)."""


def _git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[Optional[int], str]:
    try:
        proc = subprocess.run(
            # ext::/fd:: transports run an arbitrary command as the "transport";
            # a crafted --remote could execute it on the first fetch. Ban them
            # (leaving file/https/ssh) so a pasted URL can't be a code path.
            ["git", "-C", str(cwd), "-c", "credential.helper=",
             "-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never", *args],
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
        # NB: no bootstrap commit here. A local commit made before the first
        # fetch orphans the branch from the remote's history, so a second
        # machine could never fast-forward-adopt the shared office. The
        # .gitignore is written in do_my_sync *after* reconciling with origin.
    # Author identity fallback, applied every sync (not only on fresh init): a
    # pre-existing ~/.cohort/my repo with no configured identity would otherwise
    # fail the commit — silently, since _git swallows it — and never push.
    if _git(my, "config", "user.email")[1] == "":
        _git(my, "config", "user.email", "cohort@localhost")
    if _git(my, "config", "user.name")[1] == "":
        _git(my, "config", "user.name", "Cohort")


def _all_gated_paths(my: Path) -> list[Path]:
    """Every gated (hook/memory) canonical file currently in the personal layer."""
    canonical = my / "canonical"
    paths: list[Path] = []
    for kind in quarantine.GATED_KINDS:
        d = canonical / KIND_DIRS[kind]
        if d.exists():
            paths.extend(sorted(d.glob("*.md")))
    return paths


def _record_pulled_gated(
    my: Path, state_dir: Path, *, before: str, after: str, unborn: bool
) -> list[quarantine.QuarantinedArtifact]:
    """Quarantine the gated artifacts a pull introduced or changed (#107).

    The window is the pull itself (``before..after``); local authoring is committed
    *after* the merge, so it is never in this delta and never quarantined —
    distinguishing pulled content from the user's own without git archaeology. A
    fresh adopt (unborn branch) quarantines every gated artifact in the adopted
    history. Fail closed: if the diff cannot be computed, quarantine *every* gated
    artifact present rather than risk activating an unreviewed one.

    Identity is the on-disk file's content hash, so approving pins the exact bytes.
    """
    # Persist the record even if nothing is installed yet: a later install's first
    # recompile must still withhold this pull, not silently activate it.
    state_dir.mkdir(parents=True, exist_ok=True)
    if unborn or not before:
        changed = _all_gated_paths(my)
    else:
        gated_dirs = [f"canonical/{KIND_DIRS[k]}" for k in quarantine.GATED_KINDS]
        rc, out = _git(my, "diff", "--name-only", "-z", before, after, "--", *gated_dirs)
        if rc != 0:  # a git hiccup must not silently activate a pulled sink
            changed = _all_gated_paths(my)
        else:
            changed = [my / rel for rel in out.split("\x00") if rel]
    items = []
    for path in changed:
        if not path.exists():  # deleted by the pull → nothing to place, nothing to gate
            continue
        kn = quarantine.gated_kind_and_name(path)
        if kn is None:
            continue
        kind, name = kn
        items.append(
            quarantine.QuarantinedArtifact(kind, name, quarantine.content_hash(path), now_iso())
        )
    return quarantine.add_pending(state_dir, items)


def do_my_sync(
    home: Path, *, remote: Optional[str] = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Sync ``~/.cohort/my`` with its Git remote and recompile.

    With ``remote``, (re)configures the sync remote first. Fetches and
    fast-forwards from the remote *before* committing local changes (so a fresh
    machine's unborn branch adopts the shared history; a diverged one is refused
    to reconcile by hand), commits local changes on top, pushes, then recompiles
    the office so anything pulled is placed.

    Whole-office caveat: the personal layer is pushed wholesale, so don't store
    secrets in ``~/.cohort/my`` — a default ``.gitignore`` excludes the common
    credential files but can't catch a token pasted into an agent/memory body."""
    my = CohortPaths.for_global(home).my
    if dry_run:
        return {"action": "my-sync", "dry_run": True,
                "remote": _redact_url(remote or my_remote(home)),
                "plan": ["ensure git repo", "set remote" if remote else "use remote",
                         "fetch + ff-pull", "commit local", "push", "recompile"]}
    _ensure_repo(my)
    if remote:
        _git(my, "remote", "remove", "origin")  # idempotent: ignore "no such remote"
        rc, _ = _git(my, "remote", "add", "origin", "--", remote)
        if rc != 0:
            raise MySyncError(f"could not set remote to {_redact_url(remote)}")
    url = my_remote(home)
    if not url:
        raise MySyncError("no sync remote configured — run `cohort my-office sync --remote <url>`")
    safe_url = _redact_url(url)

    # Reconcile with the remote BEFORE committing anything local, so a fresh
    # machine (whose branch is still unborn) fast-forward-adopts the shared
    # history instead of colliding with it. Local personal files stay untracked
    # across the merge and are committed on top afterwards.
    #
    # A failed fetch is FATAL, never a silent fall-through: on a fresh machine,
    # falling through would commit an orphan root on the unborn branch that no
    # later sync could ever fast-forward — one network blip would wedge it into
    # "diverged" forever. Distinguish this from an empty-but-reachable remote
    # (fetch succeeds, just has no `main` yet), which legitimately proceeds.
    if _git(my, "fetch", "--quiet", "--", "origin", timeout=30)[0] != 0:
        raise MySyncError(
            f"could not reach sync remote {safe_url} — check the URL, network, or access"
        )
    pulled = False
    pull_before: Optional[str] = None  # None → no origin/main, no pull attempted
    pull_after = ""
    pull_unborn = False
    # Only fast-forward: a diverged personal history is the user's to reconcile.
    if _git(my, "rev-parse", "--verify", f"origin/{_BRANCH}")[0] == 0:
        unborn = _git(my, "rev-parse", "--verify", "HEAD")[0] != 0
        before = "" if unborn else _git(my, "rev-parse", "HEAD")[1]
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
        pull_after = _git(my, "rev-parse", "HEAD")[1]
        pull_before, pull_unborn = before, unborn
        pulled = before != pull_after

    # A real .gitignore (secret-excluding) only if the reconciled office lacks one.
    gitignore = my / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")

    # Commit local changes on top of the reconciled history (no-op commit is skipped).
    _git(my, "add", "-A")
    _git(my, "commit", "-q", "-m", "cohort: sync my office")  # ignores "nothing to commit"

    # A failed push is FATAL too: a non-fast-forward rejection (the remote
    # advanced between our fetch and push) or an auth/network failure must not
    # read as success. Re-running fetches the advancing commit and retries.
    if _git(my, "push", "--quiet", "-u", "origin", _BRANCH, timeout=30)[0] != 0:
        raise MySyncError(
            f"push to {safe_url} failed — the remote may have advanced; re-run sync, "
            "or check access"
        )
    pushed = True

    # Quarantine any gated (hook/memory) artifact this pull introduced BEFORE the
    # recompile, so the recompile withholds it instead of activating it (#107). No
    # pull attempted (no origin/main yet) → nothing to record.
    newly_quarantined: list[quarantine.QuarantinedArtifact] = []
    if pull_before is not None:
        newly_quarantined = _record_pulled_gated(
            my, CohortPaths.for_global(home).state,
            before=pull_before, after=pull_after, unborn=pull_unborn,
        )

    recompiled = _recompile_if_installed(home)
    # We only reach here past a successful fetch and push (both raise on failure).
    return {"action": "my-sync", "dry_run": False, "remote": safe_url,
            "fetched": True, "pulled": pulled, "pushed": pushed, "recompiled": recompiled,
            "quarantined": [f"{a.kind} {a.name}" for a in newly_quarantined]}


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

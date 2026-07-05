"""`cohort status` — a strictly read-only aggregate of the install.

Reuses Phase-4's *read-only* freshness compute (never ``staleness_check``, which
writes the throttle marker, R1) and ``merge.extract_block`` for wiring detection
(R4). Reports the global install and, inside a Cohort repo, the project state.
Never mutates the filesystem.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from .gitutil import GIT_ENV, GIT_TIMEOUT

from . import merge
from .install_model import CohortPaths
from .inventory import inventory, inventory_summary
from .loader import load_artifact
from .manifest import load_manifest
from .source import resolve_source_lenient

RELINK_HINT = "cohort relink"


def _source_health(gpaths: CohortPaths) -> dict[str, Any]:
    """Health of the install's source link (``~/.cohort/canonical``). A dangling
    symlink (the source clone moved/was deleted) is the moved-install failure mode."""
    canonical = gpaths.canonical
    if not canonical.is_symlink():
        return {"ok": True, "linked": False}
    target = os.readlink(canonical)
    ok = canonical.exists()  # follows the link → False when dangling
    health = {"ok": ok, "linked": True, "target": target}
    if not ok:
        health["restore"] = RELINK_HINT
    return health
from .project import (
    IMPORT_LINE,
    _newest_activity,
    _read_staleness_hours,
    _utc_now,
    find_repo_root,
)

RESTORE_HINT = "cohort init --force"


def _wiring_state(repo: Path) -> dict[str, Any]:
    claude_md = repo / ".claude" / "CLAUDE.md"
    if not claude_md.exists():
        return {"state": "missing"}
    inner = merge.extract_block(claude_md.read_text(encoding="utf-8"))
    if inner is None:
        return {"state": "missing"}
    if inner.strip() == IMPORT_LINE:
        return {"state": "present"}
    return {"state": "diverged"}


def _unmanaged_claude_files(home: Path, manifest) -> list[dict[str, Any]]:
    """Files in Cohort's Claude destination dirs that Cohort did not place.

    These form a shadow office: they live in the same discovery directories as
    the roster but are invisible to the injected office directory — and become
    a preflight CLOBBER if canonical ever ships the same name. Recursion matters:
    Claude Code reads namespaced commands from subdirectories. Comparison is by
    realpath so a re-spelled $HOME (e.g. /home vs /var/home) can't flag every
    managed file as unmanaged."""
    recorded = {os.path.realpath(op.dest) for op in manifest.ops} if manifest else set()
    out: list[dict[str, Any]] = []
    for sub in ("agents", "commands"):
        d = home / ".claude" / sub
        if not d.exists():
            continue
        for p in sorted(d.rglob("*.md")):
            if p.is_file() and os.path.realpath(p) not in recorded:
                # adopt only handles files directly under the dir (flat names)
                out.append({"path": str(p), "adoptable": p.parent == d})
    return out


def _remote_url(repo: Path) -> Optional[str]:
    """The ``origin`` URL of a git repo, or None. Read-only, hardened, best-effort."""
    if not (repo / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env={**os.environ, **GIT_ENV},
        )
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _tier_sources(home: Path, source: Optional[Path]) -> dict[str, Any]:
    """Where each config tier points back to (a Git repo, where applicable): the
    office's upstream, my office's sync remote, and — added per-project below —
    the consuming repo."""
    from .myoffice import my_remote

    out: dict[str, Any] = {"office": None, "my": my_remote(home)}
    if source is not None:
        out["office"] = _remote_url(source) or str(source)
    return out


def _office_local_only(source: Optional[Path]) -> list[str]:
    """Canonical files in the office clone that upstream doesn't have — personal
    content candidates for ``~/.cohort/my`` (or a PR to the org fork).

    Read-only (git status/log against tracking refs); degrades to [] without a
    git repo, an upstream, or git itself."""
    if source is None or not (source / ".git").exists():
        return []
    env = {**os.environ, **GIT_ENV}
    out: set = set()
    try:
        r = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--", "canonical"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env,
        )
        if r.returncode == 0:
            out.update(line[3:].strip() for line in r.stdout.splitlines() if line[3:].strip())
        r = subprocess.run(
            ["git", "-C", str(source), "log", "--name-only", "--pretty=format:",
             "@{upstream}..HEAD", "--", "canonical"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env,
        )
        if r.returncode == 0:
            out.update(line.strip() for line in r.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(out)


def _override_health(source: Optional[Path], gpaths: CohortPaths) -> list[dict[str, str]]:
    """Personalized overrides whose office counterpart changed or vanished (#84).

    ``stale``: the office version moved since personalize (the recorded
    ``office_sha256`` no longer matches) — the user is pinned to an old copy.
    ``dangling``: the office counterpart is gone (renamed/removed upstream)."""
    my_root = gpaths.my / "canonical"
    if source is None or not my_root.exists():
        return []
    out = []
    for p in sorted(my_root.rglob("*.md")):
        fm = load_artifact(p).frontmatter or {}
        if fm.get("overrides") is not True:
            continue
        office = source / "canonical" / p.relative_to(my_root)
        if not office.exists():
            out.append({"name": p.stem, "state": "dangling"})
        else:
            recorded = fm.get("office_sha256")
            if recorded and hashlib.sha256(office.read_bytes()).hexdigest() != recorded:
                out.append({"name": p.stem, "state": "stale"})
    return out


def do_status(home: Path, cwd: Path) -> dict[str, Any]:
    """Aggregate global + project state, read-only."""
    gpaths = CohortPaths.for_global(home)
    manifest = load_manifest(gpaths.manifest)
    agents_dir = gpaths.canonical / "agents"  # the installed canonical (R5)
    roster = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.exists() else []
    my_dir = gpaths.my / "canonical" / "agents"  # the personal layer (#84)
    my_names = sorted(p.stem for p in my_dir.glob("*.md")) if my_dir.exists() else []
    source = resolve_source_lenient(home)
    result: dict[str, Any] = {
        "action": "status",
        "global": {
            "ides": manifest.ides if manifest else [],
            # union: a personalized override shares its office name — never double-count
            "roster": {"count": len(set(roster) | set(my_names)), "names": roster,
                       "my": my_names},
            "source": _source_health(gpaths),
            "unmanaged": _unmanaged_claude_files(home, manifest),
            "office_local_only": _office_local_only(source),
            "overrides": _override_health(source, gpaths),
        },
        # Where each tier's config points back to (a Git repo, where applicable).
        "sources": _tier_sources(home, source),
    }

    repo = find_repo_root(cwd)
    ppaths = CohortPaths.for_project(repo)
    is_project = ppaths.cohort_home != gpaths.cohort_home and ppaths.cohort_home.exists()
    # Inventory summary across every kind and layer — so `cohort status` (and the
    # dashboard) recognizes the whole office, not just agents.
    result["inventory"] = inventory_summary(inventory(home, repo if is_project else None))
    # A cwd under $HOME with no enclosing repo resolves to $HOME itself, whose
    # .cohort is the GLOBAL office home — never report it as a project (the
    # roster would read as self-shadowing specialists and the wiring check
    # would advise an init --force that clobbers the global CLAUDE.md block).
    if is_project:
        spec_dir = ppaths.canonical / "agents"
        specialists = sorted(p.stem for p in spec_dir.glob("*.md")) if spec_dir.exists() else []
        legacy_dir = ppaths.cohort_home / "agents"
        legacy = sorted(p.stem for p in legacy_dir.glob("*.md")) if legacy_dir.exists() else []
        newest = _newest_activity(ppaths)
        hours = _read_staleness_hours(ppaths)
        if newest is None:
            staleness = {"stale": False, "threshold_hours": hours, "age_hours": None}
        else:
            age = (_utc_now().timestamp() - newest) / 3600.0
            staleness = {"stale": age >= hours, "threshold_hours": hours, "age_hours": round(age, 2)}
        wiring = _wiring_state(repo)
        if wiring["state"] != "present":
            wiring["restore"] = RESTORE_HINT
        global_names = set(roster) | set(my_names)
        result["project"] = {
            "repo": str(repo),
            "specialists": specialists,
            "shadowed": [s for s in specialists if s in global_names],  # mask a global agent (R4)
            "staleness": staleness,
            "wiring": wiring,
        }
        if legacy:
            # Pre-unification layout: these no longer compile or count as specialists.
            result["project"]["legacy_agents"] = legacy
        # A project's settings travel with its consuming repo — surface that source.
        result["sources"]["project"] = _remote_url(repo) or str(repo)
    return result

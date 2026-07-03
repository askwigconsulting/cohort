"""`cohort status` — a strictly read-only aggregate of the install.

Reuses Phase-4's *read-only* freshness compute (never ``staleness_check``, which
writes the throttle marker, R1) and ``merge.extract_block`` for wiring detection
(R4). Reports the global install and, inside a Cohort repo, the project state.
Never mutates the filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import merge
from .install_model import CohortPaths
from .manifest import load_manifest

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


def do_status(home: Path, cwd: Path) -> dict[str, Any]:
    """Aggregate global + project state, read-only."""
    gpaths = CohortPaths.for_global(home)
    manifest = load_manifest(gpaths.manifest)
    agents_dir = gpaths.canonical / "agents"  # the installed canonical (R5)
    roster = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.exists() else []
    result: dict[str, Any] = {
        "action": "status",
        "global": {
            "ides": manifest.ides if manifest else [],
            "roster": {"count": len(roster), "names": roster},
            "source": _source_health(gpaths),
            "unmanaged": _unmanaged_claude_files(home, manifest),
        },
    }

    repo = find_repo_root(cwd)
    ppaths = CohortPaths.for_project(repo)
    # A cwd under $HOME with no enclosing repo resolves to $HOME itself, whose
    # .cohort is the GLOBAL office home — never report it as a project (the
    # roster would read as self-shadowing specialists and the wiring check
    # would advise an init --force that clobbers the global CLAUDE.md block).
    if ppaths.cohort_home != gpaths.cohort_home and ppaths.cohort_home.exists():
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
        global_names = set(roster)
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
    return result

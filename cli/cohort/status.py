"""`cohort status` — a strictly read-only aggregate of the install.

Reuses Phase-4's *read-only* freshness compute (never ``staleness_check``, which
writes the throttle marker, R1) and ``merge.extract_block`` for wiring detection
(R4). Reports the global install and, inside a Cohort repo, the project state.
Never mutates the filesystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import merge
from .install_model import CohortPaths
from .manifest import load_manifest
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
        },
    }

    repo = find_repo_root(cwd)
    ppaths = CohortPaths.for_project(repo)
    if ppaths.cohort_home.exists():
        spec_dir = ppaths.cohort_home / "agents"
        specialists = sorted(p.stem for p in spec_dir.glob("*.md")) if spec_dir.exists() else []
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
    return result

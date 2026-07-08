"""The artifact inventory — every kind, every layer, read-only.

One enumerator over (layer × kind) so both the CLI (`cohort status`) and the
dashboard can *recognize* the whole office, not just agents. Layers: office (the
source clone's canonical), my (`~/.cohort/my/canonical`), and — inside a repo —
project (`<repo>/.cohort/canonical`). Directory names come from the single
`schema.KIND_DIRS` source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .install_model import CohortPaths
from .loader import load_artifact
from .schema import KIND_DIRS
from .source import resolve_source_lenient

# Authorable kinds. `context` is init-managed (project_context.md), not authored.
INVENTORY_KINDS = ("agent", "skill", "command", "hook", "memory")


def _entry(path: Path, kind: str, layer: str) -> dict[str, Any]:
    fm = load_artifact(path).frontmatter or {}
    return {
        "name": path.stem,
        "kind": kind,
        "layer": layer,
        "display_name": fm.get("display_name", path.stem),
        "description": (fm.get("description", "") or "").strip(),
        "department": fm.get("department", ""),
        "topology": fm.get("topology"),
        "targets": fm.get("targets", []),
        "overrides": fm.get("overrides") is True,
        # A "doer": a scope:project agent with write/exec tools (advisory: false).
        # Only project agents can be doers; synced tiers are always advisory, so
        # this is True there. The dashboard flags it so a write-capable agent reads
        # at a glance. Non-agents don't use it.
        "advisory": fm.get("advisory", True),
        "path": str(path),
    }


def _layer_entries(canonical_root: Path, layer: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind in INVENTORY_KINDS:
        d = canonical_root / KIND_DIRS[kind]
        if not d.exists():
            continue
        out.extend(_entry(p, kind, layer) for p in sorted(d.glob("*.md")))
    return out


def inventory(home: Path, repo: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every managed artifact across the office / my / project layers.

    Read-only. The office layer reads the source clone's canonical (the authored
    artifacts), falling back to the installed copy when the source can't be
    resolved. Never includes ``$HOME`` as a project (its ``.cohort`` is the global
    office home)."""
    gp = CohortPaths.for_global(home)
    items: list[dict[str, Any]] = []
    source = resolve_source_lenient(home)
    office_root = (source / "canonical") if source is not None else gp.canonical
    if office_root.exists():
        items.extend(_layer_entries(office_root, "office"))
    my_root = gp.my / "canonical"
    if my_root.exists():
        items.extend(_layer_entries(my_root, "my"))
    if repo is not None:
        pp = CohortPaths.for_project(repo)
        if pp.cohort_home != gp.cohort_home and (pp.canonical).exists():
            items.extend(_layer_entries(pp.canonical, "project"))
    return items


def inventory_summary(items: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Counts per layer → kind, for a compact `cohort status` line."""
    summary: dict[str, dict[str, int]] = {}
    for it in items:
        summary.setdefault(it["layer"], {}).setdefault(it["kind"], 0)
        summary[it["layer"]][it["kind"]] += 1
    return summary

"""`cohort adopt` — lift a loose, unmanaged IDE artifact into canonical.

Pre-Cohort agents/commands sitting directly in ``~/.claude/{agents,commands}/``
form a shadow office: invisible to the injected office directory, colliding
with the roster in the router's decision space, and a future preflight CLOBBER
if canonical ever ships the same name. Adoption makes such a file managed:
generate canonical frontmatter around its body, back the original up (never
delete), and recompile so the managed rendering takes its place.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Optional

from .frontmatter import dump_frontmatter
from .install_model import CohortPaths, resolve_mode
from .loader import load_artifact
from .manifest import load_manifest
from .roster import READONLY_TOOLS_LIST, reject_control_chars
from .schema import NAME_PATTERN, validate_frontmatter


class AdoptError(Exception):
    """A refused adopt request (wrong location, collision, invalid result)."""


_KIND_BY_DIR = {"agents": "agent", "commands": "command"}


def _infer_kind_and_name(path: Path, home: Path) -> tuple[str, str]:
    """(kind, name) from the file's location — only ``~/.claude/agents/*.md`` and
    ``~/.claude/commands/*.md`` are adoptable (the global Claude tier)."""
    claude = (home / ".claude").resolve()
    if path.suffix != ".md" or path.parent.parent != claude:
        raise AdoptError(
            f"{path} is not under {claude}/agents/ or {claude}/commands/ — "
            "only loose global Claude artifacts can be adopted"
        )
    kind = _KIND_BY_DIR.get(path.parent.name)
    if kind is None:
        raise AdoptError(f"{path.parent.name}/ is not an adoptable directory (agents, commands)")
    return kind, path.stem


def _default_display_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("-"))


def _render_adopted(
    kind: str, name: str, description: str, department: str, display_name: str, body: str
) -> str:
    if kind == "agent":
        pairs = [
            ("name", name),
            ("kind", "agent"),
            ("scope", "global"),
            ("description", description),
            ("targets", ["claude"]),
            ("department", department),
            ("topology", "specialist"),
            ("advisory", True),
            ("tools", READONLY_TOOLS_LIST),
            ("display_name", display_name),
        ]
    else:  # command
        pairs = [
            ("name", name),
            ("kind", "command"),
            ("scope", "global"),
            ("description", description),
            ("targets", ["claude"]),
            ("invocation", name),
            ("dry_run", True),
        ]
    fm = dump_frontmatter(pairs).rstrip("\n")
    return f"{fm}\n{body.strip()}\n"


def do_adopt(
    home: Path,
    source: Path,
    path: Path,
    description: Optional[str] = None,
    department: Optional[str] = None,
    display_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Adopt one loose file into ``<source>/canonical/`` and recompile.

    The original is moved to ``~/.cohort/state/adopt-backups/`` (backup, never
    delete) so the managed placement can land without a clobber; any failure
    after that point restores it and removes the authored canonical file.
    Adopted agents are advisory read-only like the rest of the roster, even if
    the loose original inherited all tools.
    """
    path = Path(path).expanduser()
    if path.is_symlink():
        raise AdoptError(f"{path} is already Cohort-managed (a symlink)")
    path = path.resolve()
    if not path.is_file():
        raise AdoptError(f"{path} not found")
    kind, name = _infer_kind_and_name(path, home)
    if not re.fullmatch(NAME_PATTERN, name):
        raise AdoptError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    parsed = load_artifact(path)
    if parsed.load_error is not None or parsed.frontmatter is None:
        # a plain-markdown file (no frontmatter) — the whole text is the body
        fm: dict[str, Any] = {}
        body_text = path.read_text(encoding="utf-8")
    else:
        fm = parsed.frontmatter
        body_text = parsed.body or ""
    description = description or fm.get("description")
    if not description:
        raise AdoptError(f"{path.name} has no description in frontmatter; pass --description")
    department = department or "Adopted"
    display_name = display_name or _default_display_name(name)
    try:
        reject_control_chars(
            description=description, department=department, display_name=display_name
        )
    except ValueError as exc:
        raise AdoptError(str(exc))
    dest = source / "canonical" / f"{kind}s" / f"{name}.md"
    if dest.exists():
        raise AdoptError(f"{kind} {name!r} already exists in canonical; refusing to overwrite")

    content = _render_adopted(kind, name, description, department, display_name, body_text)
    if dry_run:
        return {
            "action": "adopt", "dry_run": True, "kind": kind, "name": name, "path": str(dest),
            "plan": [f"author {dest}", "validate", f"back up {path}", "recompile --ide claude"],
        }

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    errors = validate_frontmatter(load_artifact(dest).frontmatter, name)
    if errors:
        dest.unlink()
        raise AdoptError(f"adopted artifact failed validation: {errors[0].code} {errors[0].message}")

    gpaths = CohortPaths.for_global(home)
    backup_dir = gpaths.state / "adopt-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{kind}-{name}.md"
    shutil.move(str(path), str(backup))
    try:
        report = _recompile_global_claude(home, source, gpaths, kind, name)
    except Exception:
        shutil.move(str(backup), str(path))  # restore the loose original
        dest.unlink()
        raise
    return {
        "action": "adopt", "dry_run": False, "kind": kind, "name": name,
        "path": str(dest), "backup": str(backup),
        "advisory_enforced": kind == "agent", "installed": report.summary,
    }


def _recompile_global_claude(home: Path, source: Path, gpaths: CohortPaths, kind: str, name: str):
    """Recompile + place the global Claude tier, honoring (and, for an adopted
    agent, extending) a persisted roster subset — mirrors ``do_add_agent``."""
    from .compile import compile_ide, planned_dests, write_staging  # lazy: import cycle
    from .install import do_install

    manifest = load_manifest(gpaths.manifest)
    subset = list(manifest.roster) if manifest and manifest.roster else None
    if subset is not None and kind == "agent" and name not in subset:
        subset = subset + [name]
    only = frozenset(subset) if subset is not None else None
    result = compile_ide(source, "claude", scope="global", only_agents=only)
    write_staging(gpaths, result)
    report = do_install(
        home=home, selection=["claude"], mode=resolve_mode(copy=False), force=False,
        source=source, dry_run=False,
        prune_stale=True, fresh_dests=planned_dests(gpaths, [result]), fresh_ides={"claude"},
    )
    if subset is not None:
        fresh = load_manifest(gpaths.manifest)
        if fresh is not None:
            fresh.roster = subset
            fresh.persist(gpaths.manifest)
    return report

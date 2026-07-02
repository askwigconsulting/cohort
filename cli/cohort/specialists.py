"""Project-isolated specialists: `cohort add-specialist`, `cohort remove-specialist`,
and `cohort promote`.

Specialists are team-owned canonical artifacts under
``<repo>/.cohort/canonical/agents/`` (``scope: project``) — the same layout every
project-tier kind uses. Authoring writes the source there; compile + placement
route through the single project install path (``do_install_project``), which
renders with **no** office-directory injection (no project generalist), stages to
``<repo>/.cohort/compiled/claude/``, and links into ``<repo>/.claude/`` — all
recorded in the *project* manifest. Nothing here touches ``~/.cohort/`` or the
Cohort source.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

from .compile import CompileError
from .executor import ClobberRefused, ReverseResult, _reverse_place_ops
from .frontmatter import dump_frontmatter
from .install_model import CohortPaths
from .loader import load_artifact, load_artifact_text
from .manifest import load_manifest, now_iso
from .roster import READONLY_TOOLS_LIST, reject_control_chars
from .schema import NAME_PATTERN, validate_frontmatter


class AddSpecialistError(Exception):
    """A refused add-specialist request."""


class PromoteError(Exception):
    """A refused promote request."""


class RemoveSpecialistError(Exception):
    """A refused remove-specialist request."""


def prompt_add_specialist_inputs() -> dict[str, str]:
    """Interactive input collection (patchable in tests)."""
    name = input("name (slug): ").strip()
    return {
        "name": name,
        "display_name": input("display_name: ").strip() or name,
        "department": input("department: ").strip() or "Project",
        "description": input("description: ").strip() or f"{name} (project specialist).",
    }


def _scaffold(
    name: str, display_name: str, department: str, description: str,
    body: Optional[str] = None,
) -> str:
    """The canonical specialist file. ``body`` (e.g. from an init interview via
    ``--body-file``) replaces the placeholder template.

    Frontmatter is emitted through the safe YAML serializer (``dump_frontmatter``),
    not string interpolation: a ``description``/``department``/``display_name`` value
    carrying newlines or YAML metacharacters is quoted/escaped, so it can never
    inject a trailing ``advisory: false`` / ``tools: [...]`` key and escape the
    advisory read-only sandbox. ``do_add_specialist`` additionally validates the
    result before it is staged (fail-closed)."""
    fm = dump_frontmatter(
        [
            ("name", name),
            ("kind", "agent"),
            ("scope", "project"),
            ("description", description),
            ("targets", ["all"]),
            ("department", department),
            ("topology", "specialist"),
            ("advisory", True),
            ("tools", READONLY_TOOLS_LIST),
            ("display_name", display_name),
        ]
    ).rstrip("\n")
    if body is None:
        body = "\n".join(
            [
                f"**Role.** {description}",
                "",
                "**Advises on.** _Areas of responsibility (edit me)._",
                "",
                "**Boundaries.** Advisory only — you recommend and never approve, execute, or take an "
                "irreversible action; a human decides.",
                "",
                "**Escalation.** Hand cross-functional questions to ChiefOfStaff; defer decisions to the "
                "responsible human.",
            ]
        )
    return f"{fm}\n{body.strip()}\n"


def _is_shadow(home: Path, name: str) -> bool:
    """True if a global roster agent of this name exists (installed canonical, R4)."""
    return (CohortPaths.for_global(home).canonical / "agents" / f"{name}.md").exists()


def _legacy_hint(paths: CohortPaths, name: str) -> str:
    """A migration hint when ``name`` still lives in the pre-unification layout."""
    legacy = paths.cohort_home / "agents" / f"{name}.md"
    if not legacy.exists():
        return ""
    return (
        f" (found under the legacy {legacy.parent} — run "
        f"`git mv .cohort/agents/{name}.md .cohort/canonical/agents/{name}.md`)"
    )


def do_add_specialist(
    repo: Path, home: Path, name: str, display_name: str, department: str,
    description: str, dry_run: bool, body: Optional[str] = None,
) -> dict[str, Any]:
    from .install import do_install_project  # lazy: avoid import cycle

    paths = CohortPaths.for_project(repo)
    if not paths.manifest.exists():
        raise AddSpecialistError("not a Cohort project; run `cohort init` first")
    if not re.fullmatch(NAME_PATTERN, name):
        raise AddSpecialistError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    try:
        reject_control_chars(
            display_name=display_name, department=department, description=description
        )
    except ValueError as exc:
        raise AddSpecialistError(str(exc))
    dest = paths.canonical / "agents" / f"{name}.md"
    if dest.exists():
        raise AddSpecialistError(f"specialist {name!r} already exists in this repo")
    legacy = sorted((paths.cohort_home / "agents").glob("*.md"))
    if legacy:
        # Refuse before authoring anything: rebuilding staging while unmigrated
        # sources exist would dangle their placed links (they no longer compile).
        names = ", ".join(p.stem for p in legacy)
        raise AddSpecialistError(
            f"unmigrated project specialists in .cohort/agents/ ({names}) — run "
            f"`git mv .cohort/agents/<n>.md .cohort/canonical/agents/<n>.md` first"
        )
    if body is not None and not body.strip():
        raise AddSpecialistError("--body-file is empty")

    content = _scaffold(name, display_name, department, description, body)
    shadow = _is_shadow(home, name)
    if dry_run:
        return {"action": "add-specialist", "dry_run": True, "name": name,
                "shadow": shadow, "path": str(dest)}

    new = load_artifact_text(content, name_stem=name)
    # Fail closed: validate the scaffolded artifact before it is ever staged or
    # placed, so an invalid frontmatter (e.g. an injected advisory/tools override
    # that slipped past emission) can never reach a live agent file.
    errors = validate_frontmatter(new.frontmatter, name)
    if errors:
        raise AddSpecialistError(f"scaffold failed validation: {errors[0].code} {errors[0].message}")
    # Author the team-owned canonical source (git-tracked, preserved on deinit),
    # then compile + place the whole project tier through the single install path
    # — one writer of compiled/claude/, so authoring can never strand another
    # command's placements.
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    try:
        report = do_install_project(repo)
    except (CompileError, ClobberRefused) as exc:
        dest.unlink()  # don't leave a source the project tier cannot compile/place
        raise AddSpecialistError(str(exc))
    return {
        "action": "add-specialist", "dry_run": False, "name": name, "shadow": shadow,
        "path": str(dest), "compiled": str(repo / ".claude" / "agents" / f"{name}.md"),
        "applied": report["applied"],
    }


# --- remove-specialist -------------------------------------------------------


def do_remove_specialist(repo: Path, home: Path, name: str, dry_run: bool) -> dict[str, Any]:
    """Prune one project specialist: canonical source, staging, placed artifact,
    and its recorded manifest ops. Reuses the executor's ownership-checked
    reversal so a user-repointed link is skipped, never clobbered."""
    paths = CohortPaths.for_project(repo)
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        raise RemoveSpecialistError("not a Cohort project; run `cohort init` first")
    if not re.fullmatch(NAME_PATTERN, name):
        raise RemoveSpecialistError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    src = paths.canonical / "agents" / f"{name}.md"
    if not src.exists():
        raise RemoveSpecialistError(
            f"no project specialist {name!r} in this repo{_legacy_hint(paths, name)}"
        )

    placed = repo / ".claude" / "agents" / f"{name}.md"
    staged = paths.compiled / "claude" / "agents" / f"{name}.md"
    # Pre-unification installs staged the scaffold separately; clean it up too.
    scaffold_stage = paths.compiled / "project-scaffold" / f"{name}.md"
    shadow = _is_shadow(home, name)
    if dry_run:
        return {"action": "remove-specialist", "dry_run": True, "name": name,
                "path": str(src), "placed": str(placed), "unshadows": shadow}

    # The legacy dest too: a migrated (git mv'd) specialist may still carry its
    # pre-unification SCAFFOLD op, which would otherwise orphan in the manifest.
    targets = {str(src), str(placed), str(paths.cohort_home / "agents" / f"{name}.md")}
    mine = [op for op in manifest.ops if op.dest in targets]
    result = ReverseResult()
    _reverse_place_ops(mine, result, purge=True)  # purge: the human explicitly targeted it
    if src.exists():
        src.unlink()  # team-owned canonical source (never a manifest op) — the command's target
    if placed.is_symlink() and (
        not placed.exists()
        or paths.compiled.resolve() in Path(os.path.realpath(placed)).parents
    ):
        # Ownership re-check on resolved paths: Windows readlink returns a
        # \\?\-prefixed substitute name the executor's exact compare skips, so
        # the recorded-op reversal above can leave the link behind. realpath
        # canonicalizes both sides (prefix, short names, case) before deciding
        # the link points into our staging (ours) or dangles.
        placed.unlink()
    for leftover in (staged, scaffold_stage):
        if leftover.exists():
            leftover.unlink()  # derived staging is a non-op artifact
    manifest.ops = [op for op in manifest.ops if op.dest not in targets]
    manifest.persist(paths.manifest)
    return {
        "action": "remove-specialist", "dry_run": False, "name": name,
        "path": str(src), "unshadows": shadow,
        "removed": result.removed, "skipped": result.skipped,
        "placed_removed": not (placed.exists() or placed.is_symlink()),
    }


# --- promote ----------------------------------------------------------------


def _render_proposal(name: str, body: str) -> str:
    # unified proposals format (P8 [R7]); safe emitter (P9 [R-audit])
    fm = dump_frontmatter(
        [("kind", "promotion"), ("name", name), ("target", "global"),
         ("requested_at", now_iso())]
    )
    return f"{fm}{body.strip()}\n"


def do_promote(repo: Path, name: str, dry_run: bool) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    spec = paths.canonical / "agents" / f"{name}.md"
    if not spec.exists():
        raise PromoteError(
            f"no project specialist {name!r} in this repo{_legacy_hint(paths, name)}"
        )
    loaded = load_artifact(spec)
    if loaded.load_error is not None:
        raise PromoteError(f"{name!r} is not a valid artifact: {loaded.load_error.message}")
    errors = validate_frontmatter(loaded.frontmatter, name)
    if errors:
        raise PromoteError(f"{name!r} is invalid: {errors[0].code} {errors[0].message}")
    dest = paths.cohort_home / "proposals" / f"{name}.md"
    if dry_run:
        return {"action": "promote", "dry_run": True, "name": name, "proposal": str(dest)}
    dest.parent.mkdir(parents=True, exist_ok=True)  # proposals/ on demand
    dest.write_text(_render_proposal(name, loaded.body), encoding="utf-8")
    return {"action": "promote", "dry_run": False, "name": name, "proposal": str(dest)}

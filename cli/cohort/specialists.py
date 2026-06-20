"""Project-isolated specialists: `cohort add-specialist` and `cohort promote`.

The new technical piece (P6 [R1]) is *project-scope agent compilation*: discover
``<repo>/.cohort/agents/`` (team-owned, ``scope: project``), render through the
reused agent renderer with **no** office-directory injection (no project
generalist), stage to ``<repo>/.cohort/compiled/claude/agents/``, and link into
``<repo>/.claude/agents/`` — all recorded in the *project* manifest. Nothing here
touches ``~/.cohort/`` or the Cohort source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .adapters.claude import MarkerError, render_agent
from .compile import CompileResult, scan_staging_ops, write_staging
from .executor import apply
from .frontmatter import dump_frontmatter
from .install_model import CohortPaths, Op, OpType
from .ir import build_ir
from .loader import load_artifact, load_artifact_text
from .manifest import load_manifest, now_iso
from .project import _stage
from .roster import READONLY_TOOLS
from .schema import NAME_PATTERN, validate_frontmatter

PROJECT_IDE = "project"


class AddSpecialistError(Exception):
    """A refused add-specialist request."""


class PromoteError(Exception):
    """A refused promote request."""


def prompt_add_specialist_inputs() -> dict[str, str]:
    """Interactive input collection (patchable in tests)."""
    name = input("name (slug): ").strip()
    return {
        "name": name,
        "display_name": input("display_name: ").strip() or name,
        "department": input("department: ").strip() or "Project",
        "description": input("description: ").strip() or f"{name} (project specialist).",
    }


def _scaffold(name: str, display_name: str, department: str, description: str) -> str:
    fm = "\n".join(
        [
            "---",
            f"name: {name}",
            "kind: agent",
            "scope: project",
            f"description: {description}",
            "targets: [all]",
            f"department: {department}",
            "topology: specialist",
            "advisory: true",
            f"tools: {READONLY_TOOLS}",
            f"display_name: {display_name}",
            "---",
        ]
    )
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
    return f"{fm}\n{body}\n"


def _is_shadow(home: Path, name: str) -> bool:
    """True if a global roster agent of this name exists (installed canonical, R4)."""
    return (CohortPaths.for_global(home).canonical / "agents" / f"{name}.md").exists()


def compile_specialists(paths: CohortPaths, extra_irs: Optional[list] = None) -> list:
    """Render every project specialist into project staging (no injection, R1)."""
    agents_dir = paths.cohort_home / "agents"
    irs = []
    for p in sorted(agents_dir.glob("*.md")):
        loaded = load_artifact(p)
        errors = validate_frontmatter(loaded.frontmatter, p.stem)
        if errors:
            raise AddSpecialistError(f"{p.name}: {errors[0].code} {errors[0].message}")
        irs.append(build_ir(loaded.frontmatter, loaded.body, p))
    irs.extend(extra_irs or [])
    try:
        staged = [render_agent(ir) for ir in irs]  # specialists carry no marker
    except MarkerError as exc:
        raise AddSpecialistError(f"project specialist must not carry an office-directory marker: {exc}")
    write_staging(paths, CompileResult(ide="claude", staged=staged))
    return staged


def do_add_specialist(
    repo: Path, home: Path, name: str, display_name: str, department: str,
    description: str, dry_run: bool,
) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    if not paths.manifest.exists():
        raise AddSpecialistError("not a Cohort project; run `cohort init` first")
    if not re.fullmatch(NAME_PATTERN, name):
        raise AddSpecialistError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    dest = paths.cohort_home / "agents" / f"{name}.md"
    if dest.exists():
        raise AddSpecialistError(f"specialist {name!r} already exists in this repo")

    content = _scaffold(name, display_name, department, description)
    shadow = _is_shadow(home, name)
    if dry_run:
        return {"action": "add-specialist", "dry_run": True, "name": name,
                "shadow": shadow, "path": str(dest)}

    new = load_artifact_text(content, name_stem=name)
    new_ir = build_ir(new.frontmatter, new.body, dest)
    compile_specialists(paths, extra_irs=[new_ir])  # stage all specialists incl the new one
    scaffold_src = _stage(paths.compiled / "project-scaffold", f"{name}.md", content)
    plan = [
        Op(OpType.SCAFFOLD.value, PROJECT_IDE, str(dest), src=scaffold_src, preserve=True),
        *scan_staging_ops(paths, "claude", "link"),
    ]
    manifest = load_manifest(paths.manifest)
    outcomes = apply(plan, paths, manifest, force=False)
    manifest.persist(paths.manifest)
    return {
        "action": "add-specialist", "dry_run": False, "name": name, "shadow": shadow,
        "path": str(dest), "compiled": str(repo / ".claude" / "agents" / f"{name}.md"),
        "applied": sum(1 for o in outcomes if o.status == "applied"),
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
    spec = paths.cohort_home / "agents" / f"{name}.md"
    if not spec.exists():
        raise PromoteError(f"no project specialist {name!r} in this repo")
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

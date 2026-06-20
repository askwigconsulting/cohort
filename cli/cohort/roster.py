"""`cohort add-agent` — author a new agent into the global roster.

Scaffolds a canonical agent from the Phase-3 five-part template, enforces the
Phase-3 invariants at creation time (advisory, slug==stem, exactly-one
generalist, no collision), then recompiles so a new specialist auto-appears in
ChiefOfStaff's directory via the Phase-3 injection. Operates on a ``--source``
tree so tests run against a copy and never mutate the real roster (R3).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .compile import compile_ide, write_staging
from .install import do_install
from .install_model import CohortPaths
from .loader import load_artifact
from .schema import NAME_PATTERN, validate_frontmatter

READONLY_TOOLS = "[read, grep, glob]"


class AddAgentError(Exception):
    """A refused add-agent request (collision, second generalist, bad input)."""


def prompt_add_agent_inputs() -> dict[str, str]:
    """Interactive input collection (patchable in tests)."""
    name = input("name (slug): ").strip()
    return {
        "name": name,
        "display_name": input("display_name: ").strip() or name,
        "department": input("department: ").strip() or "General",
        "topology": (input("topology [specialist]: ").strip() or "specialist"),
        "description": input("description: ").strip() or f"{name} advisor.",
    }


def _scaffold(name: str, display_name: str, department: str, topology: str, description: str) -> str:
    fm = "\n".join(
        [
            "---",
            f"name: {name}",
            "kind: agent",
            "scope: global",
            f"description: {description}",
            "targets: [all]",
            f"department: {department}",
            f"topology: {topology}",
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


def _existing_generalist(agents_dir: Path) -> Optional[str]:
    for p in sorted(agents_dir.glob("*.md")):
        fm = load_artifact(p).frontmatter or {}
        if fm.get("topology") == "generalist":
            return fm.get("name", p.stem)
    return None


def do_add_agent(
    source: Path,
    home: Path,
    name: str,
    display_name: str,
    department: str,
    topology: str,
    description: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Scaffold + (unless dry-run) validate and recompile a new roster agent."""
    if not re.fullmatch(NAME_PATTERN, name):
        raise AddAgentError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    if topology not in ("specialist", "generalist"):
        raise AddAgentError(f"topology must be specialist|generalist, got {topology!r}")
    agents_dir = source / "canonical" / "agents"
    dest = agents_dir / f"{name}.md"
    if dest.exists():
        raise AddAgentError(f"agent {name!r} already exists; refusing to overwrite")
    if topology == "generalist":
        existing = _existing_generalist(agents_dir)
        if existing is not None:
            raise AddAgentError(
                f"a generalist ({existing!r}) already exists; the roster allows exactly one"
            )

    content = _scaffold(name, display_name, department, topology, description)
    if dry_run:
        return {
            "action": "add-agent", "dry_run": True, "name": name, "path": str(dest),
            "plan": ["scaffold " + str(dest), "validate", "recompile --ide claude"],
        }

    agents_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    errors = validate_frontmatter(load_artifact(dest).frontmatter, name)
    if errors:
        dest.unlink()
        raise AddAgentError(f"scaffold failed validation: {errors[0].code} {errors[0].message}")

    paths = CohortPaths.for_global(home)
    write_staging(paths, compile_ide(source, "claude"))
    report = do_install(
        home=home, selection=["claude"], mode="link", force=False, source=source, dry_run=False
    )
    return {
        "action": "add-agent", "dry_run": False, "name": name, "path": str(dest),
        "installed": report.summary,
    }

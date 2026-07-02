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

from .compile import compile_ide, planned_dests, write_staging
from .frontmatter import dump_frontmatter
from .install import do_install
from .install_model import CohortPaths
from .loader import load_artifact
from .manifest import load_manifest
from .schema import NAME_PATTERN, validate_frontmatter

# The canonical read-only tool set. The string form preserves the historical
# byte layout for callers that still interpolate; the list form feeds the safe
# YAML emitter (dump_frontmatter), which quotes/escapes every scalar so a
# metadata value can never inject a trailing frontmatter key.
READONLY_TOOLS_LIST = ["read", "grep", "glob"]
READONLY_TOOLS = "[read, grep, glob]"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def reject_control_chars(**fields: str) -> None:
    """Refuse a free-text metadata value containing a newline or control char.

    Safe YAML emission already prevents such a value from injecting a frontmatter
    key, but a newline would still fracture the rendered agent body (the office
    directory line). These are single-line display fields, so reject at the input
    boundary — a clean refusal beats a mangled artifact. Raises ``ValueError``."""
    for label, value in fields.items():
        if value and _CONTROL_CHARS.search(value):
            raise ValueError(f"{label} must not contain newlines or control characters")


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
    fm = dump_frontmatter(
        [
            ("name", name),
            ("kind", "agent"),
            ("scope", "global"),
            ("description", description),
            ("targets", ["all"]),
            ("department", department),
            ("topology", topology),
            ("advisory", True),
            ("tools", READONLY_TOOLS_LIST),
            ("display_name", display_name),
        ]
    ).rstrip("\n")
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
    try:
        reject_control_chars(
            display_name=display_name, department=department, description=description
        )
    except ValueError as exc:
        raise AddAgentError(str(exc))
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
    # Honor a tailored roster: on a subset office, compile with roster+[name] and
    # extend the persisted subset, so the new agent is placed AND survives the
    # next recompile/update (which would otherwise prune it as "not in roster").
    manifest = load_manifest(paths.manifest)
    subset = list(manifest.roster) if manifest and manifest.roster else None
    if subset is not None and name not in subset:
        subset = subset + [name]
    only = frozenset(subset) if subset is not None else None
    result = compile_ide(source, "claude", scope="global", only_agents=only)
    write_staging(paths, result)
    report = do_install(
        home=home, selection=["claude"], mode="link", force=False, source=source, dry_run=False,
        prune_stale=True, fresh_dests=planned_dests(paths, [result]), fresh_ides={"claude"},
    )
    if subset is not None:
        # Reload: do_install persisted its own manifest instance, so extend the
        # roster on the current file rather than overwriting with a stale copy.
        fresh = load_manifest(paths.manifest)
        if fresh is not None:
            fresh.roster = subset
            fresh.persist(paths.manifest)
    return {
        "action": "add-agent", "dry_run": False, "name": name, "path": str(dest),
        "installed": report.summary,
    }

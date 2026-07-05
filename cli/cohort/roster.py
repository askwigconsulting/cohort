"""`cohort add-agent` — author a new agent into the global roster.

Scaffolds a canonical agent from the Phase-3 five-part template, enforces the
Phase-3 invariants at creation time (advisory, slug==stem, exactly-one
generalist, no collision), then recompiles so a new specialist auto-appears in
ChiefOfStaff's directory via the Phase-3 injection. Operates on a ``--source``
tree so tests run against a copy and never mutate the real roster (R3).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from .compile import compile_ide, planned_dests, write_staging
from .frontmatter import dump_frontmatter
from .install import do_install
from .install_model import CohortPaths, resolve_mode
from .loader import load_artifact, load_artifact_text
from .manifest import load_manifest
from .schema import KIND_DIRS, NAME_PATTERN, validate_frontmatter

# The canonical read-only tool set. The string form preserves the historical
# byte layout for callers that still interpolate; the list form feeds the safe
# YAML emitter (dump_frontmatter), which quotes/escapes every scalar so a
# metadata value can never inject a trailing frontmatter key.
READONLY_TOOLS_LIST = ["read", "grep", "glob"]
READONLY_TOOLS = "[read, grep, glob]"

# Includes NEL (\x85) and the U+2028/U+2029 line/paragraph separators:
# yaml.safe_dump emits those verbatim, so they would survive round-trip and
# fracture the rendered office-directory line exactly like a raw newline.
_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


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


def _layer_dirs(source: Path, home: Path, kind_dir: str) -> dict[str, Path]:
    """The office and my roots for one artifact kind directory (#84)."""
    return {
        "office": source / "canonical" / kind_dir,
        "my": CohortPaths.for_global(home).my / "canonical" / kind_dir,
    }


def _check_cross_layer(dirs: dict[str, Path], name: str, label: str, err) -> None:
    """Refuse a name taken in either layer — the compile merge would refuse it
    anyway (additions-only, #84); failing here is earlier and names the layer."""
    for layer, d in dirs.items():
        if (d / f"{name}.md").exists():
            where = "the office layer" if layer == "office" else "my office"
            raise err(f"{label} {name!r} already exists in {where}; refusing to overwrite")


def _first_my_write(home: Path) -> bool:
    return not (CohortPaths.for_global(home).my / "canonical").exists()


def do_add_agent(
    source: Path,
    home: Path,
    name: str,
    display_name: str,
    department: str,
    topology: str,
    description: str,
    dry_run: bool,
    to: str = "my",
) -> dict[str, Any]:
    """Scaffold + (unless dry-run) validate and recompile a new roster agent.

    ``to`` picks the layer (#84): ``my`` (default — the personal overlay at
    ``~/.cohort/my``, never touched by updates or included in proposals) or
    ``office`` (the shared source clone; the explicit contribution path)."""
    if not re.fullmatch(NAME_PATTERN, name):
        raise AddAgentError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    if topology not in ("specialist", "generalist"):
        raise AddAgentError(f"topology must be specialist|generalist, got {topology!r}")
    if to not in ("my", "office"):
        raise AddAgentError(f"--to must be my|office, got {to!r}")
    try:
        reject_control_chars(
            display_name=display_name, department=department, description=description
        )
    except ValueError as exc:
        raise AddAgentError(str(exc))
    dirs = _layer_dirs(source, home, "agents")
    agents_dir = dirs[to]
    dest = agents_dir / f"{name}.md"
    _check_cross_layer(dirs, name, "agent", AddAgentError)
    if topology == "generalist":
        # exactly one generalist across BOTH layers — two routers cannot coexist
        for d in dirs.values():
            if d.exists():
                existing = _existing_generalist(d)
                if existing is not None:
                    raise AddAgentError(
                        f"a generalist ({existing!r}) already exists; the roster allows exactly one"
                    )

    content = _scaffold(name, display_name, department, topology, description)
    if dry_run:
        return {
            "action": "add-agent", "dry_run": True, "name": name, "path": str(dest),
            "layer": to,
            "plan": ["scaffold " + str(dest), "validate", "recompile --ide claude"],
        }

    first_my = to == "my" and _first_my_write(home)
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    errors = validate_frontmatter(load_artifact(dest).frontmatter, name)
    if errors:
        dest.unlink()
        raise AddAgentError(f"scaffold failed validation: {errors[0].code} {errors[0].message}")

    paths = CohortPaths.for_global(home)
    # Honor a tailored roster. Only an OFFICE-layer addition must extend the
    # persisted subset (the subset filters the office layer only, #84); a
    # my-layer agent always compiles regardless of the subset.
    manifest = load_manifest(paths.manifest)
    subset = list(manifest.roster) if manifest and manifest.roster else None
    if to == "office" and subset is not None and name not in subset:
        subset = subset + [name]
    only = frozenset(subset) if subset is not None else None
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=paths.my)
    write_staging(paths, result)
    report = do_install(
        home=home, selection=["claude"], mode=resolve_mode(copy=False), force=False,
        source=source, dry_run=False,
        prune_stale=True, fresh_dests=planned_dests(paths, [result]), fresh_ides={"claude"},
    )
    if to == "office" and subset is not None:
        # Reload: do_install persisted its own manifest instance, so extend the
        # roster on the current file rather than overwriting with a stale copy.
        fresh = load_manifest(paths.manifest)
        if fresh is not None:
            fresh.roster = subset
            fresh.persist(paths.manifest)
    return {
        "action": "add-agent", "dry_run": False, "name": name, "path": str(dest),
        "layer": to, "first_my_write": first_my,
        "installed": report.summary,
    }


# --- add-memory (global office memories) --------------------------------------


class AddMemoryError(Exception):
    """A refused add-memory request (collision, bad input)."""


def _memory_scaffold(
    name: str, display_name: str, description: str, priority: str, body: Optional[str]
) -> str:
    fm = dump_frontmatter(
        [
            ("name", name),
            ("kind", "memory"),
            ("scope", "global"),
            ("description", description),
            ("targets", ["claude"]),
            ("priority", priority),
            ("display_name", display_name),
        ]
    ).rstrip("\n")
    text = body.strip() if body else f"_{description} One focused memory per file (edit me)._"
    return f"{fm}\n{text}\n"


def do_add_memory(
    source: Path,
    home: Path,
    name: str,
    description: str,
    priority: str = "normal",
    display_name: Optional[str] = None,
    body: Optional[str] = None,
    dry_run: bool = False,
    to: str = "my",
) -> dict[str, Any]:
    """Scaffold + (unless dry-run) validate and recompile a new office memory.

    Memories are global-scope by construction (the project tier has no CLAUDE.md
    merge) and land in the compiled corpus every session reads. ``to`` picks the
    layer (#84): ``my`` (default) or ``office`` (the shared clone)."""
    if not re.fullmatch(NAME_PATTERN, name):
        raise AddMemoryError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    if priority not in ("low", "normal", "high"):
        raise AddMemoryError(f"priority must be low|normal|high, got {priority!r}")
    if to not in ("my", "office"):
        raise AddMemoryError(f"--to must be my|office, got {to!r}")
    display_name = display_name or name
    try:
        reject_control_chars(display_name=display_name, description=description)
    except ValueError as exc:
        raise AddMemoryError(str(exc))
    if body is not None and not body.strip():
        raise AddMemoryError("--body-file is empty")
    dirs = _layer_dirs(source, home, "memories")
    dest = dirs[to] / f"{name}.md"
    _check_cross_layer(dirs, name, "memory", AddMemoryError)

    content = _memory_scaffold(name, display_name, description, priority, body)
    if dry_run:
        return {
            "action": "add-memory", "dry_run": True, "name": name, "path": str(dest),
            "layer": to,
            "plan": ["scaffold " + str(dest), "validate", "recompile --ide claude"],
        }

    first_my = to == "my" and _first_my_write(home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    errors = validate_frontmatter(load_artifact(dest).frontmatter, name)
    if errors:
        dest.unlink()
        raise AddMemoryError(f"scaffold failed validation: {errors[0].code} {errors[0].message}")

    paths = CohortPaths.for_global(home)
    # Honor a tailored roster: compile with the persisted subset so the recompile
    # never prunes agents the user chose (memories are unaffected by only_agents).
    manifest = load_manifest(paths.manifest)
    only = frozenset(manifest.roster) if manifest and manifest.roster else None
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=paths.my)
    write_staging(paths, result)
    report = do_install(
        home=home, selection=["claude"], mode=resolve_mode(copy=False), force=False,
        source=source, dry_run=False,
        prune_stale=True, fresh_dests=planned_dests(paths, [result]), fresh_ides={"claude"},
    )
    return {
        "action": "add-memory", "dry_run": False, "name": name, "path": str(dest),
        "layer": to, "first_my_write": first_my,
        "installed": report.summary,
    }


# --- personalize (deliberate my-over-office override, #84 increment 4) --------


class PersonalizeError(Exception):
    """A refused personalize request (missing artifact, already personalized)."""


# Kinds a user can personalize (context is init-managed). Directory names come
# from the single KIND_DIRS source of truth.
_PERSONALIZE_KINDS = ("agent", "command", "memory", "hook", "skill")


def do_personalize(
    source: Path, home: Path, kind: str, name: str, dry_run: bool = False
) -> dict[str, Any]:
    """Copy an office artifact into my office as a deliberate override.

    The copy carries ``overrides: true`` (the merge marker — an unmarked
    collision still refuses) and ``office_sha256`` (the office content hash at
    personalize time, so ``status`` can flag the override as *stale* when the
    office version later changes, or *dangling* when it disappears)."""
    if kind not in _PERSONALIZE_KINDS:
        raise PersonalizeError(
            f"kind must be one of {', '.join(sorted(_PERSONALIZE_KINDS))}, got {kind!r}"
        )
    sub = KIND_DIRS[kind]
    office = source / "canonical" / sub / f"{name}.md"
    if not office.exists():
        raise PersonalizeError(f"no office {kind} named {name!r} to personalize")
    my_dir = CohortPaths.for_global(home).my / "canonical" / sub
    dest = my_dir / f"{name}.md"
    if dest.exists():
        raise PersonalizeError(f"{name!r} is already personalized — edit {dest}")
    raw = office.read_bytes()
    parsed = load_artifact(office)
    if parsed.load_error is not None:
        raise PersonalizeError(f"office {kind} {name!r} does not parse: {parsed.load_error.message}")
    fm = dict(parsed.frontmatter or {})
    fm["overrides"] = True
    fm["office_sha256"] = hashlib.sha256(raw).hexdigest()
    content = dump_frontmatter(list(fm.items())).rstrip("\n") + "\n" + (parsed.body or "").strip() + "\n"
    check = load_artifact_text(content, name_stem=name)
    errors = validate_frontmatter(check.frontmatter, name)
    if errors:
        raise PersonalizeError(
            f"personalized copy failed validation: {errors[0].code} {errors[0].message}"
        )
    if dry_run:
        return {"action": "personalize", "dry_run": True, "kind": kind, "name": name,
                "path": str(dest)}
    first_my = _first_my_write(home)
    my_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")

    paths = CohortPaths.for_global(home)
    manifest = load_manifest(paths.manifest)
    only = frozenset(manifest.roster) if manifest and manifest.roster else None
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=paths.my)
    write_staging(paths, result)
    report = do_install(
        home=home, selection=["claude"], mode=resolve_mode(copy=False), force=False,
        source=source, dry_run=False,
        prune_stale=True, fresh_dests=planned_dests(paths, [result]), fresh_ides={"claude"},
    )
    return {
        "action": "personalize", "dry_run": False, "kind": kind, "name": name,
        "path": str(dest), "layer": "my", "first_my_write": first_my,
        "overridden": result.overridden, "installed": report.summary,
    }


# --- add-skill / add-command / add-hook + edit (#66 dashboard authoring) -----

from .frontmatter import dump_frontmatter as _dump_fm  # noqa: E402  (already imported above)


class AuthoringError(Exception):
    """A refused add-skill/add-command/add-hook (bad input, collision, invalid)."""


class EditError(Exception):
    """A refused ``cohort edit`` (missing artifact, invalid result)."""


def recompile_global_claude(home: Path, source: Path):
    """Compile + place the global Claude tier honoring the persisted roster subset,
    the recorded install mode, and the my-office overlay — the single tail every
    global authoring/edit path shares."""
    paths = CohortPaths.for_global(home)
    manifest = load_manifest(paths.manifest)
    only = frozenset(manifest.roster) if manifest and manifest.roster else None
    mode = (manifest.mode if manifest and manifest.mode else None) or resolve_mode(copy=False)
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=paths.my)
    write_staging(paths, result)
    return do_install(
        home=home, selection=["claude"], mode=mode, force=False, source=source,
        dry_run=False, prune_stale=True, fresh_dests=planned_dests(paths, [result]),
        fresh_ides={"claude"} if result.staged else set(),
    )


def _author_global(
    home: Path, source: Path, kind: str, name: str, to: str, content: str, dry_run: bool
) -> dict[str, Any]:
    """Write scaffolded ``content`` into the chosen layer, validate fail-closed,
    recompile. Shared body for the non-agent global authoring commands."""
    if to not in ("my", "office"):
        raise AuthoringError(f"--to must be my|office, got {to!r}")
    if not re.fullmatch(NAME_PATTERN, name):
        raise AuthoringError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    dirs = _layer_dirs(source, home, KIND_DIRS[kind])
    _check_cross_layer(dirs, name, kind, AuthoringError)
    dest = dirs[to] / f"{name}.md"
    if dry_run:
        return {"action": f"add-{kind}", "dry_run": True, "name": name, "kind": kind,
                "layer": to, "path": str(dest)}
    first_my = to == "my" and _first_my_write(home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    errors = validate_frontmatter(load_artifact(dest).frontmatter, name)
    if errors:
        dest.unlink()
        raise AuthoringError(f"scaffold failed validation: {errors[0].code} {errors[0].message}")
    report = recompile_global_claude(home, source)
    return {"action": f"add-{kind}", "dry_run": False, "name": name, "kind": kind,
            "layer": to, "path": str(dest), "first_my_write": first_my,
            "installed": report.summary}


def do_add_skill(
    source: Path, home: Path, name: str, description: str, *,
    display_name: Optional[str] = None, triggers: Optional[list[str]] = None,
    body: Optional[str] = None, to: str = "my", dry_run: bool = False,
) -> dict[str, Any]:
    """Author a skill (compiled to ~/.claude/skills/<name>/SKILL.md)."""
    display_name = display_name or name
    try:
        reject_control_chars(display_name=display_name, description=description)
    except ValueError as exc:
        raise AuthoringError(str(exc))
    if body is not None and not body.strip():
        raise AuthoringError("--body-file is empty")
    pairs = [("name", name), ("kind", "skill"), ("scope", "global"),
             ("description", description), ("targets", ["claude"]),
             ("display_name", display_name)]
    if triggers:
        pairs.insert(5, ("triggers", list(triggers)))
    fm = _dump_fm(pairs).rstrip("\n")
    text = body.strip() if body else f"_{description} Describe when and how to use this skill._"
    return _author_global(home, source, "skill", name, to, f"{fm}\n{text}\n", dry_run)


def do_add_command(
    source: Path, home: Path, name: str, description: str, *,
    invocation: Optional[str] = None, body: Optional[str] = None,
    to: str = "my", dry_run: bool = False,
) -> dict[str, Any]:
    """Author a command (compiled to ~/.claude/commands/<name>.md). dry_run:true
    is fixed — the v1 safety invariant on commands."""
    try:
        reject_control_chars(description=description, invocation=invocation or name)
    except ValueError as exc:
        raise AuthoringError(str(exc))
    if body is not None and not body.strip():
        raise AuthoringError("--body-file is empty")
    fm = _dump_fm([("name", name), ("kind", "command"), ("scope", "global"),
                  ("description", description), ("targets", ["claude"]),
                  ("invocation", invocation or name), ("dry_run", True)]).rstrip("\n")
    text = body.strip() if body else f"_{description} Spell out what this command does._"
    return _author_global(home, source, "command", name, to, f"{fm}\n{text}\n", dry_run)


def do_add_hook(
    source: Path, home: Path, name: str, description: str, event: str, action: str, *,
    matcher: Optional[str] = None, body: Optional[str] = None,
    to: str = "my", dry_run: bool = False,
) -> dict[str, Any]:
    """Author a hook (merged into settings.json for the mapped IDE event)."""
    try:
        reject_control_chars(description=description, event=event, action=action,
                             matcher=matcher or "")
    except ValueError as exc:
        raise AuthoringError(str(exc))
    if body is not None and not body.strip():
        raise AuthoringError("--body-file is empty")
    pairs = [("name", name), ("kind", "hook"), ("scope", "global"),
             ("description", description), ("targets", ["claude"]),
             ("event", event), ("action", action)]
    if matcher:
        pairs.append(("matcher", matcher))
    fm = _dump_fm(pairs).rstrip("\n")
    text = body.strip() if body else f"_{description}_"
    return _author_global(home, source, "hook", name, to, f"{fm}\n{text}\n", dry_run)


# --- edit -------------------------------------------------------------------

_EDIT_KINDS = ("agent", "skill", "command", "hook", "memory")


def do_edit(
    source: Path, home: Path, kind: str, name: str, *,
    body: Optional[str] = None, description: Optional[str] = None,
    layer: str = "my", dry_run: bool = False,
) -> dict[str, Any]:
    """Edit a global artifact in place: round-trip its frontmatter (overlay only
    ``description`` when given), replace the body, re-validate fail-closed, and
    recompile. Editing the **office** layer rewrites the shared clone, so it needs
    an explicit ``--layer office``; ``my`` is the default. Round-tripping (never
    regenerating) preserves hand-added keys and a personalized copy's
    ``overrides``/``office_sha256`` — regenerating would strip them and hard-collide."""
    if kind not in _EDIT_KINDS:
        raise EditError(f"kind must be one of {', '.join(_EDIT_KINDS)}, got {kind!r}")
    if layer not in ("my", "office"):
        raise EditError(f"--layer must be my|office, got {layer!r}")
    if body is None and description is None:
        raise EditError("nothing to edit — pass --body-file and/or --description")
    if body is not None and not body.strip():
        raise EditError("--body-file is empty")
    target_dir = _layer_dirs(source, home, KIND_DIRS[kind])[layer]
    path = target_dir / f"{name}.md"
    if not path.exists():
        where = "my office" if layer == "my" else "the office layer"
        raise EditError(f"no {kind} named {name!r} in {where} to edit")
    parsed = load_artifact(path)
    if parsed.load_error is not None:
        raise EditError(f"{name!r} does not parse: {parsed.load_error.message}")
    fm = dict(parsed.frontmatter or {})
    if description is not None:
        try:
            reject_control_chars(description=description)
        except ValueError as exc:
            raise EditError(str(exc))
        fm["description"] = description
    new_body = (body if body is not None else parsed.body or "").strip()
    content = _dump_fm(list(fm.items())).rstrip("\n") + "\n" + new_body + "\n"
    check = load_artifact_text(content, name_stem=name)
    errors = validate_frontmatter(check.frontmatter, name)
    if errors:
        raise EditError(f"edited artifact failed validation: {errors[0].code} {errors[0].message}")
    if dry_run:
        return {"action": "edit", "dry_run": True, "kind": kind, "name": name,
                "layer": layer, "path": str(path)}
    path.write_text(content, encoding="utf-8")
    report = recompile_global_claude(home, source)
    return {"action": "edit", "dry_run": False, "kind": kind, "name": name,
            "layer": layer, "path": str(path), "installed": report.summary}

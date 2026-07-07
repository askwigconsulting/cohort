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
from .schema import KIND_DIRS, NAME_PATTERN, validate_frontmatter


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
        "applied": report["applied"], "scope_filtered": report.get("scope_filtered", []),
    }


# --- project-scoped authoring for any kind -----------------------------------
# The dashboard's project-level "Create" (and a future CLI) authors any supported
# kind at project scope, the analogue of the global add-<kind> commands. Agents keep
# their richer path (do_add_specialist: shadow detection, legacy-migration guard);
# skill/command/hook/memory are scaffolded here. Project memories compile into the
# repo's own CLAUDE.md corpus (do_install_project wires the @import).

_PROJECT_KINDS = ("agent", "skill", "command", "hook", "memory")


def _project_scaffold(
    kind: str, name: str, description: str, *, display_name: Optional[str] = None,
    triggers: Optional[list[str]] = None, invocation: Optional[str] = None,
    event: Optional[str] = None, action: Optional[str] = None,
    matcher: Optional[str] = None, priority: Optional[str] = None,
    body: Optional[str] = None,
) -> str:
    """Frontmatter+body for a project-scoped skill/command/hook/memory (``scope:
    project``), mirroring the global ``add-<kind>`` scaffolds. Frontmatter goes
    through the safe YAML serializer so a metacharacter-laden value can't inject
    extra keys."""
    if kind == "memory":
        pairs = [("name", name), ("kind", "memory"), ("scope", "project"),
                 ("description", description), ("targets", ["claude"]),
                 ("priority", priority or "normal"), ("display_name", display_name or name)]
        text = body.strip() if body else f"_{description} (project memory — edit me)._"
    elif kind == "skill":
        pairs = [("name", name), ("kind", "skill"), ("scope", "project"),
                 ("description", description), ("targets", ["claude"]),
                 ("display_name", display_name or name)]
        if triggers:
            pairs.insert(5, ("triggers", list(triggers)))
        text = body.strip() if body else f"_{description} Describe when and how to use this skill._"
    elif kind == "command":
        pairs = [("name", name), ("kind", "command"), ("scope", "project"),
                 ("description", description), ("targets", ["claude"]),
                 ("invocation", invocation or name), ("dry_run", True)]
        text = body.strip() if body else f"_{description} Spell out what this command does._"
    elif kind == "hook":
        if not (event and action):
            raise AddSpecialistError("a hook needs both an event and an action")
        pairs = [("name", name), ("kind", "hook"), ("scope", "project"),
                 ("description", description), ("targets", ["claude"]),
                 ("event", event), ("action", action)]
        if matcher:
            pairs.append(("matcher", matcher))
        text = body.strip() if body else f"_{description}_"
    else:
        raise AddSpecialistError(f"unsupported project kind {kind!r}")
    return f"{dump_frontmatter(pairs).rstrip(chr(10))}\n{text}\n"


def do_add_project_artifact(
    repo: Path, home: Path, kind: str, name: str, description: str, *,
    display_name: Optional[str] = None, department: Optional[str] = None,
    triggers: Optional[list[str]] = None, invocation: Optional[str] = None,
    event: Optional[str] = None, action: Optional[str] = None,
    matcher: Optional[str] = None, priority: Optional[str] = None,
    body: Optional[str] = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Author a project-scoped artifact into ``<repo>/.cohort/canonical/<kind>/`` and
    compile+place the project tier — the project analogue of the global authoring
    commands, and the backend for the dashboard's project-level Create. Agents route
    to :func:`do_add_specialist`; skill/command/hook are handled here."""
    if kind == "agent":
        return do_add_specialist(
            repo, home, name, (display_name or name).strip() or name,
            (department or "Project").strip() or "Project", description, dry_run, body=body,
        )
    if kind not in _PROJECT_KINDS:
        raise AddSpecialistError(
            f"cannot create a project {kind!r} here — supported: {', '.join(_PROJECT_KINDS)}"
        )
    from .install import do_install_project  # lazy: avoid import cycle

    paths = CohortPaths.for_project(repo)
    if not paths.manifest.exists():
        raise AddSpecialistError("not a Cohort project; run `cohort init` first")
    if not re.fullmatch(NAME_PATTERN, name):
        raise AddSpecialistError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    try:
        reject_control_chars(
            display_name=display_name or "", description=description,
            invocation=invocation or "", event=event or "", action=action or "",
            matcher=matcher or "",
        )
    except ValueError as exc:
        raise AddSpecialistError(str(exc))
    if body is not None and not body.strip():
        raise AddSpecialistError("body is empty")
    dest = paths.canonical / KIND_DIRS[kind] / f"{name}.md"
    if dest.exists():
        raise AddSpecialistError(f"a project {kind} named {name!r} already exists in this repo")

    content = _project_scaffold(
        kind, name, description, display_name=display_name, triggers=triggers,
        invocation=invocation, event=event, action=action, matcher=matcher,
        priority=priority, body=body,
    )
    if dry_run:
        return {"action": "add-project-artifact", "dry_run": True, "kind": kind,
                "name": name, "path": str(dest)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    errors = validate_frontmatter(load_artifact(dest).frontmatter, name)
    if errors:
        dest.unlink()  # never leave a source the project tier cannot compile
        raise AddSpecialistError(f"scaffold failed validation: {errors[0].code} {errors[0].message}")
    try:
        report = do_install_project(repo)
    except (CompileError, ClobberRefused) as exc:
        dest.unlink()
        raise AddSpecialistError(str(exc))
    return {"action": "add-project-artifact", "dry_run": False, "kind": kind, "name": name,
            "path": str(dest), "applied": report["applied"],
            "scope_filtered": report.get("scope_filtered", [])}


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
    # Drop the removed name from the project-context specialist roster (#24) —
    # remove-specialist doesn't route through do_install_project, so refresh here.
    from .project import refresh_project_context  # lazy: avoid import cycle

    refresh_project_context(paths)
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


def _recompile_global(home: Path, source: Path) -> dict[str, Any]:
    """Recompile + place the global Claude tier (subset- and mode-honoring) —
    the same path recompile/update run, with the my-office overlay."""
    from .compile import compile_ide, planned_dests, write_staging  # lazy: cycle
    from .install import do_install
    from .install_model import resolve_mode

    gpaths = CohortPaths.for_global(home)
    manifest = load_manifest(gpaths.manifest)
    only = frozenset(manifest.roster) if manifest and manifest.roster else None
    mode = (manifest.mode if manifest and manifest.mode else None) or resolve_mode(copy=False)
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=gpaths.my)
    write_staging(gpaths, result)
    report = do_install(
        home=home, selection=["claude"], mode=mode, force=False, source=source,
        dry_run=False, prune_stale=True, fresh_dests=planned_dests(gpaths, [result]),
        fresh_ides={"claude"} if result.staged else set(),
    )
    return report.summary


def do_promote(
    repo: Path, home: Path, name: str, dry_run: bool,
    to: str = "my", source: Optional[Path] = None,
) -> dict[str, Any]:
    """Lift a project specialist up a level (#84).

    ``--to my`` (default): a direct copy into ``~/.cohort/my/canonical/agents/``
    — your machine, your layer, no gate needed; the artifact is re-scoped to
    global and recompiled in. The project copy stays (and still wins inside its
    own repo — Claude Code's project-over-user precedence).

    ``--to office``: the human-gated path, unchanged — writes a promotion
    *proposal* consumed by ``submit-proposals`` (a draft PR someone reviews).
    Promote never writes the shared clone directly.
    """
    if to not in ("my", "office"):
        raise PromoteError(f"--to must be my|office, got {to!r}")
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

    if to == "office":
        dest = paths.cohort_home / "proposals" / f"{name}.md"
        if dry_run:
            return {"action": "promote", "dry_run": True, "name": name, "to": to,
                    "proposal": str(dest)}
        dest.parent.mkdir(parents=True, exist_ok=True)  # proposals/ on demand
        dest.write_text(_render_proposal(name, loaded.body), encoding="utf-8")
        return {"action": "promote", "dry_run": False, "name": name, "to": to,
                "proposal": str(dest)}

    if source is None:
        raise PromoteError("promoting to my office needs the source clone (pass --source)")
    gpaths = CohortPaths.for_global(home)
    my_dir = gpaths.my / "canonical" / "agents"
    for d, where in ((source / "canonical" / "agents", "the office layer"), (my_dir, "my office")):
        if (d / f"{name}.md").exists():
            raise PromoteError(f"agent {name!r} already exists in {where}")
    fm = dict(loaded.frontmatter or {})
    fm["scope"] = "global"  # the copy lives at the global tier now
    content = dump_frontmatter(list(fm.items())).rstrip("\n") + "\n" + (loaded.body or "").strip() + "\n"
    parsed = load_artifact_text(content, name_stem=name)
    errors = validate_frontmatter(parsed.frontmatter, name)
    if errors:
        raise PromoteError(f"promoted copy failed validation: {errors[0].code} {errors[0].message}")
    dest = my_dir / f"{name}.md"
    if dry_run:
        return {"action": "promote", "dry_run": True, "name": name, "to": to, "path": str(dest)}
    my_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    try:
        summary = _recompile_global(home, source)
    except (CompileError, ClobberRefused) as exc:
        dest.unlink()  # don't leave a my artifact the global tier can't compile
        raise PromoteError(str(exc))
    return {"action": "promote", "dry_run": False, "name": name, "to": to,
            "path": str(dest), "installed": summary}

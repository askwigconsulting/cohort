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
from .project import _short_id, _utc_compact
from .roster import READONLY_TOOLS_LIST, reject_control_chars
from .schema import NAME_PATTERN, validate_frontmatter


class AdoptError(Exception):
    """A refused adopt request (wrong location, collision, invalid result)."""


def _refuse_life_source(path: Path) -> None:
    """Personal data never crosses a sync boundary (RFC 0003): an artifact that
    lives inside a ``template = "life"`` project must not lift into a synced tier
    (my office / the shared office). The marker lives in the source project's
    cohort.toml, not the artifact, so it is read here — mirroring the
    doer-promotion guard."""
    from .project import find_repo_root, is_life_project

    repo = find_repo_root(path if path.is_dir() else path.parent)
    if is_life_project(CohortPaths.for_project(repo)):
        raise AdoptError(
            f'{path} is inside a life project (template = "life" in {repo}/.cohort/'
            "cohort.toml) — refusing to adopt it into a synced tier. Life-project "
            "agents stay in the life project."
        )


_KIND_BY_DIR = {"agents": "agent", "commands": "command"}

# Concrete model names found in the wild (#143) → nearest abstract tier. Substring
# match on the lowercased value so date-suffixed IDs (e.g. "claude-3-5-haiku-20241022")
# and short aliases ("opus") both resolve. A canonical tier value already present
# (fast/default/top) passes through unchanged. Anything unrecognized is dropped
# (documented, never guessed) — `_render_adopted` never emits an unmapped value, so
# adoption can never produce a schema-invalid `model:` field.
_CANONICAL_TIERS = ("fast", "default", "top")
_MODEL_TIER_HINTS = (("opus", "top"), ("haiku", "fast"), ("sonnet", "default"))


def _map_concrete_model_to_tier(value: Any) -> Optional[str]:
    """Nearest abstract tier for a loose ``model`` value, or ``None`` to drop it."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in _CANONICAL_TIERS:
        return lowered
    for hint, tier in _MODEL_TIER_HINTS:
        if hint in lowered:
            return tier
    return None


def _infer_kind_and_name(path: Path) -> tuple[str, str]:
    """(kind, name) from the file's location — a ``.claude/agents/*.md`` or
    ``.claude/commands/*.md`` file. Accepts both the global tier (``~/.claude/``)
    and a project's ``<repo>/.claude/`` (the import path), but still requires the
    ``.claude/{agents,commands}/`` structure so an arbitrary path can't be adopted."""
    if path.suffix != ".md" or path.parent.parent.name != ".claude":
        raise AdoptError(
            f"{path} is not under a .claude/agents/ or .claude/commands/ directory — "
            "only native Claude agents/commands can be adopted"
        )
    kind = _KIND_BY_DIR.get(path.parent.name)
    if kind is None:
        raise AdoptError(f"{path.parent.name}/ is not an adoptable directory (agents, commands)")
    return kind, path.stem


def _default_display_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("-"))


def _render_adopted(
    kind: str,
    name: str,
    description: str,
    department: str,
    display_name: str,
    body: str,
    model_tier: Optional[str] = None,
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
        # A concrete model name found in the wild (#143) is mapped to its nearest
        # abstract tier by the caller; an unrecognized value is dropped rather than
        # guessed, so this never emits anything outside the schema's fast|default|top.
        if model_tier is not None:
            pairs.append(("model", model_tier))
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
    _refuse_life_source(path)  # adoption targets a SYNCED tier (my office)
    if path.stat().st_nlink > 1:
        # a pre-planted hardlink would copy some other file's content into the
        # (typically git-tracked) canonical tree — refuse the ambiguity
        raise AdoptError(f"{path} has multiple hard links; copy it to a fresh file first")
    kind, name = _infer_kind_and_name(path)
    if not re.fullmatch(NAME_PATTERN, name):
        raise AdoptError(f"name {name!r} must match the slug pattern {NAME_PATTERN}")
    raw = path.read_text(encoding="utf-8")
    parsed = load_artifact(path)
    if parsed.load_error is not None:
        if raw.lstrip().startswith("---"):
            # frontmatter was intended but doesn't parse — embedding it as body
            # text would silently bake the breakage in; refuse instead
            raise AdoptError(
                f"{path.name}: frontmatter does not parse "
                f"({parsed.load_error.message}) — fix it or strip it, then re-adopt"
            )
        fm: dict[str, Any] = {}
        body_text = raw  # plain markdown: the whole text is the body
    elif parsed.frontmatter is None:
        fm = {}
        body_text = raw
    else:
        fm = parsed.frontmatter
        body_text = parsed.body or ""
    description = description or fm.get("description")
    if not description:
        raise AdoptError(f"{path.name} has no description in frontmatter; pass --description")
    if not isinstance(description, str):
        # frontmatter YAML may hand back a dict/list/int — untrusted input, refuse
        raise AdoptError(f"{path.name}: description must be a string, got {type(description).__name__}")
    department = department or "Adopted"
    display_name = display_name or _default_display_name(name)
    try:
        reject_control_chars(
            description=description, department=department, display_name=display_name
        )
    except ValueError as exc:
        raise AdoptError(str(exc))
    # Adoption is personal by definition — it lands in my office (#84), never in
    # the shared clone; contributing upstream stays an explicit PR/proposal act.
    gpaths = CohortPaths.for_global(home)
    dest = gpaths.my / "canonical" / f"{kind}s" / f"{name}.md"
    for layer_dir, where in (
        (source / "canonical" / f"{kind}s", "the office layer"),
        (gpaths.my / "canonical" / f"{kind}s", "my office"),
    ):
        if (layer_dir / f"{name}.md").exists():
            raise AdoptError(f"{kind} {name!r} already exists in {where}; refusing to overwrite")

    model_tier = _map_concrete_model_to_tier(fm.get("model")) if kind == "agent" else None
    content = _render_adopted(
        kind, name, description, department, display_name, body_text, model_tier
    )
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

    backup_dir = gpaths.state / "adopt-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if backup_dir.is_symlink():
        raise AdoptError(f"{backup_dir} is a symlink; refusing to write backups through it")
    # Uniquified so a re-adopt of the same name can never replace an earlier
    # backup ("never delete user data" — mirrors the executor's backups/<id>/).
    backup = backup_dir / f"{kind}-{name}-{_utc_compact()}-{_short_id()}.md"
    shutil.move(str(path), str(backup))
    try:
        report = _recompile_global_claude(home, source, gpaths, kind, name)
    except Exception as exc:
        restored = _rollback_failed_adopt(gpaths, source, dest, path, backup)
        where = "original restored" if restored else f"original kept at {backup}"
        raise AdoptError(f"recompile failed; nothing adopted ({where}): {exc}") from exc
    return {
        "action": "adopt", "dry_run": False, "kind": kind, "name": name,
        "path": str(dest), "backup": str(backup), "layer": "my",
        "advisory_enforced": kind == "agent", "installed": report.summary,
    }


def _rollback_failed_adopt(
    gpaths: CohortPaths, source: Path, dest: Path, path: Path, backup: Path
) -> bool:
    """Return the install to its pre-adopt shape after a failed recompile.

    The recompile may have partially applied before failing: a placed link at the
    original's dest and a recorded manifest op would otherwise outlive the adopt
    (a ghost op that blocks future installs and hides the restored file from the
    unmanaged scan). Order matters: drop the canonical source, rebuild staging
    without it, drop the op, clear the placed link, then restore the original.
    Returns True when the original was moved back into place; False when the
    dest was occupied and the backup was kept instead (never clobber).
    """
    dest.unlink(missing_ok=True)
    try:
        from .compile import compile_ide, write_staging  # lazy: import cycle

        manifest = load_manifest(gpaths.manifest)
        subset = frozenset(manifest.roster) if manifest and manifest.roster else None
        write_staging(
            gpaths,
            compile_ide(source, "claude", scope="global", only_agents=subset, overlay=gpaths.my),
        )
    except Exception:  # noqa: BLE001 - staging rebuild is best-effort during rollback
        pass
    manifest = load_manifest(gpaths.manifest)
    if manifest is not None:
        kept = [op for op in manifest.ops if op.dest != str(path)]
        if len(kept) != len(manifest.ops):
            manifest.ops = kept
            manifest.persist(gpaths.manifest)
    if path.is_symlink():
        path.unlink()  # the link this attempt placed
    if not path.exists():
        shutil.move(str(backup), str(path))
        return True
    return False


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
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=gpaths.my)
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


# --- import: bring pre-existing native Claude agents into the office ----------
#
# `adopt` above handles ONE loose file → my office (forced advisory). The importer
# adds the two things a repo with pre-Cohort agents needs: a whole directory at
# once, and the PROJECT tier (`--to project`) — where, since project-scope doers
# are allowed, a write-capable source agent is imported as a real doer (tools
# preserved) instead of being neutered. `--to my` still forces advisory (a synced
# tier), so a doer source is imported read-only and flagged.

_READONLY_CANON = frozenset({"read", "grep", "glob", "webfetch", "websearch"})


def _native_tools(fm: dict[str, Any]) -> tuple[list[str], bool]:
    """(canonical tool names, tools_were_declared) from a native agent's frontmatter.

    Claude writes `tools` as a comma string or a YAML list; unknown/MCP tools drop
    (they'd drop at render anyway). No `tools` key means Claude grants ALL tools —
    we can't infer a safe doer set from that, so the caller treats it as advisory."""
    from .adapters.claude import _TOOL_MAP  # canonical → Claude; keys are canonical

    raw = fm.get("tools")
    if raw is None:
        return [], False
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    known, out = set(_TOOL_MAP), []
    for t in items:
        key = str(t).strip().lower().replace(" ", "").replace("_", "")
        if key in known and key not in out:
            out.append(key)
    return out, True


class _Native:
    """Parsed facts about a native agent file the importer needs."""

    def __init__(
        self,
        path: Path,
        name: str,
        description: str,
        tools: list[str],
        is_doer: bool,
        model_tier: Optional[str] = None,
    ):
        self.path, self.name, self.description = path, name, description
        self.tools, self.is_doer = tools, is_doer
        self.model_tier = model_tier


def _read_native_agent(path: Path) -> _Native:
    kind, name = _infer_kind_and_name(path)
    if kind != "agent":
        raise AdoptError(f"{path.name}: only agents import (got {kind})")
    if not re.fullmatch(NAME_PATTERN, name):
        raise AdoptError(f"{path.name}: name {name!r} must match the slug pattern {NAME_PATTERN}")
    parsed = load_artifact(path)
    if parsed.load_error is not None and path.read_text(encoding="utf-8").lstrip().startswith("---"):
        raise AdoptError(f"{path.name}: frontmatter does not parse ({parsed.load_error.message})")
    fm = parsed.frontmatter or {}
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AdoptError(f"{path.name}: no usable description; add one or pass --description")
    tools, declared = _native_tools(fm)
    model_tier = _map_concrete_model_to_tier(fm.get("model"))
    # A doer only when it EXPLICITLY declares a write/exec tool. An implicit
    # all-tools grant (no `tools` key) is imported advisory — safest, since we
    # can't know which tools it actually needs; the author can widen it later.
    is_doer = declared and any(t not in _READONLY_CANON for t in tools)
    return _Native(path, name, description.strip(), tools, is_doer, model_tier)


def _project_agent_canonical(
    n: _Native, department: str, display_name: str, *, as_doer: bool
) -> str:
    """Canonical for a project agent, preserving the source's doer/advisory nature."""
    tools = n.tools if as_doer else READONLY_TOOLS_LIST
    pairs = [
        ("name", n.name), ("kind", "agent"), ("scope", "project"),
        ("description", n.description), ("targets", ["claude"]),
        ("department", department), ("topology", "specialist"),
        ("advisory", not as_doer), ("tools", tools), ("display_name", display_name),
    ]
    # A concrete model name found in the wild (#143) is mapped to its nearest
    # abstract tier at read time (`_read_native_agent`); an unrecognized value was
    # already dropped there, so this never emits anything outside the schema.
    if n.model_tier is not None:
        pairs.append(("model", n.model_tier))
    body = load_artifact(n.path).body or ""
    return f"{dump_frontmatter(pairs).rstrip(chr(10))}\n{body.strip()}\n"


def do_import_agents(
    home: Path, source: Path, target: Path, *,
    to: str = "my", department: Optional[str] = None,
    advisory_only: bool = False, dry_run: bool = False, repo: Optional[Path] = None,
) -> dict[str, Any]:
    """Import a native agent file — or a whole `.claude/agents/` directory — into
    Cohort. `to="project"` preserves doers; `to="my"` forces advisory. `repo`
    defaults to the current working repo (the CLI's caller)."""
    if to not in ("my", "project"):
        raise AdoptError(f"--to must be my|project, got {to!r}")
    target = Path(target).expanduser()
    if target.is_dir():
        files = sorted(target.glob("*.md"))
        if not files:
            raise AdoptError(f"{target} contains no .md agent files")
    elif target.is_file():
        files = [target.resolve()]
    else:
        raise AdoptError(f"{target} not found")
    if to == "my":
        return _import_to_my(home, source, files, department, advisory_only, dry_run)
    return _import_to_project(home, files, department, advisory_only, dry_run, repo)


def _import_to_my(
    home: Path, source: Path, files: list[Path],
    department: Optional[str], advisory_only: bool, dry_run: bool,
) -> dict[str, Any]:
    imported, skipped, downgraded = [], [], []
    for f in files:
        n = _read_native_agent(f)
        if n.is_doer and advisory_only:
            skipped.append({"name": n.name, "reason": "doer skipped (--advisory-only)"})
            continue
        do_adopt(home, source, f, department=department, dry_run=dry_run)
        imported.append({"name": n.name, "was_doer": n.is_doer})
        if n.is_doer:  # my office is synced → advisory-only; the write tools are dropped
            downgraded.append(n.name)
    return {"action": "import", "to": "my", "dry_run": dry_run,
            "imported": imported, "skipped": skipped, "doers_downgraded": downgraded}


def _import_to_project(
    home: Path, files: list[Path],
    department: Optional[str], advisory_only: bool, dry_run: bool, repo: Optional[Path] = None,
) -> dict[str, Any]:
    from .install import do_install_project
    from .project import find_repo_root

    repo = repo or find_repo_root(Path.cwd())
    ppaths = CohortPaths.for_project(repo)
    if not ppaths.manifest.exists():
        raise AdoptError("not a Cohort project; run `cohort init` in the repo first")
    dept = department or "Imported"
    natives = [_read_native_agent(f) for f in files]  # parse all before mutating (fail-closed)
    agents_dir = ppaths.cohort_home / "canonical" / "agents"
    for n in natives:
        if (agents_dir / f"{n.name}.md").exists():
            raise AdoptError(f"a project agent {n.name!r} already exists; remove it first")

    to_import = [n for n in natives if not (n.is_doer and advisory_only)]
    skipped = [{"name": n.name, "reason": "doer skipped (--advisory-only)"}
               for n in natives if n.is_doer and advisory_only]
    if dry_run:
        return {"action": "import", "to": "project", "dry_run": True,
                "imported": [{"name": n.name, "as_doer": n.is_doer} for n in to_import],
                "skipped": skipped}

    agents_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = CohortPaths.for_global(home).state / "adopt-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    backups: list[tuple[Path, Path]] = []  # (original, backup) to restore on failure
    try:
        for n in to_import:
            dest = agents_dir / f"{n.name}.md"
            dest.write_text(
                _project_agent_canonical(n, dept, _default_display_name(n.name), as_doer=n.is_doer),
                encoding="utf-8",
            )
            errors = validate_frontmatter(load_artifact(dest).frontmatter, n.name)
            if errors:
                dest.unlink()
                raise AdoptError(f"{n.name}: {errors[0].code} {errors[0].message}")
            written.append(dest)
            # The source native file sits at the placement dest (<repo>/.claude/
            # agents/<name>.md); back it up (never delete) so the managed symlink
            # can land without a clobber.
            if n.path.exists() and not n.path.is_symlink():
                backup = backup_dir / f"agent-{n.name}-{_utc_compact()}-{_short_id()}.md"
                shutil.move(str(n.path), str(backup))
                backups.append((n.path, backup))
        report = do_install_project(repo)
    except Exception as exc:
        for dest in written:
            dest.unlink(missing_ok=True)
        for original, backup in backups:
            if not original.exists():
                shutil.move(str(backup), str(original))
        raise AdoptError(f"import failed; nothing changed: {exc}") from exc
    return {"action": "import", "to": "project", "dry_run": False,
            "imported": [{"name": n.name, "as_doer": n.is_doer} for n in to_import],
            "skipped": skipped, "doers": report.get("doers", []),
            "backups": [str(b) for _, b in backups]}

"""Project-scope commands: init, snapshot, context refresh, deinit, staleness.

Builds on the base-parameterized executor (project paths) and the merge op at
project scope. The git-tracked content (`project_context.md`, `sessions/`,
`cohort.toml`) is scaffolded (create-if-absent, preserved on deinit); the wiring
(`state/`, `compiled/`, the `.claude/CLAUDE.md` @import block, `.gitignore`) is
Cohort-owned and reversed on deinit.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .executor import apply, preflight, reverse_full
from .frontmatter import dump_frontmatter
from .install_model import CohortPaths, Op, OpType
from .loader import load_artifact
from .manifest import Manifest, load_manifest, new_install_id, now_iso

PROJECT_IDE = "project"
IMPORT_LINE = "@import ../.cohort/project_context.md"
GITIGNORE_CONTENT = "# Cohort machine-local bookkeeping (do not commit)\nstate/\ncompiled/\n"
COHORT_TOML_CONTENT = (
    "# Cohort project config (git-tracked, shared)\n"
    "staleness_hours = 24\n"
    "# Write a minimal session record at session end (fuels weekly-report,\n"
    "# propose-improvement, and the next session's recall). On by default so exit\n"
    "# context is never lost silently; set false to opt this repo out.\n"
    "auto_capture = true\n"
    "\n"
    "# Opt-in: let /plan add filed issues to a GitHub Projects (v2) board once\n"
    "# it has created them. project_number must be an integer; project_owner\n"
    "# must be a GitHub user/org login. Absent, or either value invalid, means\n"
    "# the board add is skipped (issues are still filed).\n"
    "# [tracker]\n"
    '# project_owner = "my-org"\n'
    "# project_number = 4\n"
)
INDEX_EMPTY = "_No sessions yet._"
INDEX_LIMIT = 10


# --- repo & git helpers -----------------------------------------------------


def find_repo_root(start: Path) -> Path:
    """Nearest ancestor of ``start`` containing ``.git``; else ``start``."""
    start = Path(start).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=10
        )
        return proc.stdout.strip()
    except Exception:  # noqa: BLE001 - git is best-effort metadata
        return ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_compact() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _short_id() -> str:
    return uuid.uuid4().hex[:6]


# --- project config (the ONE shared cohort.toml reader) ----------------------


def _coerce_toml_scalar(value: str) -> Any:
    """A best-effort scalar for the minimal fallback parser (strings, booleans,
    numbers). Unrecognized shapes come back as the raw string."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    bare = value.split("#", 1)[0].strip()
    if bare == "true":
        return True
    if bare == "false":
        return False
    for cast in (int, float):
        try:
            return cast(bare)
        except ValueError:
            continue
    return bare


def _parse_toml_minimal(text: str) -> dict[str, Any]:
    """A minimal line-based TOML reader for Python 3.10 (no ``tomllib``).

    Understands exactly the flat ``key = value`` + ``[table]`` shape Cohort's own
    cohort.toml scaffolds use. Never a general TOML parser — the caller treats
    any surprise as fail-safe defaults."""
    out: dict[str, Any] = {}
    table = out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            table = out.setdefault(line[1:-1].strip(), {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            table[key.strip()] = _coerce_toml_scalar(value.strip())
    return out


def read_project_config(paths: CohortPaths) -> dict[str, Any]:
    """The project's ``.cohort/cohort.toml`` as a dict — the one shared reader.

    Fail-safe: a missing or unparseable file is ``{}`` (absent = defaults
    everywhere). Uses ``tomllib`` where available (3.11+); on 3.10 a minimal
    fallback parser reads the flat shape Cohort scaffolds, so ``[dashboard]``
    still resolves there.
    """
    toml_path = paths.cohort_home / "cohort.toml"
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            return _parse_toml_minimal(text)
        except Exception:  # noqa: BLE001 - bad config falls back to defaults
            return {}
    try:
        return tomllib.loads(text)
    except Exception:  # noqa: BLE001 - bad config falls back to defaults
        return {}


def read_dashboard_private(paths: CohortPaths) -> bool:
    """The ``[dashboard].private`` flag: when true, the project is withheld from
    the dashboard's office-wide surfaces (switcher, activity feed, scorecards).
    Fail-safe: an absent or invalid value means public."""
    config = read_project_config(paths)
    dashboard = config.get("dashboard")
    value = dashboard.get("private") if isinstance(dashboard, dict) else None
    return value if isinstance(value, bool) else False


# --- templates & payloads ---------------------------------------------------


def context_template(source: Path) -> str:
    """The scaffolded ``project_context.md`` body (canonical context template)."""
    result = load_artifact(source / "canonical" / "contexts" / "project-context.md")
    return result.body


def sessions_index(paths: CohortPaths) -> str:
    """Deterministic newest-first index of ``sessions/`` (part of the managed block)."""
    sessions_dir = paths.cohort_home / "sessions"
    files = sorted(sessions_dir.glob("*.md"), reverse=True) if sessions_dir.exists() else []
    if not files:
        return INDEX_EMPTY
    lines = []
    for f in files[:INDEX_LIMIT]:
        fm = load_artifact(f).frontmatter or {}
        ts = fm.get("timestamp", "?")
        author = fm.get("author", "?")
        branch = fm.get("branch", "?")
        lines.append(f"- `{ts}` · {author} · {branch} — [{f.name}](sessions/{f.name})")
    return "\n".join(lines)


def specialists_index(paths: CohortPaths) -> str:
    """The project's specialist roster (part of the managed block, #24). Read from
    canonical so it reflects sources even before/without a placement."""
    spec_dir = paths.canonical / "agents"
    specs = sorted(spec_dir.glob("*.md")) if spec_dir.exists() else []
    if not specs:
        return "_None — add one with `cohort add-specialist`._"
    lines = []
    for p in specs:
        fm = load_artifact(p).frontmatter or {}
        label = fm.get("display_name", p.stem)
        dept = fm.get("department", "")
        desc = (fm.get("description", "") or "").strip()
        dept_part = f" ({dept})" if dept else ""
        lines.append(f"- **{label}**{dept_part} — {desc}")
    return "\n".join(lines)


def managed_context_block(paths: CohortPaths) -> str:
    """Body of the single Cohort-managed block in ``project_context.md`` — the
    project specialist roster (so ChiefOfStaff, which reads the always-@imported
    project context, can route to them, #24) plus the recent-sessions index."""
    return (
        "### Project specialists\n"
        "_Advisory agents scoped to this repo. For repo-specific requests, ChiefOfStaff "
        "routes here; you can also invoke one directly by name._\n\n"
        f"{specialists_index(paths)}\n\n"
        "### Recent sessions\n"
        f"{sessions_index(paths)}"
    )


def refresh_project_context(
    paths: CohortPaths, *, dry_run: bool = False, force: bool = False
) -> dict[str, Any]:
    """Re-merge the managed block (specialists + sessions) into project_context.md.

    Reused by ``cohort init``/``context refresh`` and, so the specialist roster
    tracks reality, by every project write path (add-/remove-specialist, project
    recompile). A no-op when the content is unchanged; respects a user-diverged
    block (skip, don't clobber). Returns ``{changed, diverged}`` or ``{error}``."""
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        return {"error": "not a Cohort project (run cohort init)"}
    project_context = paths.cohort_home / "project_context.md"
    if not project_context.exists():
        return {"changed": False}  # nothing to merge into yet
    body = managed_context_block(paths)
    stage_dir = Path(tempfile.mkdtemp()) if dry_run else (paths.compiled / "project")
    src = _stage(stage_dir, "context-block.txt", body)
    merge_op = Op(OpType.MERGE.value, PROJECT_IDE, str(project_context),
                  src=src, strategy="block", preserve=True)
    if dry_run:
        pf = preflight([merge_op], manifest, force=force)
        return {"changed": pf.classified[0].status.value != "satisfied"}
    outcomes = apply([merge_op], paths, manifest, force=force)
    manifest.persist(paths.manifest)
    return {
        "changed": any(o.status == "applied" for o in outcomes),
        "diverged": sum(getattr(o, "diverged", 0) for o in outcomes),
    }


# Project memories compile into <repo>/.claude/cohort/CLAUDE.cohort.md; this @import
# (relative to <repo>/.claude/CLAUDE.md) is added to the managed block alongside the
# project-context import when the project has memories, and removed when it has none.
MEMORY_CORPUS_IMPORT = "@import cohort/CLAUDE.cohort.md"


def claude_import_block(has_memory: bool) -> str:
    """The inner of the managed CLAUDE.md block: always the project-context import,
    plus the memory-corpus import when the project has compiled memories."""
    lines = [IMPORT_LINE] + ([MEMORY_CORPUS_IMPORT] if has_memory else [])
    return "\n".join(lines) + "\n"


def refresh_claude_imports(
    paths: CohortPaths, repo: Path, has_memory: bool, *, force: bool = False
) -> dict[str, Any]:
    """Re-merge the repo's ``.claude/CLAUDE.md`` managed block so it imports the
    project memory corpus iff the project has memories — the project-tier analogue
    of the global CLAUDE.md corpus wiring. Called by ``do_install_project`` after
    every project compile; a no-op when unchanged, and it respects a user-diverged
    block (skip, never clobber), exactly like ``refresh_project_context``."""
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        return {"error": "not a Cohort project (run cohort init)"}
    claude_md = repo / ".claude" / "CLAUDE.md"
    if not claude_md.exists():
        return {"changed": False}  # init hasn't wired it yet
    src = _stage(paths.compiled / "project", "claude-import.txt", claude_import_block(has_memory))
    merge_op = Op(OpType.MERGE.value, PROJECT_IDE, str(claude_md),
                  src=src, strategy="block", preserve=False)
    outcomes = apply([merge_op], paths, manifest, force=force)
    manifest.persist(paths.manifest)
    return {
        "changed": any(o.status == "applied" for o in outcomes),
        "diverged": sum(getattr(o, "diverged", 0) for o in outcomes),
    }


def render_snapshot_entry(repo: Path) -> str:
    """A dated session entry: frontmatter + Changed/Decisions/Open items/Notes."""
    author = _git(repo, "config", "user.name") or "unknown"
    email = _git(repo, "config", "user.email")
    if email:
        author = f"{author} <{email}>"
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    changed = _git(repo, "diff", "--stat") or _git(repo, "status", "--short") or "(no changes)"
    fm = dump_frontmatter(
        [("timestamp", now_iso()), ("author", author), ("branch", branch)]
    ).rstrip("\n").split("\n")
    body = [
        "## Changed",
        "```",
        changed,
        "```",
        "",
        "## Decisions",
        "_What was decided and why._",
        "",
        "## Open items",
        "_What's still open._",
        "",
        "## Notes",
        "",
    ]
    return "\n".join(fm) + "\n" + "\n".join(body) + "\n"


def _stage(stage_dir: Path, name: str, content: str) -> str:
    stage_dir.mkdir(parents=True, exist_ok=True)
    p = stage_dir / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# --- plan building ----------------------------------------------------------


def _build_init_plan(
    paths: CohortPaths, repo: Path, source: Path, stage_dir: Path,
) -> list[Op]:
    cohort_home = paths.cohort_home
    project_context = cohort_home / "project_context.md"
    claude_md = repo / ".claude" / "CLAUDE.md"
    srcs = {
        "gitignore": _stage(stage_dir, "gitignore", GITIGNORE_CONTENT),
        "toml": _stage(stage_dir, "cohort.toml", COHORT_TOML_CONTENT),
        "context": _stage(stage_dir, "project_context.md", context_template(source)),
        "index": _stage(stage_dir, "context-block.txt", managed_context_block(paths)),
        "import": _stage(stage_dir, "claude-import.txt", IMPORT_LINE + "\n"),
    }
    return [
        Op(OpType.MKDIR.value, PROJECT_IDE, str(cohort_home), preserve=True),
        Op(OpType.MKDIR.value, PROJECT_IDE, str(cohort_home / "sessions"), preserve=True),
        Op(OpType.MKDIR.value, PROJECT_IDE, str(paths.state), preserve=False),
        Op(OpType.SCAFFOLD.value, PROJECT_IDE, str(cohort_home / ".gitignore"),
           src=srcs["gitignore"], preserve=False),
        Op(OpType.SCAFFOLD.value, PROJECT_IDE, str(cohort_home / "cohort.toml"),
           src=srcs["toml"], preserve=True),
        Op(OpType.SCAFFOLD.value, PROJECT_IDE, str(project_context),
           src=srcs["context"], preserve=True),
        Op(OpType.MERGE.value, PROJECT_IDE, str(project_context),
           src=srcs["index"], strategy="block", preserve=True),
        Op(OpType.MKDIR.value, PROJECT_IDE, str(repo / ".claude"), preserve=False),
        Op(OpType.MERGE.value, PROJECT_IDE, str(claude_md),
           src=srcs["import"], strategy="block", preserve=False),
    ]


def _new_manifest() -> Manifest:
    return Manifest(
        install_id=new_install_id(), created_at=now_iso(), mode="project", ides=[PROJECT_IDE]
    )


def _summary(outcomes) -> dict[str, int]:
    return {
        "applied": sum(1 for o in outcomes if o.status == "applied"),
        "skipped": sum(1 for o in outcomes if o.status == "skipped"),
        "diverged": sum(getattr(o, "diverged", 0) for o in outcomes),
    }


# --- project registry (multi-project awareness, #66) ------------------------


def _registry_path(home: Path) -> Path:
    return CohortPaths.for_global(home).state / "projects.json"


def _read_registry(home: Path) -> list[str]:
    import json

    try:
        data = json.loads(_registry_path(home).read_text(encoding="utf-8"))
        return [str(p) for p in data.get("projects", [])] if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001 - missing/corrupt registry → empty
        return []


def _write_registry(home: Path, projects: list[str]) -> None:
    import json

    path = _registry_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"projects": projects}, indent=2), encoding="utf-8")
    except OSError:
        pass  # advisory state; a write failure never fails init/deinit


def _register_project(home: Path, repo: Path) -> None:
    """Record a repo as a Cohort project (dedup). Never records ``$HOME`` — its
    ``.cohort`` is the global office home, not a project."""
    gp = CohortPaths.for_global(home)
    resolved = str(Path(repo).resolve())
    if Path(resolved) == gp.home.resolve():
        return
    projects = _read_registry(home)
    if resolved not in projects:
        projects.append(resolved)
        _write_registry(home, projects)


def _deregister_project(home: Path, repo: Path) -> None:
    resolved = str(Path(repo).resolve())
    projects = [p for p in _read_registry(home) if p != resolved]
    _write_registry(home, projects)


def _project_wiring(repo: Path) -> str:
    """The @import wiring state of a repo's CLAUDE.md, without importing status.py."""
    from . import merge

    claude_md = repo / ".claude" / "CLAUDE.md"
    if not claude_md.exists():
        return "missing"
    inner = merge.extract_block(claude_md.read_text(encoding="utf-8"))
    if inner is None:
        return "missing"
    # Present in either managed form: the project-context import alone, or with the
    # project-memory corpus import added (a project that has memories).
    valid = {claude_import_block(False).strip(), claude_import_block(True).strip()}
    return "present" if inner.strip() in valid else "diverged"


def list_projects(home: Path, include_private: bool = True) -> list[dict[str, Any]]:
    """Registered projects, each with a health summary. Prunes (and rewrites)
    entries whose ``.cohort`` manifest is gone — a deleted/deinited repo drops off
    the list on the next read. Read-only aside from that self-heal.

    ``include_private=True`` (the default) enumerates every live project. The
    dashboard's office-wide surfaces (switcher, ``resolve_registered``,
    cross-project activity/scorecards) pass ``include_private=False`` to withhold
    any project that set ``[dashboard].private = true``."""
    gp = CohortPaths.for_global(home)
    original = _read_registry(home)
    kept: list[str] = []
    out: list[dict[str, Any]] = []
    for p in original:
        repo = Path(p)
        pp = CohortPaths.for_project(repo)
        if pp.cohort_home == gp.cohort_home or not pp.manifest.exists():
            continue  # dead or $HOME → prune
        kept.append(p)  # a live project stays registered (self-heal only prunes dead ones)
        # A private project stays registered but is withheld from office-wide
        # dashboard surfaces — never from the CLI resolution path.
        if not include_private and read_dashboard_private(pp):
            continue
        spec_dir = pp.canonical / "agents"
        specialists = sorted(x.stem for x in spec_dir.glob("*.md")) if spec_dir.exists() else []
        out.append({
            "index": len(out),
            "path": p,
            "name": repo.name,
            "specialists": len(specialists),
            "wiring": _project_wiring(repo),
        })
    if kept != original:
        _write_registry(home, kept)
    return out


def resolve_registered(home: Path, index: Any) -> Optional[Path]:
    """Map a project *index* (from the UI switcher) to its repo path — re-validated
    against the live registry (manifest exists, not ``$HOME``). Never accepts a
    client path, only an index, so the dashboard can't be steered to an arbitrary
    directory."""
    try:
        i = int(index)
    except (TypeError, ValueError):
        return None
    # Filter private (same as the switcher) so indices align and a private
    # project is never focusable from another dashboard's switcher.
    for entry in list_projects(home, include_private=False):
        if entry["index"] == i:
            return Path(entry["path"])
    return None


# --- commands ---------------------------------------------------------------


def do_init(
    repo: Path, source: Path, dry_run: bool, force: bool = False, home: Optional[Path] = None,
) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    existing = load_manifest(paths.manifest)
    if dry_run:
        with tempfile.TemporaryDirectory() as tmp:
            plan = _build_init_plan(paths, repo, source, Path(tmp))
            pf = preflight(plan, existing, force=force)
        statuses = ["skipped" if c.status.value == "satisfied" else "applied" for c in pf.classified]
        return {
            "action": "init", "dry_run": True,
            "ops": [{"op": c.op.op, "dest": c.op.dest, "status": s}
                    for c, s in zip(pf.classified, statuses)],
            "summary": {"applied": statuses.count("applied"), "skipped": statuses.count("skipped")},
        }
    plan = _build_init_plan(paths, repo, source, paths.compiled / "project")
    manifest = existing or _new_manifest()
    outcomes = apply(plan, paths, manifest, force=force)
    manifest.persist(paths.manifest)
    _register_project(home if home is not None else Path.home(), repo)  # multi-project registry
    return {
        "action": "init", "dry_run": False,
        "ops": [{"op": o.op.op, "dest": o.op.dest, "status": o.status} for o in outcomes],
        "summary": _summary(outcomes),
        "diverged": sum(getattr(o, "diverged", 0) for o in outcomes),
    }


def do_context_refresh(repo: Path, dry_run: bool, force: bool = False) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    r = refresh_project_context(paths, dry_run=dry_run, force=force)
    if "error" in r:
        return {"action": "context-refresh", "error": r["error"]}
    return {"action": "context-refresh", "dry_run": dry_run, **r}


def do_snapshot(repo: Path, dry_run: bool, refresh_index: bool) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists():
        return {"action": "snapshot", "error": "not a Cohort project (run cohort init)"}
    sessions_dir = paths.cohort_home / "sessions"
    filename = f"{_utc_compact()}-{_short_id()}.md"
    content = render_snapshot_entry(repo)
    if dry_run:
        return {"action": "snapshot", "dry_run": True, "file": filename}
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / filename).write_text(content, encoding="utf-8")
    result = {"action": "snapshot", "dry_run": False, "file": filename}
    if refresh_index:
        result["index"] = do_context_refresh(repo, dry_run=False)
    return result


def do_deinit(
    repo: Path, purge: bool, dry_run: bool, home: Optional[Path] = None,
) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        return {"action": "deinit", "nothing": True}
    if dry_run:
        would = [
            {"op": o.op, "dest": o.dest, "action": "keep" if (o.preserve and not purge) else "remove"}
            for o in manifest.ops
        ]
        return {"action": "deinit", "dry_run": True, "purge": purge, "ops": would}
    result = reverse_full(manifest, paths, purge=purge)
    if purge and paths.cohort_home.exists():
        import shutil

        shutil.rmtree(paths.cohort_home)
    _deregister_project(home if home is not None else Path.home(), repo)  # drop from registry
    return {
        "action": "deinit", "dry_run": False, "purge": purge,
        "summary": {"removed": result.removed, "restored": result.restored,
                    "dirs_removed": result.dirs_removed, "preserved": result.skipped},
    }


# --- staleness --------------------------------------------------------------


def _read_staleness_hours(paths: CohortPaths) -> float:
    try:
        return float(read_project_config(paths).get("staleness_hours", 24))
    except (TypeError, ValueError):  # bad value falls back to the default
        return 24.0


def _newest_activity(paths: CohortPaths) -> Optional[float]:
    candidates = []
    ctx = paths.cohort_home / "project_context.md"
    if ctx.exists():
        candidates.append(ctx.stat().st_mtime)
    sessions_dir = paths.cohort_home / "sessions"
    if sessions_dir.exists():
        candidates.extend(p.stat().st_mtime for p in sessions_dir.glob("*.md"))
    return max(candidates) if candidates else None


def staleness_check(cwd: Path) -> Optional[str]:
    """Return a warning string if the repo's context is stale (and not throttled).

    Read-only except for a machine-local daily-throttle marker in ``state/``.
    Returns None outside a Cohort repo, when fresh, or when already warned today.
    """
    repo = find_repo_root(cwd)
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists():
        return None  # not a Cohort project
    newest = _newest_activity(paths)
    if newest is None:
        return None
    age_hours = (_utc_now().timestamp() - newest) / 3600.0
    if age_hours < _read_staleness_hours(paths):
        return None
    # throttle to once per UTC calendar day per machine
    today = _utc_now().strftime("%Y-%m-%d")
    marker = paths.state / ".staleness-warned"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
        return None
    if paths.state.exists():
        marker.write_text(today, encoding="utf-8")
    return (
        f"cohort: project context is stale (>{_read_staleness_hours(paths):g}h since last "
        f"activity). Consider `cohort snapshot`."
    )


# --- session capture (default-on observation, opt-out per repo) --------------


def _read_auto_capture(paths: CohortPaths) -> bool:
    # Default-on (opt-out): exit context is captured unless the repo sets false.
    return bool(read_project_config(paths).get("auto_capture", True))


def render_auto_capture_entry(repo: Path) -> str:
    """A machine-generated session record: frontmatter + the change summary only.

    No placeholder sections a human must fill — `cohort snapshot` remains the
    rich, human-authored entry."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    changed = _git(repo, "diff", "--stat") or _git(repo, "status", "--short") or "(no changes)"
    fm = dump_frontmatter(
        [("timestamp", now_iso()), ("branch", branch), ("captured", "auto")]
    ).rstrip("\n")
    return f"{fm}\n## Changed\n```\n{changed}\n```\n"


def session_capture(cwd: Path) -> Optional[str]:
    """Write an automatic session record unless this repo opted out; else do nothing.

    The compiled ``session_end`` hook calls this on every session end. Default-on
    (``auto_capture`` absent or true); a repo that sets ``auto_capture = false`` in
    ``.cohort/cohort.toml`` opts out. Returns the relative path written, else None.
    """
    repo = find_repo_root(cwd)
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists() or not _read_auto_capture(paths):
        return None
    sessions_dir = paths.cohort_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_utc_compact()}-{_short_id()}-auto.md"
    (sessions_dir / filename).write_text(render_auto_capture_entry(repo), encoding="utf-8")
    return f"sessions/{filename}"


_RECALL_MARKER = "session-recall.marker"

SESSION_RECALL_TEMPLATE = (
    "A prior session in this repo was auto-captured to {path} ({where}, {when}) but "
    "its context was never carried into durable memory. If you are continuing that "
    "work, read that record now and promote the key decisions, in-flight state, and "
    "open questions into your persistent memory directory before proceeding — the "
    "record is a mechanical change summary, not the reasoning, so reconstruct what "
    "still matters. If this is unrelated work, ignore it. (Surfaced once per record.)"
)


def _record_meta(path: Path) -> dict[str, str]:
    """Best-effort ``branch``/``timestamp`` from a session record's frontmatter."""
    meta: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return meta
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 2:
            for line in parts[1].splitlines():
                key, sep, val = line.partition(":")
                if sep:
                    meta[key.strip()] = val.strip().strip("'\"")
    return meta


def session_recall(cwd: Path, source: str) -> Optional[str]:
    """Return a one-time recall nudge if a fresh, not-yet-surfaced auto session
    record exists for this repo; else None.

    The exit→next-session bridge: ``session_capture`` writes a record at session
    end, and this surfaces it at the *next* start (the only moment a model turn
    exists) so its context can be promoted into durable memory. Silent on the
    ``compact`` source — ``compact-recall`` owns the post-compaction path, and a
    compaction also writes an auto record that would otherwise double-surface here.

    Idempotent via a machine-local marker under ``state/`` (git-ignored): each
    record is surfaced at most once, so ordinary session starts stay quiet."""
    if source == "compact":
        return None
    repo = find_repo_root(cwd)
    paths = CohortPaths.for_project(repo)
    sessions_dir = paths.cohort_home / "sessions"
    if not paths.cohort_home.exists() or not sessions_dir.is_dir():
        return None
    records = list(sessions_dir.glob("*.md"))
    if not records:
        return None
    newest = max(records, key=lambda p: p.stat().st_mtime)
    marker = paths.cohort_home / "state" / _RECALL_MARKER
    already = ""
    if marker.exists():
        try:
            already = marker.read_text(encoding="utf-8").strip()
        except OSError:
            already = ""
    if newest.name == already:
        return None  # this record was already surfaced once
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(newest.name, encoding="utf-8")
    meta = _record_meta(newest)
    where = f"branch {meta['branch']}" if meta.get("branch") else "an earlier session"
    when = meta.get("timestamp", "recently")
    return SESSION_RECALL_TEMPLATE.format(
        path=f"sessions/{newest.name}", where=where, when=when
    )


# --- working memory (mid-session scratch, consolidated at boundaries) ---------
#
# The exit-capture record (above) is deterministic but mechanical — it can't hold the
# *reasoning*, because at exit there is no model turn. Working memory closes that: the
# model stages semantic notes *during* the session (`working_note`, at task milestones)
# and a Stop-hook stages a mechanical record when a turn changed the tree
# (`working_capture`). Both land in a git-ignored, disposable staging dir. At the next
# compaction or session start the recall hooks surface the staged notes so the durable
# ones are promoted into memory and the rest cleared. Cheap continuous capture, curated
# at the boundary — so an abrupt exit no longer loses the "why".

_WORKING_DIRNAME = "working-memory"
_WORKING_CAPTURE_MARKER = ".last-capture"


def _working_dir(paths: CohortPaths) -> Path:
    return paths.cohort_home / "state" / _WORKING_DIRNAME


def pending_working_notes(cwd: Path) -> list[Path]:
    """The working-memory notes staged for this repo (empty if none/opted out)."""
    paths = CohortPaths.for_project(find_repo_root(cwd))
    d = _working_dir(paths)
    return sorted(d.glob("*.md")) if d.is_dir() else []


def working_note(cwd: Path, text: str) -> Optional[str]:
    """Stage a model-authored working note mid-session (semantic scratch).

    Called by the model at task milestones when a task produced durable context.
    Governed by the same ``auto_capture`` opt-out as session capture; disposable and
    git-ignored. Returns the relative path written, else None."""
    text = text.strip()
    if not text:
        return None
    repo = find_repo_root(cwd)
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists() or not _read_auto_capture(paths):
        return None
    d = _working_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    fm = dump_frontmatter(
        [("timestamp", now_iso()), ("branch", branch), ("kind", "note")]
    ).rstrip("\n")
    filename = f"{_utc_compact()}-{_short_id()}-note.md"
    (d / filename).write_text(f"{fm}\n{text}\n", encoding="utf-8")
    return f"state/{_WORKING_DIRNAME}/{filename}"


def working_capture(cwd: Path) -> Optional[str]:
    """Stage a mechanical working record for the current turn — the deterministic Stop
    backstop — but only when the turn actually changed the tree, and not twice for the
    same unchanged state (a hash marker dedupes). Governed by ``auto_capture``. Returns
    the relative path written, else None."""
    repo = find_repo_root(cwd)
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists() or not _read_auto_capture(paths):
        return None
    changed = _git(repo, "diff", "--stat") or _git(repo, "status", "--short")
    if not changed:
        return None  # a turn that touched nothing on disk is not worth staging
    d = _working_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(changed.encode("utf-8")).hexdigest()
    marker = d / _WORKING_CAPTURE_MARKER
    if marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == digest:
                return None  # tree unchanged since the last capture — skip the duplicate
        except OSError:
            pass
    marker.write_text(digest, encoding="utf-8")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    fm = dump_frontmatter(
        [("timestamp", now_iso()), ("branch", branch), ("kind", "auto")]
    ).rstrip("\n")
    filename = f"{_utc_compact()}-{_short_id()}-auto.md"
    (d / filename).write_text(f"{fm}\n## Changed\n```\n{changed}\n```\n", encoding="utf-8")
    return f"state/{_WORKING_DIRNAME}/{filename}"


def working_memory_review(cwd: Path) -> str:
    """A consolidation directive if working notes are staged for this repo, else ''.

    Appended to the recall injections so that, at the next compaction/start, the model
    promotes the durable staged notes into memory and clears the scratch."""
    try:
        notes = pending_working_notes(cwd)
    except Exception:  # noqa: BLE001 - recall must never break on a stat error
        return ""
    if not notes:
        return ""
    return (
        f"\n\nWorking memory: {len(notes)} staged note(s) in "
        f"`.cohort/state/{_WORKING_DIRNAME}/` from earlier in this work — semantic notes "
        "and mechanical per-turn records that survived even an abrupt exit. Read them, "
        "promote anything durable into your persistent memory directory, then delete "
        "them. They are disposable scratch, not the record of truth."
    )

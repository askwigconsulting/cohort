"""Project-scope commands: init, snapshot, context refresh, deinit, staleness.

Builds on the base-parameterized executor (project paths) and the merge op at
project scope. The git-tracked content (`project_context.md`, `sessions/`,
`cohort.toml`) is scaffolded (create-if-absent, preserved on deinit); the wiring
(`state/`, `compiled/`, the `.claude/CLAUDE.md` @import block, `.gitignore`) is
Cohort-owned and reversed on deinit.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .executor import apply, preflight, reverse_full
from .install_model import CohortPaths, Op, OpType
from .loader import load_artifact
from .manifest import Manifest, load_manifest, new_install_id, now_iso

PROJECT_IDE = "project"
IMPORT_LINE = "@import ../.cohort/project_context.md"
GITIGNORE_CONTENT = "# Cohort machine-local bookkeeping (do not commit)\nstate/\ncompiled/\n"
COHORT_TOML_CONTENT = "# Cohort project config (git-tracked, shared)\nstaleness_hours = 24\n"
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


# --- templates & payloads ---------------------------------------------------


def context_template(source: Path) -> str:
    """The scaffolded ``project_context.md`` body (canonical context template)."""
    result = load_artifact(source / "canonical" / "contexts" / "project-context.md")
    return result.body


def sessions_index(paths: CohortPaths) -> str:
    """Deterministic newest-first index of ``sessions/`` (the managed block body)."""
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


def render_snapshot_entry(repo: Path) -> str:
    """A dated session entry: frontmatter + Changed/Decisions/Open items/Notes."""
    author = _git(repo, "config", "user.name") or "unknown"
    email = _git(repo, "config", "user.email")
    if email:
        author = f"{author} <{email}>"
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    changed = _git(repo, "diff", "--stat") or _git(repo, "status", "--short") or "(no changes)"
    fm = [
        "---",
        f"timestamp: {now_iso()}",
        f"author: {author}",
        f"branch: {branch}",
        "---",
    ]
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


def _build_init_plan(paths: CohortPaths, repo: Path, source: Path, stage_dir: Path) -> list[Op]:
    cohort_home = paths.cohort_home
    project_context = cohort_home / "project_context.md"
    claude_md = repo / ".claude" / "CLAUDE.md"
    srcs = {
        "gitignore": _stage(stage_dir, "gitignore", GITIGNORE_CONTENT),
        "toml": _stage(stage_dir, "cohort.toml", COHORT_TOML_CONTENT),
        "context": _stage(stage_dir, "project_context.md", context_template(source)),
        "index": _stage(stage_dir, "sessions-index.txt", sessions_index(paths)),
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


# --- commands ---------------------------------------------------------------


def do_init(repo: Path, source: Path, dry_run: bool, force: bool = False) -> dict[str, Any]:
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
    return {
        "action": "init", "dry_run": False,
        "ops": [{"op": o.op.op, "dest": o.op.dest, "status": o.status} for o in outcomes],
        "summary": _summary(outcomes),
        "diverged": sum(getattr(o, "diverged", 0) for o in outcomes),
    }


def do_context_refresh(repo: Path, dry_run: bool, force: bool = False) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        return {"action": "context-refresh", "error": "not a Cohort project (run cohort init)"}
    project_context = paths.cohort_home / "project_context.md"
    index = sessions_index(paths)
    stage_dir = Path(tempfile.mkdtemp()) if dry_run else (paths.compiled / "project")
    src = _stage(stage_dir, "sessions-index.txt", index)
    merge_op = Op(OpType.MERGE.value, PROJECT_IDE, str(project_context),
                  src=src, strategy="block", preserve=True)
    if dry_run:
        pf = preflight([merge_op], manifest, force=force)
        changed = pf.classified[0].status.value != "satisfied"
        return {"action": "context-refresh", "dry_run": True, "changed": changed}
    outcomes = apply([merge_op], paths, manifest, force=force)
    manifest.persist(paths.manifest)
    return {
        "action": "context-refresh", "dry_run": False,
        "changed": any(o.status == "applied" for o in outcomes),
        "diverged": sum(getattr(o, "diverged", 0) for o in outcomes),
    }


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


def do_deinit(repo: Path, purge: bool, dry_run: bool) -> dict[str, Any]:
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
    return {
        "action": "deinit", "dry_run": False, "purge": purge,
        "summary": {"removed": result.removed, "restored": result.restored,
                    "dirs_removed": result.dirs_removed, "preserved": result.skipped},
    }


# --- staleness --------------------------------------------------------------


def _read_staleness_hours(paths: CohortPaths) -> float:
    toml_path = paths.cohort_home / "cohort.toml"
    if not toml_path.exists():
        return 24.0
    try:
        import tomllib

        with open(toml_path, "rb") as fh:
            return float(tomllib.load(fh).get("staleness_hours", 24))
    except Exception:  # noqa: BLE001 - bad config falls back to the default
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

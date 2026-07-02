"""``install`` / ``uninstall`` orchestration: selection, plan, reports.

Builds the global-home op set (per-IDE adapter ops are empty in Phase 1) and
hands it to the executor. Exceptions carry the intended exit code; the CLI maps
them.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TextIO

from .executor import (
    ClobberRefused,
    Preflight,
    ReverseResult,
    _reverse_place_ops,
    apply,
    path_hash,
    preflight,
    reverse_full,
    reverse_slice,
)
from .install_model import (
    GLOBAL_IDE,
    IDE_VALUES,
    IDES,
    CohortPaths,
    Op,
    OpOutcome,
    OpStatus,
    OpType,
    resolve_mode,
)
from .manifest import Manifest, load_manifest, new_install_id, now_iso


class UsageError(Exception):
    """A usage error → exit 2."""


class CancelledSelection(Exception):
    """The interactive picker was cancelled → exit 0 no-op."""


# --- IDE selection ----------------------------------------------------------


def parse_ide(value: str) -> list[str]:
    """Parse a ``--ide`` value into a deduped IDE list; ``all`` expands."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise UsageError("empty --ide selection")
    out: list[str] = []
    for p in parts:
        if p not in IDE_VALUES:
            raise UsageError(f"unknown ide {p!r}; choose from {', '.join(IDE_VALUES)}")
        targets = IDES if p == "all" else (p,)
        for ide in targets:
            if ide not in out:
                out.append(ide)
    return out


def _isatty() -> bool:
    """Whether both stdin and stdout are TTYs (patchable in tests)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def prompt_ide_selection(
    stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None
) -> Optional[list[str]]:
    """Minimal numbered stdin picker (no dependency).

    Protocol (F): numbers (space/comma separated) + Enter selects; an empty line
    confirms an empty selection (returns ``[]`` → exit 2); ``q`` or EOF cancels
    (returns ``None`` → exit 0).
    """
    sin = stdin or sys.stdin
    sout = stdout or sys.stdout
    print("Select IDEs to install:", file=sout)
    for i, ide in enumerate(IDES, 1):
        print(f"  {i}) {ide}", file=sout)
    print("Enter numbers (space/comma separated); empty = none; q = cancel:", file=sout)
    line = sin.readline()
    if line == "":  # EOF
        return None
    stripped = line.strip()
    if stripped.lower() == "q":
        return None
    if stripped == "":
        return []
    selected: list[str] = []
    for token in re.split(r"[ ,]+", stripped):
        if not token:
            continue
        if not token.isdigit() or not (1 <= int(token) <= len(IDES)):
            raise UsageError(f"invalid selection {token!r}")
        ide = IDES[int(token) - 1]
        if ide not in selected:
            selected.append(ide)
    return selected


def resolve_selection(value: Optional[str]) -> list[str]:
    """Resolve the effective IDE selection from the flag, picker, or TTY policy."""
    if value is not None:
        return parse_ide(value)
    if _isatty():
        result = prompt_ide_selection()
        if result is None:
            raise CancelledSelection()
        if not result:
            raise UsageError("no IDEs selected")
        return result
    raise UsageError("specify --ide (no interactive terminal)")


def merge_ides(existing: list[str], new: list[str]) -> list[str]:
    """Additive merge, deduped, first-seen order preserved."""
    out = list(existing)
    for ide in new:
        if ide not in out:
            out.append(ide)
    return out


# --- Plan building ----------------------------------------------------------


def build_global_plan(paths: CohortPaths, source: Path, mode: str) -> list[Op]:
    """The global-home ops every install produces (decision M5)."""
    place_op = OpType.COPY.value if mode == "copy" else OpType.LINK.value
    canonical_src = str((source / "canonical").resolve())
    return [
        Op(op=OpType.MKDIR.value, ide=GLOBAL_IDE, dest=str(paths.cohort_home)),
        Op(op=OpType.MKDIR.value, ide=GLOBAL_IDE, dest=str(paths.state)),
        Op(op=place_op, ide=GLOBAL_IDE, dest=str(paths.canonical), src=canonical_src),
    ]


def adapter_ops(ides: list[str], paths: CohortPaths, source: Path, mode: str) -> list[Op]:
    """Per-IDE ops placing each IDE's *staged* files (Phase 2).

    Reads existing staging only — ``install`` never compiles. A selected IDE with
    no staging contributes nothing (the caller surfaces a "run recompile" hint).
    """
    from .compile import scan_staging_ops

    ops: list[Op] = []
    for ide in ides:
        ops += scan_staging_ops(paths, ide, mode)
    return ops


def _existing_global_mode(existing, paths: CohortPaths) -> Optional[str]:
    """The op type already recorded for the shared canonical artifact, if any."""
    if existing is None:
        return None
    for o in existing.ops:
        if o.ide == GLOBAL_IDE and o.dest == str(paths.canonical) and o.op in (
            OpType.LINK.value,
            OpType.COPY.value,
        ):
            return "copy" if o.op == OpType.COPY.value else "link"
    return None


# --- Reports ----------------------------------------------------------------


@dataclass
class OpRecord:
    op: Op
    status: str

    def to_dict(self) -> dict[str, Any]:
        d = self.op.to_dict()
        d["status"] = self.status
        return d


@dataclass
class InstallReport:
    mode: str
    ides: list[str]
    records: list[OpRecord]
    install_id: Optional[str]
    dry_run: bool
    staging_missing: list[str] = field(default_factory=list)
    diverged: int = 0  # merge entries left untouched (user-edited) — skip+warn

    @property
    def summary(self) -> dict[str, int]:
        return {
            "applied": sum(1 for r in self.records if r.status == "applied"),
            "skipped": sum(1 for r in self.records if r.status == "skipped"),
            "backed_up": sum(1 for r in self.records if r.status == "backup"),
            "removed": sum(1 for r in self.records if r.status in ("removed", "restored")),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": "install",
            "mode": self.mode,
            "ides": list(self.ides),
            "install_id": self.install_id,
            "ops": [r.to_dict() for r in self.records],
            "summary": self.summary,
            "staging_missing": list(self.staging_missing),
            "diverged": self.diverged,
        }


@dataclass
class UninstallReport:
    ides: list[str]
    records: list[OpRecord]
    dry_run: bool
    nothing: bool = False

    @property
    def summary(self) -> dict[str, int]:
        return {
            "removed": sum(1 for r in self.records if r.status == "removed"),
            "restored": sum(1 for r in self.records if r.status == "restored"),
            "dirs_removed": sum(1 for r in self.records if r.status == "dir_removed"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": "uninstall",
            "ides": list(self.ides),
            "ops": [r.to_dict() for r in self.records],
            "summary": self.summary,
        }


def _classified_to_records(pf: Preflight, force: bool) -> list[OpRecord]:
    records: list[OpRecord] = []
    for c in pf.classified:
        if c.status == OpStatus.SATISFIED:
            records.append(OpRecord(c.op, "skipped"))
        elif c.status == OpStatus.APPLY:
            records.append(OpRecord(c.op, "applied"))
        else:  # CLOBBER under --force (refusal already raised otherwise)
            records.append(OpRecord(c.op, "backup"))
            records.append(OpRecord(c.op, "applied"))
    return records


def _outcomes_to_records(outcomes: list[OpOutcome]) -> list[OpRecord]:
    return [OpRecord(o.op, o.status) for o in outcomes]


# --- install ----------------------------------------------------------------


def do_install(
    home: Path,
    selection: list[str],
    mode: str,
    force: bool,
    source: Path,
    dry_run: bool,
    prune_stale: bool = False,
    fresh_dests: Optional[set[str]] = None,
    fresh_ides: Optional[set[str]] = None,
) -> InstallReport:
    """Plan and (unless dry-run) apply an install. Raises ClobberRefused/exit 1.

    ``prune_stale`` (only the compile-then-install callers — ``recompile`` /
    ``setup`` — pass it) removes artifacts an IDE placed before but that the
    fresh compile no longer produces: an agent dropped from a tailored roster,
    or a canonical file deleted upstream. Plain ``install`` never prunes, so a
    partial/missing staging can never be read as "everything left the office".
    ``fresh_dests`` is the authoritative post-compile dest set (from the caller's
    in-memory results, so a dry-run plans correctly without writing staging);
    ``fresh_ides`` limits pruning to IDEs that actually recompiled to non-empty
    output, so a compile that yields nothing for an IDE never wipes it.
    """
    paths = CohortPaths(home)
    existing = load_manifest(paths.manifest)
    # The shared global home's mode is fixed by the first install; a later --copy
    # applies to new per-IDE ops and never re-flips the shared canonical (decision
    # J, the natural consequence of S2: per-op type governs, mode is informational;
    # a mode conversion is a deliberate uninstall+reinstall, not an add-IDE side
    # effect).
    global_mode = _existing_global_mode(existing, paths) or mode
    plan = build_global_plan(paths, source, global_mode) + adapter_ops(
        selection, paths, source, mode
    )
    pf = preflight(plan, existing, force)
    if pf.clobbers and not force:
        raise ClobberRefused(pf.clobbers)

    merged = merge_ides(existing.ides if existing else [], selection)
    staging_missing = [ide for ide in selection if not paths.compiled_ide(ide).exists()]
    planned = fresh_dests if fresh_dests is not None else {op.dest for op in plan}
    stale = (
        _stale_placed_ops(existing, planned, fresh_ides or set(selection), paths)
        if prune_stale else []
    )
    if dry_run:
        return InstallReport(
            mode=mode,
            ides=merged,
            records=_classified_to_records(pf, force)
            + [OpRecord(o, "removed") for o in stale],
            install_id=None,
            dry_run=True,
            staging_missing=staging_missing,
        )

    if existing is not None:
        manifest = existing
        manifest.mode = mode
        manifest.ides = merged
    else:
        manifest = Manifest(
            install_id=new_install_id(), created_at=now_iso(), mode=mode, ides=merged, ops=[]
        )
    outcomes = apply(plan, paths, manifest, force)
    outcomes += _remove_stale_placed(stale, manifest, paths)
    manifest.persist(paths.manifest)  # ensure ides/mode update lands even if all skipped
    return InstallReport(
        mode=mode,
        ides=merged,
        records=_outcomes_to_records(outcomes),
        install_id=manifest.install_id,
        dry_run=False,
        staging_missing=staging_missing,
        diverged=sum(o.diverged for o in outcomes),
    )


def _stale_placed_ops(
    existing: Optional[Manifest], planned: set[str], prune_ides: set[str], paths: CohortPaths
) -> list[Op]:
    """Recorded placement ops (and their paired backups) an IDE no longer produces.

    Returns the ops in manifest order so a LIFO reverse removes each placed
    link/copy and *then* restores any ``--force`` backup parked at that dest —
    the same restore path a slice uninstall takes. Only LINK/COPY ops whose src
    points into ``compiled/`` are candidates (the shared canonical link and merges
    are never staged placements), and only for IDEs in ``prune_ides`` (those that
    recompiled to real output). Backup ops for a stale dest are pulled in so the
    user's displaced original is not stranded.
    """
    if existing is None:
        return []
    staged_root = str(paths.compiled) + os.sep
    stale_dests = {
        o.dest
        for o in existing.ops
        if o.ide in prune_ides
        and o.op in (OpType.LINK.value, OpType.COPY.value)
        and o.dest not in planned
        and (o.src or "").startswith(staged_root)
    }
    if not stale_dests:
        return []
    return [
        o
        for o in existing.ops
        if o.dest in stale_dests
        and o.op in (OpType.LINK.value, OpType.COPY.value, OpType.BACKUP.value)
    ]


def _remove_stale_placed(stale: list[Op], manifest: Manifest, paths: CohortPaths) -> list[OpOutcome]:
    """Reverse the stale slice (ownership-checked, LIFO) and drop only the ops it
    actually acted on. An op whose reversal was *skipped* (a user re-pointed link
    or edited copy failed the ownership check) stays in the manifest so Cohort
    keeps tracking it — never silently forgotten."""
    if not stale:
        return []
    result = ReverseResult()
    _reverse_place_ops(stale, result, purge=True)
    acted = {id(o.op) for o in result.outcomes}  # removed/restored ops only
    manifest.ops = [o for o in manifest.ops if id(o) not in acted]
    return result.outcomes


def do_install_project(repo: Path, mode: Optional[str] = None) -> dict[str, Any]:
    """Compile ``<repo>/.cohort/canonical/`` at *project* scope and place it into
    ``<repo>/.claude/`` via the project manifest — reversible, and isolated from the
    global office (the executor's base is the repo, never ``$HOME``).

    Claude-only for now (the project tier hardcodes claude, like ``add-specialist``).
    ``project_tier=True`` disables office-directory injection (no project
    generalist) and the CLAUDE.md memory merge — that managed block is owned by
    ``cohort init`` (the ``@import`` of ``project_context.md``). Mirrors
    ``do_install``'s discipline: preflight-refuse before any mutation, and prune
    placements the fresh compile no longer produces (only when it produced
    something — an empty compile never wipes the tier). Refuses to run while
    unmigrated pre-unification sources sit in ``.cohort/agents/``: rebuilding
    staging without compiling them would dangle their placed links.
    """
    from .compile import (  # lazy: avoid import cycle
        compile_ide,
        planned_dests,
        scan_staging_ops,
        write_staging,
    )

    ppaths = CohortPaths.for_project(repo)
    if not ppaths.manifest.exists():
        raise UsageError("not a Cohort project; run `cohort init` first")
    legacy = sorted((ppaths.cohort_home / "agents").glob("*.md"))
    if legacy:
        names = ", ".join(p.stem for p in legacy)
        raise UsageError(
            f"unmigrated project specialists in .cohort/agents/ ({names}) — run "
            f"`git mv .cohort/agents/<name>.md .cohort/canonical/agents/<name>.md` first"
        )
    if not (ppaths.cohort_home / "canonical").exists():
        return {"action": "project-recompile", "ide": "claude", "staged": [], "applied": 0}

    ide = "claude"
    mode = mode or resolve_mode(False)  # Windows-safe default (copy without symlink rights)
    result = compile_ide(ppaths.cohort_home, ide, scope="project", project_tier=True)
    write_staging(ppaths, result)
    plan = scan_staging_ops(ppaths, ide, mode)
    manifest = load_manifest(ppaths.manifest)
    pf = preflight(plan, manifest, force=False)
    if pf.clobbers:
        raise ClobberRefused(pf.clobbers)
    stale = _stale_placed_ops(
        manifest, planned_dests(ppaths, [result]), {ide} if result.staged else set(), ppaths
    )
    outcomes = apply(plan, ppaths, manifest, force=False)
    stale_outcomes = _remove_stale_placed(stale, manifest, ppaths)
    manifest.persist(ppaths.manifest)
    return {
        "action": "project-recompile",
        "ide": ide,
        "staged": [s.staged_rel for s in result.staged],
        "applied": sum(1 for o in outcomes if o.status == "applied"),
        "pruned": sum(1 for o in stale_outcomes if o.status == "removed"),
    }


# --- uninstall --------------------------------------------------------------


def _simulate_reverse(
    manifest: Manifest, paths: CohortPaths, selection: Optional[list[str]]
) -> list[OpRecord]:
    """Read-only preview of a reverse (for --dry-run); mutates nothing."""
    from .executor import _symlink_points_to  # local import: preview-only helper

    if selection:
        ops = [o for o in manifest.ops if o.ide in selection and o.ide in manifest.ides]
        full = False
    else:
        ops = list(manifest.ops)
        full = True

    removed: set[Path] = set()
    records: list[OpRecord] = []
    for op in reversed([o for o in ops if o.op != OpType.MKDIR.value]):
        dest = Path(op.dest)
        if op.op == OpType.LINK.value:
            owned = _symlink_points_to(dest, op.src or "")
            records.append(OpRecord(op, "removed" if owned else "skipped"))
            if owned:
                removed.add(dest)
        elif op.op == OpType.COPY.value:
            owned = (
                not dest.is_symlink()
                and dest.exists()
                and op.tree_hash is not None
                and path_hash(dest) == op.tree_hash
            )
            records.append(OpRecord(op, "removed" if owned else "skipped"))
            if owned:
                removed.add(dest)
        elif op.op == OpType.BACKUP.value:
            free = (not (dest.exists() or dest.is_symlink())) or dest in removed
            ok = free and Path(op.backup or "").exists()
            records.append(OpRecord(op, "restored" if ok else "skipped"))
        elif op.op == OpType.MERGE.value:
            records.append(OpRecord(op, "removed" if _merge_reversible(op) else "skipped"))

    if full:
        # Full teardown deletes these outside the op model before rmdir'ing state/.
        removed.add(paths.manifest)
        removed.add(paths.backups / manifest.install_id)
        removed.add(paths.backups)
    for op in reversed([o for o in ops if o.op == OpType.MKDIR.value and o.created]):
        d = Path(op.dest)
        if d.is_dir():
            remaining = set(d.iterdir()) - removed
            empty = not remaining
            records.append(OpRecord(op, "dir_removed" if empty else "skipped"))
            if empty:
                removed.add(d)
        else:
            records.append(OpRecord(op, "skipped"))
    return records


def _merge_reversible(op: Op) -> bool:
    """Read-only check: would a reverse of this merge op remove anything?"""
    import json as _j

    from . import merge as _m

    dest = Path(op.dest)
    if not dest.exists():
        return False
    if op.strategy == "block":
        inner = _m.extract_block(dest.read_text(encoding="utf-8"))
        return inner is not None and _m.block_hash(inner) == op.block_hash
    existing = _j.loads(dest.read_text(encoding="utf-8"))
    _new, removed, _skipped = _m.remove_tagged(existing, op.tags or [])
    return removed > 0


def do_uninstall(
    home: Path, selection: Optional[list[str]], dry_run: bool
) -> UninstallReport:
    """Reverse an install (whole or per-IDE slice)."""
    paths = CohortPaths(home)
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        return UninstallReport(ides=[], records=[], dry_run=dry_run, nothing=True)

    if dry_run:
        return UninstallReport(
            ides=selection or list(manifest.ides),
            records=_simulate_reverse(manifest, paths, selection),
            dry_run=True,
        )

    if selection:
        outcomes: list[OpOutcome] = []
        for ide in selection:
            outcomes += reverse_slice(manifest, paths, ide).outcomes
        return UninstallReport(
            ides=selection, records=_outcomes_to_records(outcomes), dry_run=False
        )

    affected = list(manifest.ides)
    result = reverse_full(manifest, paths)
    return UninstallReport(
        ides=affected, records=_outcomes_to_records(result.outcomes), dry_run=False
    )

"""The two-pass install executor: preflight → apply | dry-run | reverse.

Guarantees:
- **Atomic refusal** — a read-only preflight classifies every op; if any foreign
  ``dest`` would be clobbered and ``--force`` is unset, nothing is written.
- **Idempotency** — already-satisfied ops are skipped; a re-run applies nothing.
- **Reversibility** — every applied op is recorded (incrementally, fsync'd); a
  reverse replays the manifest LIFO, removing only artifacts still owned by
  Cohort and restoring backups.
- **Apply-time re-check** — each dest is re-classified at write time; one that
  turned foreign since preflight is refused, never clobbered (TOCTOU safety).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import merge
from .install_model import (
    ClassifiedOp,
    CohortPaths,
    Op,
    OpOutcome,
    OpStatus,
    OpType,
)
from .manifest import Manifest


class ClobberRefused(Exception):
    """Raised when foreign files block an install and ``--force`` is unset."""

    def __init__(self, clobbers: list[ClassifiedOp]) -> None:
        self.clobbers = clobbers
        names = ", ".join(c.op.dest for c in clobbers)
        super().__init__(f"refusing to overwrite pre-existing file(s): {names}")


# --- Hashing ----------------------------------------------------------------


def path_hash(path: Path) -> str:
    """Content hash of a file/dir/symlink. Symlinks hash their *target* string
    (never followed); directories hash sorted (name, child-hash) pairs."""
    h = hashlib.sha256()
    if path.is_symlink():
        h.update(b"L\0")
        h.update(os.readlink(path).encode("utf-8"))
    elif path.is_dir():
        h.update(b"D\0")
        for name in sorted(os.listdir(path)):
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(path_hash(path / name).encode("ascii"))
    elif path.is_file():
        h.update(b"F\0")
        h.update(path.read_bytes())
    else:
        h.update(b"?\0")
    return h.hexdigest()


def _symlink_points_to(dest: Path, src: str) -> bool:
    return dest.is_symlink() and Path(os.readlink(dest)) == Path(src)


# --- Classification (preflight) --------------------------------------------


def classify(
    op: Op,
    recorded_copy_hashes: dict[str, str],
    recorded_links: Optional[dict[str, str]] = None,
    prior_merge: Optional[dict[str, Op]] = None,
    force: bool = False,
) -> OpStatus:
    """Classify one op against current filesystem state.

    ``force`` only affects merge ops (it re-asserts a diverged/removed block or
    entry — the restore path); clobber/copy classification stays force-agnostic.
    """
    dest = Path(op.dest)
    if op.op == OpType.MKDIR.value:
        if dest.is_dir() and not dest.is_symlink():
            return OpStatus.SATISFIED
        if dest.is_symlink() or dest.exists():
            return OpStatus.CLOBBER  # a non-dir file where a dir is wanted (N1)
        return OpStatus.APPLY
    if op.op == OpType.LINK.value:
        if _symlink_points_to(dest, op.src or ""):
            return OpStatus.SATISFIED
        if dest.is_symlink():
            # A symlink the manifest records as ours is re-pointed in place (no
            # backup) when it is dangling or still points at our prior target —
            # e.g. the source clone moved/renamed. A symlink the user re-pointed
            # to some other live target is treated as foreign (CLOBBER).
            recorded_src = (recorded_links or {}).get(op.dest)
            if recorded_src is not None and (
                not dest.exists() or _symlink_points_to(dest, recorded_src)
            ):
                return OpStatus.APPLY
        if dest.is_symlink() or dest.exists():
            return OpStatus.CLOBBER
        return OpStatus.APPLY
    if op.op == OpType.COPY.value:
        if not dest.exists() and not dest.is_symlink():
            return OpStatus.APPLY
        if not dest.is_symlink() and path_hash(dest) == path_hash(Path(op.src or "")):
            return OpStatus.SATISFIED
        recorded = recorded_copy_hashes.get(op.dest)
        if recorded is not None and not dest.is_symlink() and path_hash(dest) == recorded:
            return OpStatus.APPLY  # our stale copy → overwrite, no backup
        return OpStatus.CLOBBER
    if op.op == OpType.MERGE.value:
        # Merging into a user-owned file is never a clobber; it's satisfied only
        # when re-merging changes nothing AND reports no divergence to warn about.
        plan = _plan_merge(op, (prior_merge or {}).get(op.dest), force)
        return OpStatus.SATISFIED if (not plan["changed"] and plan["skipped"] == 0) else OpStatus.APPLY
    if op.op == OpType.SCAFFOLD.value:
        # Create-if-absent: a team-owned file is never overwritten or clobbered.
        return OpStatus.SATISFIED if dest.exists() else OpStatus.APPLY
    raise ValueError(f"unknown op type: {op.op!r}")


def _plan_merge(op: Op, prior: Optional[Op], force: bool = False) -> dict:
    """Plan a merge op against the current file + the prior recorded identity."""
    dest = Path(op.dest)
    created = not dest.exists()
    if op.strategy == "block":
        text = dest.read_text(encoding="utf-8") if dest.exists() else ""
        desired = Path(op.src).read_text(encoding="utf-8")
        plan = merge.plan_block_merge(text, desired, prior.block_hash if prior else None, force)
        plan["created"] = created
        return plan
    # json
    fragment = json.loads(Path(op.src).read_text(encoding="utf-8"))
    existing = json.loads(dest.read_text(encoding="utf-8")) if dest.exists() else {}
    new_obj, owned, skipped = merge.merge_hooks(
        existing, fragment, prior.tags if prior else None, force
    )
    return {
        "new_obj": new_obj,
        "changed": new_obj != existing,
        "skipped": skipped,
        "tags": owned,
        "created": created,
    }


def _recorded_copy_hashes(manifest: Optional[Manifest]) -> dict[str, str]:
    if manifest is None:
        return {}
    return {
        o.dest: o.tree_hash
        for o in manifest.ops
        if o.op == OpType.COPY.value and o.tree_hash is not None
    }


def _prior_merge_ops(manifest: Optional[Manifest]) -> dict[str, Op]:
    """The merge op previously recorded per dest (its tags / block_hash)."""
    if manifest is None:
        return {}
    return {o.dest: o for o in manifest.ops if o.op == OpType.MERGE.value}


def _recorded_links(manifest: Optional[Manifest]) -> dict[str, str]:
    """dest → previously recorded link target, for Cohort-owned LINK ops. Lets a
    moved/renamed source self-heal (re-point) instead of refusing as a clobber."""
    if manifest is None:
        return {}
    return {o.dest: (o.src or "") for o in manifest.ops if o.op == OpType.LINK.value}


@dataclass
class Preflight:
    classified: list[ClassifiedOp]
    clobbers: list[ClassifiedOp] = field(default_factory=list)


def preflight(
    plan: list[Op], manifest: Optional[Manifest], force: bool
) -> Preflight:
    """Read-only classification of an entire plan. No filesystem mutation."""
    recorded = _recorded_copy_hashes(manifest)
    recorded_links = _recorded_links(manifest)
    prior_merge = _prior_merge_ops(manifest)
    classified: list[ClassifiedOp] = []
    clobbers: list[ClassifiedOp] = []
    for op in plan:
        status = classify(op, recorded, recorded_links, prior_merge, force)
        c = ClassifiedOp(op=op, status=status)
        if status == OpStatus.CLOBBER and not force:
            clobbers.append(c)
        classified.append(c)
    return Preflight(classified=classified, clobbers=clobbers)


# --- Apply ------------------------------------------------------------------


def _remove_path(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def _backup_path(paths: CohortPaths, install_id: str, dest: Path) -> Path:
    # Mirror the absolute dest under backups/<id>/ (anchor stripped).
    rel = Path(*dest.parts[1:]) if dest.is_absolute() else dest
    return paths.backups / install_id / rel


def apply(
    plan: list[Op],
    paths: CohortPaths,
    manifest: Manifest,
    force: bool,
) -> list[OpOutcome]:
    """Apply a preflight-clean plan, recording each op incrementally.

    Re-checks each dest at write time: an op that turned foreign since preflight
    is refused (raises ClobberRefused) rather than clobbered. The partial
    manifest written so far remains valid for a subsequent uninstall.
    """
    recorded = _recorded_copy_hashes(manifest)
    recorded_links = _recorded_links(manifest)
    prior_merge = _prior_merge_ops(manifest)  # snapshot before we mutate the manifest
    outcomes: list[OpOutcome] = []
    for op in plan:
        if op.op == OpType.MERGE.value:
            outcomes.append(_apply_merge(op, paths, manifest, prior_merge.get(op.dest), force))
            continue
        status = classify(op, recorded, recorded_links)
        if status == OpStatus.SATISFIED:
            outcomes.append(OpOutcome(op=op, status="skipped"))
            continue
        dest = Path(op.dest)
        if status == OpStatus.CLOBBER:
            if not force:
                raise ClobberRefused([ClassifiedOp(op=op, status=status)])
            backup_op = _inject_backup(op, paths, manifest, dest)
            outcomes.append(OpOutcome(op=backup_op, status="backup"))
        outcomes.append(_place(op, paths, manifest, recorded))
    return outcomes


def _apply_merge(
    op: Op, paths: CohortPaths, manifest: Manifest, prior: Optional[Op], force: bool = False
) -> OpOutcome:
    """Apply a merge op, honoring divergence on re-merge (decision K).

    A canonical entry the user has edited/removed is left untouched (never
    re-added/overwritten) unless ``force`` re-asserts it; the recorded op carries
    the identity reverse needs. The per-dest merge op is *replaced* in the
    manifest, not appended.
    """
    plan = _plan_merge(op, prior, force)
    dest = Path(op.dest)
    # `created` is sticky: if Cohort created the file at first install, that holds
    # across recompiles even though the file now exists.
    created = prior.created if prior is not None else plan["created"]
    if op.strategy == "block":
        if plan["changed"]:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(plan["new_text"], encoding="utf-8")
        recorded_op = Op(
            op=op.op, ide=op.ide, dest=op.dest, strategy="block",
            created=created, block_hash=plan["block_hash"], preserve=op.preserve,
        )
    else:  # json
        if plan["changed"]:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(merge.dumps_json(plan["new_obj"]), encoding="utf-8")
        recorded_op = Op(
            op=op.op, ide=op.ide, dest=op.dest, strategy="json",
            created=created, tags=plan["tags"], preserve=op.preserve,
        )
    # Replace any prior merge op for this dest so the manifest holds exactly one.
    manifest.ops = [o for o in manifest.ops if not (o.op == OpType.MERGE.value and o.dest == op.dest)]
    manifest.ops.append(recorded_op)
    manifest.persist(paths.manifest)
    status = "applied" if plan["changed"] else "skipped"
    return OpOutcome(op=recorded_op, status=status, diverged=plan["skipped"])


def _inject_backup(op: Op, paths: CohortPaths, manifest: Manifest, dest: Path) -> Op:
    backup_dest = _backup_path(paths, manifest.install_id, dest)
    backup_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(dest), str(backup_dest))
    backup_op = Op(op=OpType.BACKUP.value, ide=op.ide, dest=str(dest), backup=str(backup_dest))
    manifest.ops.append(backup_op)
    manifest.persist(paths.manifest)
    return backup_op


def _place(op: Op, paths: CohortPaths, manifest: Manifest, recorded: dict[str, str]) -> OpOutcome:
    dest = Path(op.dest)
    if op.op == OpType.MKDIR.value:
        dest.mkdir()
        recorded_op = Op(op=op.op, ide=op.ide, dest=op.dest, created=True)
    elif op.op == OpType.LINK.value:
        if dest.is_symlink() or dest.exists():
            _remove_path(dest)  # re-point our own (possibly dangling/stale) link in place
        os.symlink(op.src, dest)
        recorded_op = Op(op=op.op, ide=op.ide, dest=op.dest, src=op.src)
    elif op.op == OpType.COPY.value:
        if dest.exists() or dest.is_symlink():
            _remove_path(dest)  # overwrite-ours
        src = Path(op.src or "")
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
        recorded_op = Op(op=op.op, ide=op.ide, dest=op.dest, src=op.src, tree_hash=path_hash(dest))
        recorded[op.dest] = recorded_op.tree_hash
    elif op.op == OpType.SCAFFOLD.value:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(op.src).read_bytes())
        recorded_op = Op(
            op=op.op, ide=op.ide, dest=op.dest, created=True, preserve=bool(op.preserve)
        )
    else:
        raise ValueError(f"unknown op type: {op.op!r}")
    manifest.ops.append(recorded_op)
    manifest.persist(paths.manifest)
    return OpOutcome(op=recorded_op, status="applied")


# --- Reverse ----------------------------------------------------------------


@dataclass
class ReverseResult:
    outcomes: list[OpOutcome] = field(default_factory=list)
    skipped: int = 0  # diverged/occupied dests left untouched (ownership check)

    @property
    def removed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "removed")

    @property
    def restored(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "restored")

    @property
    def dirs_removed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "dir_removed")


def _reverse_place_ops(ops: list[Op], result: ReverseResult, purge: bool = False) -> None:
    """Reverse non-mkdir ops LIFO, verifying Cohort ownership before removing.

    ``preserve: true`` ops (team-owned scaffolded content) are skipped unless
    ``purge`` — non-purge deinit never removes ``project_context.md`` etc.
    """
    for op in reversed(ops):
        if op.op == OpType.MKDIR.value:
            continue
        if op.preserve and not purge:
            result.skipped += 1
            continue
        dest = Path(op.dest)
        if op.op == OpType.SCAFFOLD.value:
            if dest.exists() and not dest.is_dir():
                dest.unlink()
                result.outcomes.append(OpOutcome(op=op, status="removed"))
            continue
        if op.op == OpType.LINK.value:
            # Remove our link if it still points at our target, or if it dangles
            # (the source moved) — a dangling link we recorded is ours to clean up,
            # never left to leak. A link re-pointed by the user to a live target is
            # treated as foreign and skipped.
            if _symlink_points_to(dest, op.src or "") or (dest.is_symlink() and not dest.exists()):
                dest.unlink()
                result.outcomes.append(OpOutcome(op=op, status="removed"))
            else:
                result.skipped += 1
        elif op.op == OpType.COPY.value:
            if (
                not dest.is_symlink()
                and dest.exists()
                and op.tree_hash is not None
                and path_hash(dest) == op.tree_hash
            ):
                _remove_path(dest)
                result.outcomes.append(OpOutcome(op=op, status="removed"))
            else:
                result.skipped += 1
        elif op.op == OpType.BACKUP.value:
            _restore_backup(op, dest, result)
        elif op.op == OpType.MERGE.value:
            _reverse_merge(op, dest, result)


def _reverse_merge(op: Op, dest: Path, result: ReverseResult) -> None:
    """Remove Cohort's block / tagged entries, verifying ownership first (B)."""
    if not dest.exists():
        result.skipped += 1
        return
    if op.strategy == "block":
        text = dest.read_text(encoding="utf-8")
        inner = merge.extract_block(text)
        if inner is None or merge.block_hash(inner) != op.block_hash:
            result.skipped += 1  # block gone or user edited inside it
            return
        new_text = merge.remove_block(text)
        if op.created and new_text.strip() == "":
            dest.unlink()
        else:
            dest.write_text(new_text, encoding="utf-8")
        result.outcomes.append(OpOutcome(op=op, status="removed"))
    else:  # json
        existing = json.loads(dest.read_text(encoding="utf-8"))
        new_obj, removed, skipped = merge.remove_tagged(existing, op.tags or [])
        result.skipped += skipped
        if removed:
            if op.created and not new_obj:
                dest.unlink()
            else:
                dest.write_text(merge.dumps_json(new_obj), encoding="utf-8")
            result.outcomes.append(OpOutcome(op=op, status="removed"))


def _restore_backup(op: Op, dest: Path, result: ReverseResult) -> None:
    if dest.exists() or dest.is_symlink():
        result.skipped += 1  # occupied → never overwrite (B)
        return
    backup = Path(op.backup or "")
    if backup.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(dest))
        result.outcomes.append(OpOutcome(op=op, status="restored"))
    else:
        result.skipped += 1


def _reverse_created_dirs(ops: list[Op], result: ReverseResult, purge: bool = False) -> None:
    """rmdir created dirs LIFO, only if empty (never remove a pre-existing dir)."""
    for op in reversed(ops):
        if op.op != OpType.MKDIR.value or not op.created:
            continue
        if op.preserve and not purge:
            continue  # team-owned dir (e.g. sessions/) — keep on non-purge deinit
        d = Path(op.dest)
        if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
            d.rmdir()
            result.outcomes.append(OpOutcome(op=op, status="dir_removed"))
        else:
            result.skipped += 1


def reverse_full(manifest: Manifest, paths: CohortPaths, purge: bool = False) -> ReverseResult:
    """Reverse an entire install and tear down the non-op artifacts (A).

    Order: LIFO-reverse link/copy/backup/merge/scaffold ops → delete backups/<id>/
    → delete the manifest → rmtree staging → rmdir-if-empty the created dirs and the
    home. ``preserve: true`` ops are skipped unless ``purge`` (P4 deinit): non-purge
    keeps team-owned content, so the home is not swept while content remains.
    """
    result = ReverseResult()
    _reverse_place_ops(manifest.ops, result, purge)

    backups_dir = paths.backups / manifest.install_id
    if backups_dir.exists():
        shutil.rmtree(backups_dir)
    if paths.backups.exists() and not any(paths.backups.iterdir()):
        paths.backups.rmdir()
    if paths.manifest.exists():
        paths.manifest.unlink()
    # Derived staging is a non-op artifact (written by compile, not a recorded
    # mkdir), so it is torn down here so bare uninstall leaves no ~/.cohort.
    if paths.compiled.exists():
        shutil.rmtree(paths.compiled)

    _reverse_created_dirs(manifest.ops, result, purge)
    # Safety sweep: compile pre-creates ~/.cohort (via the staging dir) before
    # install records its mkdir, so the home may not be a recorded created dir.
    # It is unambiguously Cohort's namespace — rmdir it (and state/) if now empty.
    _sweep_empty_dir(paths.state, result)
    _sweep_empty_dir(paths.cohort_home, result)
    return result


def _sweep_empty_dir(d: Path, result: ReverseResult) -> None:
    if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
        d.rmdir()
        result.outcomes.append(
            OpOutcome(op=Op(OpType.MKDIR.value, "global", str(d), created=True), status="dir_removed")
        )


def reverse_slice(manifest: Manifest, paths: CohortPaths, ide: str) -> ReverseResult:
    """Reverse only one IDE's ops; never touch the shared global home (S4).

    Updates the manifest (drops the IDE and its ops) and persists it.
    """
    result = ReverseResult()
    if ide not in manifest.ides:
        return result  # unrecorded IDE → no-op
    ide_ops = [o for o in manifest.ops if o.ide == ide]
    _reverse_place_ops(ide_ops, result)
    _reverse_created_dirs(ide_ops, result)
    manifest.ides.remove(ide)
    manifest.ops = [o for o in manifest.ops if o.ide != ide]
    manifest.persist(paths.manifest)
    return result

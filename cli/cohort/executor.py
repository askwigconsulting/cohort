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
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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


def classify(op: Op, recorded_copy_hashes: dict[str, str]) -> OpStatus:
    """Classify one op against current filesystem state (force-agnostic)."""
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
    raise ValueError(f"unknown op type: {op.op!r}")


def _recorded_copy_hashes(manifest: Optional[Manifest]) -> dict[str, str]:
    if manifest is None:
        return {}
    return {
        o.dest: o.tree_hash
        for o in manifest.ops
        if o.op == OpType.COPY.value and o.tree_hash is not None
    }


@dataclass
class Preflight:
    classified: list[ClassifiedOp]
    clobbers: list[ClassifiedOp] = field(default_factory=list)


def preflight(
    plan: list[Op], manifest: Optional[Manifest], force: bool
) -> Preflight:
    """Read-only classification of an entire plan. No filesystem mutation."""
    recorded = _recorded_copy_hashes(manifest)
    classified: list[ClassifiedOp] = []
    clobbers: list[ClassifiedOp] = []
    for op in plan:
        status = classify(op, recorded)
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
    outcomes: list[OpOutcome] = []
    for op in plan:
        status = classify(op, recorded)
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


def _reverse_place_ops(ops: list[Op], result: ReverseResult) -> None:
    """Reverse non-mkdir ops LIFO, verifying Cohort ownership before removing."""
    for op in reversed(ops):
        if op.op == OpType.MKDIR.value:
            continue
        dest = Path(op.dest)
        if op.op == OpType.LINK.value:
            if _symlink_points_to(dest, op.src or ""):
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


def _reverse_created_dirs(ops: list[Op], result: ReverseResult) -> None:
    """rmdir created dirs LIFO, only if empty (never remove a pre-existing dir)."""
    for op in reversed(ops):
        if op.op != OpType.MKDIR.value or not op.created:
            continue
        d = Path(op.dest)
        if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
            d.rmdir()
            result.outcomes.append(OpOutcome(op=op, status="dir_removed"))
        else:
            result.skipped += 1


def reverse_full(manifest: Manifest, paths: CohortPaths) -> ReverseResult:
    """Reverse an entire install and tear down the non-op artifacts (A).

    Order: LIFO-reverse link/copy/backup ops → delete backups/<id>/ → delete the
    manifest file → rmdir-if-empty the recorded global mkdir dirs (state/, home).
    """
    result = ReverseResult()
    _reverse_place_ops(manifest.ops, result)

    backups_dir = paths.backups / manifest.install_id
    if backups_dir.exists():
        shutil.rmtree(backups_dir)
    if paths.backups.exists() and not any(paths.backups.iterdir()):
        paths.backups.rmdir()
    if paths.manifest.exists():
        paths.manifest.unlink()

    _reverse_created_dirs(manifest.ops, result)
    return result


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

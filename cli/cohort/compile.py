"""The compile pipeline: canonical → IR → staged Claude files → ops.

Renderers are pure and byte-stable; staging is a derived mirror of each IDE's
native layout under ``~/.cohort/compiled/<ide>/``. The installer then links (or
copies) staging → the IDE dests, so a ``git pull`` + ``recompile`` propagates
through the symlinks. ``install`` places whatever is staged; ``recompile`` =
compile-then-install.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.claude import MERGE_SUBDIR, ClaudeRenderer, MarkerError, StagedFile
from .adapters.codex import CodexRenderer
from .adapters.cursor import CursorRenderer
from .executor import path_hash
from .install_model import CohortPaths, Op, OpType
from .ir import build_ir
from .loader import load_artifact
from .schema import discover_artifacts, validate_frontmatter

# Renderers by IDE — each is a descriptor the pipeline drives off (P7-R1).
RENDERERS: dict = {
    "claude": ClaudeRenderer(),
    "codex": CodexRenderer(),
    "cursor": CursorRenderer(),
}


class CompileError(Exception):
    """Raised when a canonical artifact fails to load or validate during compile."""


@dataclass
class CompileResult:
    ide: str
    staged: list[StagedFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # names skipped (wrong target / deferred kind)

    def to_dict(self) -> dict:
        return {
            "action": "compile",
            "ide": self.ide,
            "staged": [s.staged_rel for s in self.staged],
            "skipped": self.skipped,
        }


def _load_irs(source: Path, scope: Optional[str] = None):
    irs = []
    for p in discover_artifacts(source / "canonical"):
        result = load_artifact(p)
        if result.load_error is not None:
            raise CompileError(f"{p}: {result.load_error.message}")
        errors = validate_frontmatter(result.frontmatter, p.stem)
        if errors:
            raise CompileError(f"{p}: {errors[0].code} {errors[0].message}")
        ir = build_ir(result.frontmatter, result.body, p)
        # Tier partition: a tier only ever compiles its own scope. This is the leak
        # guard — a scope:project artifact in the global canonical can never reach the
        # global office, and vice versa.
        if scope is not None and ir.scope != scope:
            continue
        irs.append(ir)
    return irs


def compile_ide(source: Path, ide: str, scope: Optional[str] = None) -> CompileResult:
    """Render every targeting canonical artifact of ``scope`` into staged files for
    ``ide``. The global install passes ``scope="global"`` (the leak guard — project
    artifacts never reach the global office); a project-tier compile passes
    ``"project"``; ``None`` (default) compiles all scopes, for direct/test use.

    Generic over the renderer descriptor (P7-R1): ``renderer.compile(irs)`` owns
    the IDE-specific 1:1 + aggregate staging; this function just loads/validates
    the IR and wraps render errors.
    """
    result = CompileResult(ide=ide)
    renderer = RENDERERS.get(ide)
    if renderer is None:
        return result  # no renderer for this IDE
    try:
        staged, skipped = renderer.compile(_load_irs(source, scope))
    except MarkerError as exc:
        raise CompileError(str(exc)) from exc
    result.staged = staged
    result.skipped = skipped
    return result


def write_staging(paths: CohortPaths, result: CompileResult) -> None:
    """Write a compile result to ``compiled/<ide>/``, replacing prior staging.

    Staging is derived and disposable, so the IDE subtree is rebuilt wholesale —
    a canonical artifact removed since last compile leaves no stale staged file.
    """
    staging_root = paths.compiled_ide(result.ide)
    if staging_root.exists():
        shutil.rmtree(staging_root)
    for sf in result.staged:
        dest = staging_root / sf.staged_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(sf.content)


def staging_tree_hash(paths: CohortPaths, ide: str) -> str:
    """Hash of an IDE's staging tree (for byte-stability assertions)."""
    root = paths.compiled_ide(ide)
    return path_hash(root) if root.exists() else ""


# --- staging → install ops --------------------------------------------------


def scan_staging_ops(paths: CohortPaths, ide: str, mode: str) -> list[Op]:
    """Emit Phase-1 ops to place an IDE's staged files at their dests.

    Staging mirrors the IDE's native layout, so a file at
    ``compiled/<ide>/<rel>`` maps to ``~/.<ide>/<rel>`` (here ``~/.claude/<rel>``).
    Files under the ``.merge`` subdir are payloads consumed by merge ops (T3),
    not mirrored. mkdir ops cover the dest dirs (user-owned, usually satisfied).
    """
    renderer = RENDERERS.get(ide)
    staging = paths.compiled_ide(ide)
    if renderer is None or not staging.exists():
        return []
    dest_root = renderer.dest_root(paths.base)
    files = [
        p
        for p in sorted(staging.rglob("*"))
        if p.is_file() and MERGE_SUBDIR not in p.relative_to(staging).parts
    ]
    dirs: set[Path] = {dest_root}
    file_ops: list[Op] = []
    op_type = OpType.COPY.value if mode == "copy" else OpType.LINK.value
    for f in files:
        dest = dest_root / f.relative_to(staging)
        d = dest.parent
        while True:
            dirs.add(d)
            if d == dest_root:
                break
            d = d.parent
        file_ops.append(Op(op_type, ide, str(dest), src=str(f)))
    mkdir_ops = [
        Op(OpType.MKDIR.value, ide, str(d)) for d in sorted(dirs, key=lambda p: len(p.parts))
    ]
    return mkdir_ops + file_ops + _merge_ops(renderer, staging, dest_root, ide)


def _merge_ops(renderer, staging: Path, dest_root: Path, ide: str) -> list[Op]:
    """Merge ops for the renderer's declared merge targets whose payload was staged."""
    ops: list[Op] = []
    for mt in getattr(renderer, "merge_targets", ()):
        payload = staging / mt.payload_rel
        if payload.exists():
            ops.append(
                Op(
                    OpType.MERGE.value,
                    ide,
                    str(dest_root / mt.dest_name),
                    src=str(payload),
                    strategy=mt.strategy,
                )
            )
    return ops

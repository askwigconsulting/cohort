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
from typing import Optional

import json

from .adapters import claude as claude_adapter
from .adapters.claude import (
    CLAUDE_MERGE_MAP,
    CORPUS_REL,
    HOOKS_FRAGMENT_REL,
    IMPORT_BLOCK_REL,
    IMPORT_LINE,
    MERGE_SUBDIR,
    ClaudeRenderer,
    StagedFile,
)
from .executor import path_hash
from .install_model import CohortPaths, Op, OpType
from .ir import build_ir
from .loader import load_artifact
from .schema import discover_artifacts, validate_frontmatter

# Renderers by IDE. Codex/Cursor land in Phase 7.
RENDERERS = {"claude": ClaudeRenderer()}


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


def _load_irs(source: Path):
    irs = []
    for p in discover_artifacts(source / "canonical"):
        result = load_artifact(p)
        if result.load_error is not None:
            raise CompileError(f"{p}: {result.load_error.message}")
        errors = validate_frontmatter(result.frontmatter, p.stem)
        if errors:
            raise CompileError(f"{p}: {errors[0].code} {errors[0].message}")
        irs.append(build_ir(result.frontmatter, result.body, p))
    return irs


def compile_ide(source: Path, ide: str) -> CompileResult:
    """Render every targeting canonical artifact into staged files for ``ide``."""
    renderer = RENDERERS.get(ide)
    result = CompileResult(ide=ide)
    if renderer is None:
        return result  # no renderer yet (codex/cursor → Phase 7)
    hook_irs = []
    memory_irs = []
    for ir in _load_irs(source):
        if not renderer.matches(ir):
            result.skipped.append(ir.name)
            continue
        if ir.kind == "hook":
            hook_irs.append(ir)
        elif ir.kind == "memory":
            memory_irs.append(ir)
        elif ir.kind == "context":
            result.skipped.append(ir.name)  # deferred to Phase 4
        else:
            staged = renderer.render_one_to_one(ir)
            if staged is not None:
                result.staged.append(staged)
    if ide == "claude":
        _stage_claude_aggregates(result, hook_irs, memory_irs)
    return result


def _stage_claude_aggregates(result, hook_irs, memory_irs) -> None:
    """Stage the corpus (1:1) + the merge payloads (.merge) for aggregating kinds."""
    if memory_irs:
        corpus = claude_adapter.render_memory_corpus(memory_irs)
        result.staged.append(StagedFile(CORPUS_REL, corpus.encode("utf-8")))
        result.staged.append(
            StagedFile(IMPORT_BLOCK_REL, (IMPORT_LINE + "\n").encode("utf-8"))
        )
    if hook_irs:
        fragment = claude_adapter.render_hooks_fragment(hook_irs)
        payload = json.dumps(fragment, indent=2) + "\n"
        result.staged.append(StagedFile(HOOKS_FRAGMENT_REL, payload.encode("utf-8")))


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
    staging = paths.compiled_ide(ide)
    if not staging.exists():
        return []
    dest_root = paths.home / f".{ide}"
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
    merge_ops = _claude_merge_ops(staging, dest_root, ide) if ide == "claude" else []
    return mkdir_ops + file_ops + merge_ops


def _claude_merge_ops(staging: Path, dest_root: Path, ide: str) -> list[Op]:
    """Merge ops for staged .merge payloads (hook→settings.json, memory→CLAUDE.md)."""
    ops: list[Op] = []
    for rel, dest_name, strategy in CLAUDE_MERGE_MAP:
        payload = staging / rel
        if payload.exists():
            ops.append(
                Op(
                    OpType.MERGE.value,
                    ide,
                    str(dest_root / dest_name),
                    src=str(payload),
                    strategy=strategy,
                )
            )
    return ops

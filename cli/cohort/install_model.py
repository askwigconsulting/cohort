"""Core data types for the Phase 1 install engine.

The install engine is a *producer/executor* seam: adapters (Phase 2/7) produce
``mkdir``/``link``/``copy`` ops; the executor (this phase) applies, dry-runs, or
reverses any plan idempotently and reversibly. ``backup`` is never authored by a
producer — the executor injects it when ``--force`` displaces a foreign file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# The IDEs Cohort can install. "all" is sugar for the three.
IDES = ("claude", "codex", "cursor")
IDE_VALUES = (*IDES, "all")
GLOBAL_IDE = "global"


class OpType(str, Enum):
    MKDIR = "mkdir"
    LINK = "link"
    COPY = "copy"
    BACKUP = "backup"
    MERGE = "merge"


class OpStatus(str, Enum):
    """Preflight classification of an op against current filesystem state."""

    SATISFIED = "satisfied"  # already in the desired state → skip
    APPLY = "apply"  # must be performed (create / place / overwrite-ours)
    CLOBBER = "clobber"  # a foreign file/dir sits at dest → refuse unless --force


@dataclass
class Op:
    """One filesystem operation in an InstallPlan or recorded in the manifest.

    ``created`` (mkdir): True only when the executor actually created the dir, so
    reverse rmdirs only what it made. ``tree_hash`` (copy): hash of what we wrote,
    used so reverse removes a copied tree only if still byte-identical (ownership
    check). ``backup`` (executor-injected backup): where the displaced file went.
    """

    op: str
    ide: str
    dest: str
    src: Optional[str] = None
    backup: Optional[str] = None
    created: Optional[bool] = None
    tree_hash: Optional[str] = None
    # merge-op fidelity fields (P2-T3): how reverse verifies Cohort ownership.
    strategy: Optional[str] = None  # "block" | "json"
    block_hash: Optional[str] = None  # managed-block: hash of the block we wrote
    tags: Optional[list] = None  # key-merge: [{event, entry_hash}] Cohort added

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op, "ide": self.ide, "dest": self.dest}
        for key in ("src", "backup", "created", "tree_hash", "strategy", "block_hash", "tags"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Op":
        return cls(
            op=data["op"],
            ide=data.get("ide", GLOBAL_IDE),
            dest=data["dest"],
            src=data.get("src"),
            backup=data.get("backup"),
            created=data.get("created"),
            tree_hash=data.get("tree_hash"),
            strategy=data.get("strategy"),
            block_hash=data.get("block_hash"),
            tags=data.get("tags"),
        )


@dataclass
class ClassifiedOp:
    """An op paired with its preflight classification."""

    op: Op
    status: OpStatus
    reason: str = ""


@dataclass
class OpOutcome:
    """The result of acting on one op during apply or reverse."""

    op: Op
    status: str  # applied | skipped | backup | removed | restored | dir_removed


@dataclass
class CohortPaths:
    """Resolves the well-known Cohort paths under an injected home directory.

    The home is injectable so the whole engine is testable against a temp ``$HOME``.
    """

    home: Path

    @property
    def cohort_home(self) -> Path:
        return self.home / ".cohort"

    @property
    def state(self) -> Path:
        return self.cohort_home / "state"

    @property
    def manifest(self) -> Path:
        return self.state / "manifest.json"

    @property
    def backups(self) -> Path:
        return self.state / "backups"

    @property
    def canonical(self) -> Path:
        return self.cohort_home / "canonical"

    @property
    def compiled(self) -> Path:
        return self.cohort_home / "compiled"

    def compiled_ide(self, ide: str) -> Path:
        """Staging root for one IDE's rendered artifacts."""
        return self.compiled / ide

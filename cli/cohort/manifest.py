"""The install manifest — the source of truth a reverse replays.

Persisted incrementally and fsync'd after every applied op (P1-T1 C), so a
crashed or partially-applied install is still fully reversible.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .filelock import file_lock
from .install_model import Op


def new_install_id() -> str:
    """Generate a fresh install id (patchable in tests for determinism)."""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    """Current UTC timestamp in ISO-8601 (patchable in tests)."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Manifest:
    """Records an install: identity, mode, selected IDEs, and applied ops.

    ``mode`` is informational (the most-recent install's mode); per-op ``op``
    type governs reversal, never ``mode`` (decision S2).
    """

    install_id: str
    created_at: str
    mode: str
    ides: list[str] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    roster: Optional[list[str]] = None  # tailored agent subset; None = full roster

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "install_id": self.install_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "ides": list(self.ides),
            "ops": [op.to_dict() for op in self.ops],
        }
        if self.roster is not None:
            out["roster"] = list(self.roster)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        roster = data.get("roster")
        return cls(
            install_id=data["install_id"],
            created_at=data["created_at"],
            mode=data.get("mode", "link"),
            ides=list(data.get("ides", [])),
            ops=[Op.from_dict(o) for o in data.get("ops", [])],
            roster=list(roster) if roster is not None else None,
        )

    def persist(self, path: Path) -> None:
        """Atomically write the manifest and fsync it (and its directory).

        Skips silently if the parent ``state/`` dir does not exist yet — during
        apply the first mkdir ops create it, after which every op flushes.
        """
        if not path.parent.exists():
            return
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(self.to_dict(), indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # Directory fsync is a POSIX durability step; it's a no-op concept on
        # Windows (os.open on a directory fails there), so skip it on nt.
        if os.name != "nt":
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)


def load_manifest(path: Path) -> Optional[Manifest]:
    """Load a manifest from ``path``, or None if it does not exist."""
    if not path.exists():
        return None
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


@contextmanager
def manifest_lock(manifest_path: Path) -> Iterator[None]:
    """Serialize a ``load_manifest`` → mutate → :meth:`Manifest.persist` cycle on
    the manifest at ``manifest_path`` across concurrent ``cohort`` processes.

    ``persist`` writes atomically (tmp + ``os.replace``), so a concurrent write
    can never *corrupt* the file — but two processes that each load, mutate, and
    persist can still lose one update (last writer wins). This lock closes that
    window. It wraps the *whole* cycle, so ``persist`` itself must not also
    acquire it (the lock is not reentrant); callers hold this around the read and
    the write together.

    The manifest's parent (``state/``) must already exist, so this guards
    *post-install* read-modify-writes (recompile, context refresh) — where
    concurrent writers actually meet. The initial ``cohort init`` creates
    ``state/`` mid-apply and is a single bootstrap step, so it is not wrapped
    here; the registry write it performs is serialized by its own lock.
    """
    with file_lock(manifest_path):
        yield

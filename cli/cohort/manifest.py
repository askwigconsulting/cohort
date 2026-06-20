"""The install manifest — the source of truth a reverse replays.

Persisted incrementally and fsync'd after every applied op (P1-T1 C), so a
crashed or partially-applied install is still fully reversible.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "install_id": self.install_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "ides": list(self.ides),
            "ops": [op.to_dict() for op in self.ops],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        return cls(
            install_id=data["install_id"],
            created_at=data["created_at"],
            mode=data.get("mode", "link"),
            ides=list(data.get("ides", [])),
            ops=[Op.from_dict(o) for o in data.get("ops", [])],
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

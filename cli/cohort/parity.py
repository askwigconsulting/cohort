"""Parity check (P7-T3) — coverage, not byte-diff.

Parity is defined at the IR level (robust to structurally different IDEs): for
each canonical kind with artifacts targeting an IDE, that kind must either be
**rendered** by the IDE's renderer or be a **declared gap** in
``adapters/<ide>/parity-gaps.toml``. The check fails on an **undeclared** gap
(accidental loss) and on a **stale** declaration (a gapped kind the renderer now
renders). Claude is the reference coverage set, never a byte comparator.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .ir import build_ir
from .loader import load_artifact
from .schema import discover_artifacts, validate_frontmatter

# kinds that have no IDE target by design (project-scope context layer, Phase 4)
_NON_IDE_KINDS = frozenset({"context"})


def adapters_dir() -> Path:
    """Where per-IDE parity-gaps.toml live (source tree, git-tracked, reviewed)."""
    import os

    override = os.environ.get("COHORT_ADAPTERS_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[2] / "adapters"


def load_gaps(ide: str) -> dict[str, str]:
    """Declared {kind: reason} gaps for an IDE (empty if no file)."""
    path = adapters_dir() / ide / "parity-gaps.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {g["kind"]: g.get("reason", "") for g in data.get("gaps", [])}


@dataclass
class ParityResult:
    ide: str
    covered: set = field(default_factory=set)
    declared_gaps: dict = field(default_factory=dict)
    undeclared: set = field(default_factory=set)  # kinds present but neither rendered nor declared
    stale: set = field(default_factory=set)  # declared gaps the renderer actually renders

    @property
    def ok(self) -> bool:
        return not self.undeclared and not self.stale

    def to_dict(self) -> dict:
        return {
            "ide": self.ide,
            "ok": self.ok,
            "covered": sorted(self.covered),
            "declared_gaps": self.declared_gaps,
            "undeclared": sorted(self.undeclared),
            "stale": sorted(self.stale),
        }


def check_parity(source: Path, ide: str, renderers: dict) -> ParityResult:
    """Coverage parity for one IDE against the canonical IR set."""
    renderer = renderers[ide]
    supported = set(renderer.supported_kinds)
    gaps = load_gaps(ide)

    kinds_present: set[str] = set()
    for p in discover_artifacts(source / "canonical"):
        loaded = load_artifact(p)
        if loaded.load_error is not None:
            continue
        if validate_frontmatter(loaded.frontmatter, p.stem):
            continue
        ir = build_ir(loaded.frontmatter, loaded.body, p)
        if ir.targets_ide(ide) and ir.kind not in _NON_IDE_KINDS:
            kinds_present.add(ir.kind)

    covered = {k for k in kinds_present if k in supported}
    undeclared = {k for k in kinds_present if k not in supported and k not in gaps}
    stale = {k for k in gaps if k in supported}  # declared gap that actually renders now
    return ParityResult(
        ide=ide, covered=covered, declared_gaps=gaps, undeclared=undeclared, stale=stale
    )

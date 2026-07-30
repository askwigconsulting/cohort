"""Parity check (P7-T3) — coverage, not byte-diff.

Parity is defined at the IR level (robust to structurally different IDEs): for
each canonical kind with artifacts targeting an IDE, that kind must either be
**rendered** by the IDE's renderer or be a **declared gap** in
``adapters/<ide>/parity-gaps.toml``. The check fails on an **undeclared** gap
(accidental loss) and on a **stale** declaration (a gapped kind the renderer now
renders). Claude is the reference coverage set, never a byte comparator.
"""

from __future__ import annotations

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


def _parse_gaps_toml_minimal(text: str) -> list[dict[str, str]]:
    """A minimal ``[[gaps]]`` array-of-tables reader for Python 3.10 (no
    ``tomllib``). Understands exactly the repeated ``[[gaps]]`` block +
    quoted ``key = "value"`` shape ``parity-gaps.toml`` files use. Never a
    general TOML parser — the caller treats any surprise as fail-safe (no
    declared gaps), and this is deliberately a *different* minimal parser
    from ``project.py``'s (that one only handles flat ``[table]`` sections,
    not repeated ``[[array]]`` tables, and would silently misparse this
    file's shape)."""
    gaps: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "[[gaps]]":
            current = {}
            gaps.append(current)
            continue
        if current is None:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        current[key.strip()] = value
    return gaps


def load_gaps(ide: str) -> dict[str, str]:
    """Declared {kind: reason} gaps for an IDE (empty if no file). Uses
    ``tomllib`` where available (3.11+); on 3.10 a minimal fallback parser
    reads the ``[[gaps]]`` array-of-tables shape these files use."""
    path = adapters_dir() / ide / "parity-gaps.toml"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            gaps = _parse_gaps_toml_minimal(text)
        except Exception:  # noqa: BLE001 - malformed file, fail safe to no gaps
            gaps = []
        return {g["kind"]: g.get("reason", "") for g in gaps if "kind" in g}
    data = tomllib.loads(text)
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

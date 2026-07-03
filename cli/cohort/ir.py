"""The intermediate representation (IR) renderers consume.

Canonical artifacts (Phase 0) normalize into a kind-typed, IDE-agnostic IR that
every adapter's renderer reads. Keeping the IR stable is what lets the Phase 7
Codex/Cursor renderers reuse the Claude pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .schema import apply_defaults

# Shared frontmatter keys; everything else is kind-specific and lands in `fields`.
_SHARED_KEYS = frozenset(
    {"name", "kind", "scope", "description", "targets", "version", "owner", "display_name"}
)


@dataclass
class IRArtifact:
    """A normalized canonical artifact: shared fields hoisted, defaults applied."""

    kind: str
    name: str
    scope: str
    targets: list[str]
    description: str
    version: str
    body: str
    display_name: Optional[str]
    owner: Optional[str]
    fields: dict[str, Any] = field(default_factory=dict)
    source_path: Optional[Path] = None
    # Provenance within the global scope: "office" (the shared source clone) or
    # "my" (the machine-local ~/.cohort/my overlay). Never affects rendered
    # bytes — display/diagnostics only (proven by the byte-golden tests).
    layer: str = "office"

    def targets_ide(self, ide: str) -> bool:
        """Whether this artifact should compile for the given IDE."""
        return "all" in self.targets or ide in self.targets


def build_ir(frontmatter: dict[str, Any], body: str, source_path: Path | str | None = None) -> IRArtifact:
    """Normalize a validated frontmatter mapping + body into an ``IRArtifact``."""
    fm = apply_defaults(frontmatter)
    kind_fields = {k: v for k, v in fm.items() if k not in _SHARED_KEYS}
    return IRArtifact(
        kind=fm["kind"],
        name=fm["name"],
        scope=fm["scope"],
        targets=list(fm["targets"]),
        description=fm["description"],
        version=fm["version"],
        body=body,
        display_name=fm.get("display_name"),
        owner=fm.get("owner"),
        fields=kind_fields,
        source_path=Path(source_path) if source_path is not None else None,
    )

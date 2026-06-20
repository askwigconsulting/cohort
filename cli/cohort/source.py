"""Resolve the Cohort source root (the cloned repo containing ``canonical/``).

Resolution order (decision M5): ``--source`` → ``COHORT_SOURCE`` env →
inferred by walking up from the installed package. Inference fails closed for a
non-editable site-packages install (no ``canonical/`` above it), so bootstrap
passes ``--source`` explicitly rather than relying on it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class SourceUnresolved(Exception):
    """Raised when no valid source root can be resolved."""


def _is_source_root(path: Path) -> bool:
    return (path / "canonical").is_dir()


def _infer_from_package() -> Optional[Path]:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if _is_source_root(parent):
            return parent
    return None


def resolve_source(explicit: Optional[str] = None, env: Optional[dict] = None) -> Path:
    """Resolve and validate the source root, or raise ``SourceUnresolved``."""
    environ = os.environ if env is None else env
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        if not _is_source_root(path):
            raise SourceUnresolved(f"--source has no canonical/ dir: {path}")
        return path
    env_value = environ.get("COHORT_SOURCE")
    if env_value:
        path = Path(env_value).expanduser().resolve()
        if not _is_source_root(path):
            raise SourceUnresolved(f"COHORT_SOURCE has no canonical/ dir: {path}")
        return path
    inferred = _infer_from_package()
    if inferred is not None:
        return inferred
    raise SourceUnresolved(
        "could not resolve a source root; pass --source or set COHORT_SOURCE"
    )

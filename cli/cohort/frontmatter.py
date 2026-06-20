"""Safe frontmatter emission + a YAML-safety lint (P9 [R-audit]).

The Phase-8 bug (a generated value with ``:``/``[`` breaking frontmatter) is
systemic: any writer putting a string into frontmatter is exposed. The fix is a
single ``dump_frontmatter`` every metadata writer routes through — string
metadata (``author``, ``agent``) stays in frontmatter, emitted safely, rather
than exiled to the body.

Quoting is **safe by construction**: emission goes through PyYAML's serializer
(``yaml.safe_dump``), whose job is to produce valid YAML for any input — it
quotes only when needed (so readable values stay plain and existing output is
stable) and escapes completely. We do not hand-roll the "does this need quotes?"
decision or the escaping, which could have a gap the round-trip tests miss.
``check_frontmatter_safety`` is the regression guard (CI lint).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .loader import load_artifact

_FM_DIRS = ("sessions", "feedback", "proposals", "reports")


def dump_frontmatter(pairs: list[tuple[str, Any]]) -> str:
    """Render a YAML frontmatter block (``---``…``---``); the serializer decides
    quoting/escaping, so an unsafe value cannot be emitted. Key order is the
    given order; long values are never line-wrapped."""
    body = yaml.safe_dump(
        dict(pairs),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=2**31,
    )
    return f"---\n{body}---\n"


def check_frontmatter_safety(repo: Path) -> list[str]:
    """Return paths under ``<repo>/.cohort/{sessions,feedback,proposals,reports}``
    whose frontmatter fails to parse — the YAML-safety lint. A file with no
    frontmatter (e.g. a report) is skipped; a delimited block that won't parse is
    a failure (the Phase-8 class)."""
    bad: list[str] = []
    base = repo / ".cohort"
    for sub in _FM_DIRS:
        d = base / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            if not text.lstrip("﻿").startswith("---"):
                continue  # no frontmatter (markdown-only, e.g. reports)
            if load_artifact(f).load_error is not None:
                bad.append(str(f))
    return bad

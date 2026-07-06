"""Codex/Cursor/Claude golden-lock — activates the deferred Phase-7 sub-gate.

Until now CI ran only coverage/structure/byte-stability for Codex/Cursor; the
concrete bytes were intentionally not locked. This locks the full staged tree of
every non-trivial IDE renderer against its current output, so any drift in
Codex/Cursor/Claude rendering is caught as a byte diff.

NOTE: the Codex/Cursor bytes are **doc-cited**, not validated against a live
install — the field-level items (hook-event names, Cursor frontmatter/skills
dir) remain doc-cited. The lock is a regression guard on current output; when a
real install confirms the mappings, the renderer and these goldens move together.

Regenerate after an intentional renderer change (same COHORT_REGEN convention as
the report goldens in test_phase5):

    COHORT_REGEN=1 python -m pytest tests/test_golden_lock.py -q
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]

# Every renderer whose full staged tree is byte-locked. Claude joins Codex/Cursor
# so its commands/skills/merge surfaces are guarded too — previously only Claude's
# agents/ were locked (test_roster_compile), leaving commands like update.md
# unguarded while Cursor's equivalent was locked.
LOCKED_IDES = ["codex", "cursor", "claude"]


def _regen(golden_base: Path, produced: dict[str, bytes]) -> None:
    """Rewrite the golden tree to match current renderer output (COHORT_REGEN=1)."""
    if golden_base.exists():
        shutil.rmtree(golden_base)
    for rel, content in produced.items():
        target = golden_base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@pytest.mark.parametrize("ide", LOCKED_IDES)
def test_ide_goldens_are_byte_locked(ide):
    golden_base = REPO / "tests" / "golden" / "roster" / ide
    produced = {sf.staged_rel: sf.content for sf in compile_ide(REPO, ide).staged}

    if os.environ.get("COHORT_REGEN"):
        _regen(golden_base, produced)

    assert golden_base.exists(), f"no locked goldens for {ide} (run COHORT_REGEN=1)"
    locked = {
        p.relative_to(golden_base).as_posix(): p.read_bytes()  # posix sep to match staged_rel
        for p in golden_base.rglob("*")
        if p.is_file()
    }
    assert set(produced) == set(locked), {  # same file set (no missing/extra)
        "missing_from_golden": sorted(set(produced) - set(locked)),
        "stale_golden": sorted(set(locked) - set(produced)),
    }
    for rel in sorted(produced):
        assert produced[rel] == locked[rel], f"{ide}: {rel} drifted from its golden"

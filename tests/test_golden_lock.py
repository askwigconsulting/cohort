"""Codex/Cursor golden-lock — activates the deferred Phase-7 sub-gate.

Until now CI ran only coverage/structure/byte-stability for Codex/Cursor; the
concrete bytes were intentionally not locked. This locks them against the
renderers' current output, so any drift in Codex/Cursor rendering is caught.

NOTE: these bytes are **doc-cited**, not validated against a live Codex/Cursor
install — the field-level items (hook-event names, Cursor frontmatter/skills
dir) remain doc-cited. The lock is a regression guard on current output; when a
real install confirms the mappings, the renderer and these goldens move together.
Regenerate after an intentional renderer change:

    python -c "from pathlib import Path; from cohort.compile import compile_ide; \
[ ( (Path('tests/golden/roster')/ide/sf.staged_rel).parent.mkdir(parents=True, exist_ok=True), \
(Path('tests/golden/roster')/ide/sf.staged_rel).write_bytes(sf.content) ) \
for ide in ('codex','cursor') for sf in compile_ide(Path('.'), ide).staged ]"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("ide", ["codex", "cursor"])
def test_codex_cursor_goldens_are_byte_locked(ide):
    golden_base = REPO / "tests" / "golden" / "roster" / ide
    assert golden_base.exists(), f"no locked goldens for {ide}"
    produced = {sf.staged_rel: sf.content for sf in compile_ide(REPO, ide).staged}
    locked = {
        str(p.relative_to(golden_base)): p.read_bytes()
        for p in golden_base.rglob("*")
        if p.is_file()
    }
    assert set(produced) == set(locked), {  # same file set (no missing/extra)
        "missing_from_golden": sorted(set(produced) - set(locked)),
        "stale_golden": sorted(set(locked) - set(produced)),
    }
    for rel in sorted(produced):
        assert produced[rel] == locked[rel], f"{ide}: {rel} drifted from its golden"

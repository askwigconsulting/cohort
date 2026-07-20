"""GS2: `recompile_global_claude` generalizes to `recompile_global_ides`, which
must target exactly the requested IDE set — never a hardcoded ``["claude"]`` —
while leaving the historical single-Claude behavior byte-identical (the common
case every add-agent/add-skill/add-command/add-hook/edit/personalize path shares).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort.install import do_install
from cohort.install_model import CohortPaths
from cohort.roster import recompile_global_claude, recompile_global_ides

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_recompile_global_claude_still_installs_exactly_the_claude_tier(home):
    do_install(home=home, selection=["claude"], mode="copy", force=False,
               source=REPO_ROOT, dry_run=False)

    report = recompile_global_claude(home, REPO_ROOT)

    assert report.ides == ["claude"]
    assert (home / ".claude" / "agents" / "chief-of-staff.md").exists()
    assert not (home / ".codex").exists()
    assert not (home / ".cursor").exists()


def test_recompile_global_claude_matches_recompile_global_ides_claude_default(tmp_path):
    """`recompile_global_claude` is the ["claude"]-selection case of
    `recompile_global_ides` — same call, same result shape, byte-identical
    outcome for the common case. Compared across three IDENTICAL fresh installs
    (not sequential calls on one home, where a re-apply would report "skipped"
    instead of "applied" and make the summaries differ for a reason that has
    nothing to do with the refactor)."""
    homes = []
    for label in ("claude", "general-default", "general-explicit"):
        h = tmp_path / label
        h.mkdir()
        do_install(home=h, selection=["claude"], mode="copy", force=False,
                   source=REPO_ROOT, dry_run=False)
        homes.append(h)
    home_claude, home_default, home_explicit = homes

    via_claude = recompile_global_claude(home_claude, REPO_ROOT)
    via_general_default = recompile_global_ides(home_default, REPO_ROOT)  # ides=None -> ["claude"]
    via_general_explicit = recompile_global_ides(home_explicit, REPO_ROOT, ["claude"])

    for report in (via_claude, via_general_default, via_general_explicit):
        assert report.ides == ["claude"]
        assert report.mode == via_claude.mode
        assert report.summary == via_claude.summary


def test_recompile_global_ides_recompiles_the_full_requested_set(home):
    do_install(home=home, selection=["claude", "cursor"], mode="copy", force=False,
               source=REPO_ROOT, dry_run=False)

    report = recompile_global_ides(home, REPO_ROOT, ["claude", "cursor"])

    assert report.ides == ["claude", "cursor"]
    assert (home / ".claude" / "agents" / "chief-of-staff.md").exists()
    assert (home / ".cursor" / "agents" / "chief-of-staff.md").exists()
    assert not (home / ".codex").exists()  # never requested, never placed


def test_recompile_global_ides_codex_only_never_touches_claude(home):
    do_install(home=home, selection=["codex"], mode="copy", force=False,
               source=REPO_ROOT, dry_run=False)

    report = recompile_global_ides(home, REPO_ROOT, ["codex"])

    assert report.ides == ["codex"]
    assert (home / ".codex" / "agents" / "chief-of-staff.toml").exists()
    assert not (home / ".claude").exists()


def test_recompile_global_ides_honors_the_persisted_roster_subset(home):
    """The tailored-roster invariant `recompile_global_claude` already upheld
    (only_agents from the manifest) must carry over to the multi-IDE path."""
    do_install(home=home, selection=["claude"], mode="copy",
               force=False, source=REPO_ROOT, dry_run=False)
    paths = CohortPaths.for_global(home)
    from cohort.manifest import load_manifest

    manifest = load_manifest(paths.manifest)
    manifest.roster = ["chief-of-staff", "counsel"]
    manifest.persist(paths.manifest)

    recompile_global_ides(home, REPO_ROOT, ["claude"])

    placed = sorted(p.stem for p in (home / ".claude" / "agents").glob("*.md"))
    assert placed == ["chief-of-staff", "counsel"]

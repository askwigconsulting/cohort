"""The supervision dial (cohort.autonomy): machine-local level, fail-closed, ceiling."""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort import autonomy
from cohort.install_model import CohortPaths


def test_absent_level_is_the_recommended_default(tmp_path):
    assert autonomy.read_autonomy_level(tmp_path) == "guided"


def test_set_then_read_roundtrips(tmp_path):
    autonomy.set_autonomy_level(tmp_path, "autopilot")
    assert autonomy.read_autonomy_level(tmp_path) == "autopilot"


def test_malformed_value_fails_closed_to_safest(tmp_path):
    # A hand-edited/corrupt level must never resolve to more autonomy than intended.
    (CohortPaths.for_global(tmp_path).state).mkdir(parents=True, exist_ok=True)
    (CohortPaths.for_global(tmp_path).state / "autonomy").write_text("YOLO\n")
    assert autonomy.read_autonomy_level(tmp_path) == "paired"


def test_set_rejects_an_unknown_level(tmp_path):
    with pytest.raises(ValueError, match="unknown autonomy level"):
        autonomy.set_autonomy_level(tmp_path, "ludicrous")


def test_the_level_is_stored_machine_local_never_in_the_synced_overlay(tmp_path):
    autonomy.set_autonomy_level(tmp_path, "supervised")
    gp = CohortPaths.for_global(tmp_path)
    assert (gp.state / "autonomy").is_file()          # under state/ (never synced)
    assert not (gp.my / "autonomy").exists()           # not in the my-office overlay


def test_clamp_only_ever_lowers_autonomy(tmp_path):
    # A request may reduce autonomy relative to the machine ceiling, never raise it.
    assert autonomy.clamp("autopilot", "guided") == "guided"   # capped down
    assert autonomy.clamp("paired", "autopilot") == "paired"   # already lower — unchanged
    assert autonomy.clamp("bogus", "autopilot") == "paired"    # unknown → safest


def test_every_level_has_a_description():
    for level in autonomy.LEVELS:
        assert autonomy.describe(level)


def test_recall_hook_targets_session_start_and_the_cli():
    from cohort.ir import build_ir
    from cohort.adapters.claude import render_hook_entry
    from cohort.loader import load_artifact

    repo = Path(__file__).resolve().parents[1]
    r = load_artifact(repo / "canonical" / "hooks" / "autonomy-recall.md")
    event, entry = render_hook_entry(build_ir(r.frontmatter, r.body))
    assert event == "SessionStart"
    assert entry["hooks"][0]["command"] == "cohort autonomy-recall"

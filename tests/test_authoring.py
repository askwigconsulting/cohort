"""Authoring surface: add-skill / add-command / add-hook + edit (#66 increment 2).

Every kind is authorable across the my / office layers, and `edit` round-trips
frontmatter (it must never strip a personalized copy's override markers)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.loader import load_artifact

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    return src


@pytest.fixture
def home(tmp_path, source):
    h = tmp_path / "home"
    h.mkdir()
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=h)
    return h


def _my(home, kind_dir, name):
    return home / ".cohort" / "my" / "canonical" / kind_dir / f"{name}.md"


def test_add_skill_authors_my_office_and_places(source, home):
    proc = run_cli("add-skill", "weekly-review", "--description", "Summarize the week.",
                   "--triggers", "week, review", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert _my(home, "skills", "weekly-review").exists()
    assert not (source / "canonical" / "skills" / "weekly-review.md").exists()  # clone clean
    assert (home / ".claude" / "skills" / "weekly-review" / "SKILL.md").exists()
    assert "my office" in proc.stderr


def test_add_command_is_dry_run_safe_and_places(source, home):
    proc = run_cli("add-command", "standup", "--description", "Daily standup.",
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    fm = load_artifact(_my(home, "commands", "standup")).frontmatter
    assert fm["dry_run"] is True and fm["invocation"] == "standup"  # the safety invariant
    assert (home / ".claude" / "commands" / "standup.md").exists()


def test_add_hook_authors_and_places(source, home):
    proc = run_cli("add-hook", "note", "--description", "A note.", "--event", "session_start",
                   "--action", "cohort status", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    fm = load_artifact(_my(home, "hooks", "note")).frontmatter
    assert fm["event"] == "session_start" and fm["action"] == "cohort status"


def test_add_hook_bad_event_refused(source, home):
    proc = run_cli("add-hook", "note", "--description", "x.", "--event", "not_an_event",
                   "--action", "cohort status", "--source", str(source), home=home)
    assert proc.returncode == 1 and "validation" in proc.stderr.lower()
    assert not _my(home, "hooks", "note").exists()  # fail-closed, nothing left behind


def test_add_to_office_writes_the_clone(source, home):
    proc = run_cli("add-skill", "weekly-review", "--description", "x.", "--to", "office",
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (source / "canonical" / "skills" / "weekly-review.md").exists()
    assert "office layer" in proc.stderr


def test_cross_layer_collision_refused(source, home):
    # office-guide already exists in the office layer (a shipped skill)
    proc = run_cli("add-skill", "office-guide", "--description", "dup.",
                   "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "office layer" in proc.stderr and "already exists" in proc.stderr


def test_edit_replaces_body_and_recompiles(source, home, tmp_path):
    run_cli("add-skill", "weekly-review", "--description", "Old.", "--source", str(source), home=home)
    draft = tmp_path / "b.md"
    draft.write_text("Brand new skill body.\n", encoding="utf-8")
    proc = run_cli("edit", "skill", "weekly-review", "--body-file", str(draft),
                   "--description", "New desc.", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    src_art = load_artifact(_my(home, "skills", "weekly-review"))
    assert "Brand new skill body." in src_art.body
    assert src_art.frontmatter["description"] == "New desc."  # overlay applied
    placed = (home / ".claude" / "skills" / "weekly-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "Brand new skill body." in placed  # recompiled in


def test_edit_round_trips_and_preserves_override_markers(source, home, tmp_path):
    # the critical case: editing a personalized copy must keep overrides/office_sha256
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    run_cli("personalize", "agent", "counsel", "--source", str(source), home=home)
    copy = _my(home, "agents", "counsel")
    before = load_artifact(copy).frontmatter
    assert before.get("overrides") is True and "office_sha256" in before
    draft = tmp_path / "b.md"
    draft.write_text("My house rules for counsel.\n", encoding="utf-8")
    proc = run_cli("edit", "agent", "counsel", "--body-file", str(draft),
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    after = load_artifact(copy).frontmatter
    assert after.get("overrides") is True  # not stripped by the edit
    assert after.get("office_sha256") == before["office_sha256"]  # preserved verbatim


def test_edit_office_needs_explicit_layer(source, home, tmp_path):
    draft = tmp_path / "b.md"
    draft.write_text("edited.\n", encoding="utf-8")
    # counsel lives in the office layer; default --layer my can't find it
    proc = run_cli("edit", "agent", "counsel", "--body-file", str(draft),
                   "--source", str(source), home=home)
    assert proc.returncode == 1 and "my office" in proc.stderr
    proc = run_cli("edit", "agent", "counsel", "--body-file", str(draft),
                   "--layer", "office", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert "edited." in load_artifact(source / "canonical" / "agents" / "counsel.md").body
    assert "office layer" in proc.stderr


def test_edit_missing_and_empty_refused(source, home):
    proc = run_cli("edit", "skill", "nope", "--description", "x", "--source", str(source), home=home)
    assert proc.returncode == 1 and "no skill" in proc.stderr
    run_cli("add-skill", "s", "--description", "x.", "--source", str(source), home=home)
    proc = run_cli("edit", "skill", "s", "--source", str(source), home=home)
    assert proc.returncode == 1 and "nothing to edit" in proc.stderr

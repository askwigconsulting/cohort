"""`cohort adopt` — lifting loose pre-Cohort artifacts into canonical.

Behavioral tests drive the real CLI against a temp source copy and temp home,
mirroring the add-agent harness (R3: the real roster is never mutated).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

COHORT_SRC = Path(__file__).resolve().parents[1]

LOOSE_AGENT = (
    "---\nname: code-reviewer\ndescription: Reviews diffs for defects.\n---\n"
    "# Code Reviewer\n\nYou review code across correctness and readability.\n"
)
LOOSE_COMMAND = "Run the full build and report failures.\n"


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
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


def _loose(home: Path, sub: str, name: str, text: str) -> Path:
    d = home / ".claude" / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_adopt_agent_becomes_managed_and_advisory(source, home):
    loose = _loose(home, "agents", "code-reviewer", LOOSE_AGENT)
    proc = run_cli("adopt", str(loose), "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    canonical = source / "canonical" / "agents" / "code-reviewer.md"
    text = canonical.read_text(encoding="utf-8")
    assert "advisory: true" in text  # the v1 safety invariant applies to adoptees
    assert "You review code across correctness" in text  # body preserved
    placed = home / ".claude" / "agents" / "code-reviewer.md"
    assert placed.exists()
    assert "advisory read-only" in proc.stderr  # the enforcement is said out loud
    backup = home / ".cohort" / "state" / "adopt-backups" / "agent-code-reviewer.md"
    assert backup.read_text(encoding="utf-8") == LOOSE_AGENT  # original kept, never deleted


def test_adopted_agent_appears_in_chief_directory(source, home):
    loose = _loose(home, "agents", "code-reviewer", LOOSE_AGENT)
    run_cli("adopt", str(loose), "--source", str(source), home=home)
    chief = (home / ".claude" / "agents" / "chief-of-staff.md").read_text(encoding="utf-8")
    assert "**CodeReviewer**" in chief  # no longer invisible to the router


def test_adopt_command_requires_description_flag_when_file_has_none(source, home):
    loose = _loose(home, "commands", "build", LOOSE_COMMAND)
    proc = run_cli("adopt", str(loose), "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "--description" in proc.stderr
    proc = run_cli("adopt", str(loose), "--description", "Build and report.",
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".claude" / "commands" / "build.md").exists()
    text = (source / "canonical" / "commands" / "build.md").read_text(encoding="utf-8")
    assert "Run the full build" in text


def test_adopt_refuses_files_outside_claude_dirs(source, home, tmp_path):
    stray = tmp_path / "stray.md"
    stray.write_text("x\n", encoding="utf-8")
    proc = run_cli("adopt", str(stray), "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "adopt" in proc.stderr.lower() or "not under" in proc.stderr


def test_adopt_refuses_a_cohort_managed_symlink(source, home):
    managed = home / ".claude" / "agents" / "counsel.md"
    assert managed.is_symlink()  # placed by the fixture recompile
    proc = run_cli("adopt", str(managed), "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "already" in proc.stderr


def test_adopt_refuses_canonical_name_collision(source, home):
    # A user replaced the managed /update with their own file; adopting it must be
    # refused (canonical already has that name), and the refusal must not touch it.
    placed = home / ".claude" / "commands" / "update.md"
    placed.unlink()  # drop the managed symlink first so the loose file is real
    loose = _loose(home, "commands", "update", "---\ndescription: dup.\n---\nbody\n")
    proc = run_cli("adopt", str(loose), "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "already exists" in proc.stderr
    assert loose.exists()


def test_adopt_dry_run_changes_nothing(source, home):
    loose = _loose(home, "agents", "code-reviewer", LOOSE_AGENT)
    proc = run_cli("adopt", str(loose), "--dry-run", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert not (source / "canonical" / "agents" / "code-reviewer.md").exists()
    assert loose.read_text(encoding="utf-8") == LOOSE_AGENT


def test_status_lists_unmanaged_then_clean_after_adopt(source, home):
    loose = _loose(home, "agents", "code-reviewer", LOOSE_AGENT)
    report = json.loads(run_cli("status", "--json", home=home).stdout)
    assert str(loose) in report["global"]["unmanaged"]
    run_cli("adopt", str(loose), "--source", str(source), home=home)
    report = json.loads(run_cli("status", "--json", home=home).stdout)
    assert report["global"]["unmanaged"] == []  # the shadow office is gone


def test_adopt_extends_a_persisted_roster_subset(source, home, tmp_path):
    fresh_home = tmp_path / "home2"
    fresh_home.mkdir()
    run_cli("setup", "--ide", "claude", "--agents", "counsel,chief-of-staff",
            "--source", str(source), home=fresh_home)
    loose = _loose(fresh_home, "agents", "code-reviewer", LOOSE_AGENT)
    proc = run_cli("adopt", str(loose), "--source", str(source), home=fresh_home)
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(
        (fresh_home / ".cohort" / "state" / "manifest.json").read_text(encoding="utf-8")
    )
    assert "code-reviewer" in manifest["roster"]  # survives the next recompile
    assert (fresh_home / ".claude" / "agents" / "code-reviewer.md").exists()

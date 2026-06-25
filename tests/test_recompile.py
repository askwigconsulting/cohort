"""P2-T4: recompile, end-to-end Claude install, idempotency & golden parity.

The checked-in golden tree under tests/golden/claude/ is the reference Phase 7
diffs Codex/Cursor against.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PHASE2_SRC = REPO_ROOT / "tests" / "fixtures" / "phase2"
GOLDEN = REPO_ROOT / "tests" / "golden" / "claude"

ONE_TO_ONE = [
    "agents/security-engineer.md",
    "agents/chief-of-staff.md",
    "skills/weekly-report/SKILL.md",
    "commands/snapshot.md",
    "cohort/CLAUDE.cohort.md",
]


def run_cli(*args, home, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
    env.pop("COHORT_SOURCE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], capture_output=True, text=True, env=env
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def recompile(home, source=PHASE2_SRC):
    return run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)


# --- recompile semantics ----------------------------------------------------


def test_recompile_then_unchanged_recompile_is_noop(home):
    assert recompile(home).returncode == 0
    second = recompile(home)
    assert "applied: 0" in second.stdout


def test_dry_run_recompile_writes_nothing(home):
    proc = run_cli(
        "recompile", "--ide", "claude", "--source", str(PHASE2_SRC), "--dry-run", home=home
    )
    assert proc.returncode == 0
    assert not (home / ".cohort").exists()
    assert not (home / ".claude" / "agents").exists()


def test_editing_one_artifact_updates_only_that_dest(tmp_path):
    # mutable copy of the source so we can edit a canonical artifact
    src = tmp_path / "src"
    shutil.copytree(PHASE2_SRC, src)
    home = tmp_path / "home"
    home.mkdir()
    recompile(home, source=src)

    sec = home / ".claude" / "agents" / "security-engineer.md"
    chief = home / ".claude" / "agents" / "chief-of-staff.md"
    chief_before = chief.read_bytes()

    # edit one canonical artifact's body
    art = src / "canonical" / "agents" / "security-engineer.md"
    art.write_text(art.read_text().replace("secure-by-default posture", "ZZZ MARKER"), encoding="utf-8")
    assert recompile(home, source=src).returncode == 0

    assert b"ZZZ MARKER" in sec.read_bytes()  # the edited dest updated
    assert chief.read_bytes() == chief_before  # the untouched dest unchanged


# --- full reference build ---------------------------------------------------


def test_full_reference_build_matches_golden_and_round_trips(home):
    assert recompile(home).returncode == 0
    claude = home / ".claude"
    for rel in ONE_TO_ONE:
        assert claude.joinpath(rel).read_bytes() == (GOLDEN / rel).read_bytes(), rel

    # recompile twice → byte-stable, no churn
    assert "applied: 0" in recompile(home).stdout
    assert "applied: 0" in recompile(home).stdout

    # uninstall → .claude cleaned, no ~/.cohort staging left
    assert run_cli("uninstall", home=home).returncode == 0
    assert not (home / ".cohort").exists()
    assert not claude.joinpath("agents", "security-engineer.md").exists()
    # CLAUDE.md / settings.json were created by Cohort (no user file) → removed
    assert not claude.joinpath("CLAUDE.md").exists()
    assert not claude.joinpath("settings.json").exists()


def test_golden_tree_is_present():
    # the golden lock artifact must exist and cover every reference kind
    for rel in ONE_TO_ONE:
        assert (GOLDEN / rel).exists(), rel
    assert (GOLDEN / "merge" / "settings.hooks.json").exists()
    assert (GOLDEN / "merged" / "CLAUDE.md").exists()

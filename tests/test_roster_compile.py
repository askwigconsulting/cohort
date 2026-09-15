"""P3-T3: compile + install the roster end-to-end, golden parity, delegation DoD."""

from __future__ import annotations

import re

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROSTER_GOLDEN = REPO_ROOT / "tests" / "golden" / "roster" / "claude" / "agents"
CANON_AGENTS = REPO_ROOT / "canonical" / "agents"

ROSTER_NAMES = sorted(p.stem for p in CANON_AGENTS.glob("*.md"))
SPECIALIST_DISPLAY = [
    "HRPartner", "Counsel", "Compliance", "SecurityEngineer", "FinanceAnalyst",
    "ITSupport", "Comms", "Procurement", "PrivacyOfficer", "ProgramManager",
    "CloudArchitect", "Steward",
    "CodeReviewer", "TestEngineer", "DataAnalyst", "Researcher",
]
MUTATING = {"Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"}


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


def recompile(home):
    return run_cli("recompile", "--ide", "claude", "--source", str(REPO_ROOT), home=home)


# --- golden parity ----------------------------------------------------------


def test_fifteen_agents_have_goldens():
    assert len(ROSTER_NAMES) == 17
    for name in ROSTER_NAMES:
        assert (ROSTER_GOLDEN / f"{name}.md").exists(), name


@pytest.mark.parametrize("name", ROSTER_NAMES)
def test_rendered_agent_matches_golden(name, home):
    assert recompile(home).returncode == 0
    placed = home / ".claude" / "agents" / f"{name}.md"
    assert placed.read_bytes() == (ROSTER_GOLDEN / f"{name}.md").read_bytes()


@pytest.mark.parametrize("name", ROSTER_NAMES)
def test_every_agent_is_read_only(name):
    # advisory tool-strip (R7): no mutating tool in any rendered agent
    text = (ROSTER_GOLDEN / f"{name}.md").read_text()
    tools_line = next(ln for ln in text.splitlines() if ln.startswith("tools:"))
    tools = {t.strip() for t in tools_line.split(":", 1)[1].split(",")}
    assert tools.isdisjoint(MUTATING), (name, tools)


# --- install / idempotency / uninstall -------------------------------------


def test_install_places_fifteen_idempotently(home):
    assert recompile(home).returncode == 0
    placed = list((home / ".claude" / "agents").glob("*.md"))
    assert len(placed) == 17
    assert "applied: 0" in recompile(home).stdout  # idempotent


def test_uninstall_removes_roster_preserving_user_content(home):
    (home / ".claude" / "agents").mkdir(parents=True)
    user_file = home / ".claude" / "agents" / "my-own.md"
    user_file.write_text("mine\n", encoding="utf-8")
    recompile(home)
    assert run_cli("uninstall", "--ide", "claude", home=home).returncode == 0
    assert not (home / ".claude" / "agents" / "counsel.md").exists()
    assert user_file.read_text() == "mine\n"  # pre-existing content survives (M4)


def test_recompile_twice_byte_stable(home):
    recompile(home)
    first = (home / ".claude" / "agents" / "chief-of-staff.md").read_bytes()
    assert "applied: 0" in recompile(home).stdout
    second = (home / ".claude" / "agents" / "chief-of-staff.md").read_bytes()
    assert first == second


# --- delegation DoD ---------------------------------------------------------


def test_delegation_dod_chief_names_every_specialist(home):
    assert recompile(home).returncode == 0
    chief = (home / ".claude" / "agents" / "chief-of-staff.md").read_text()
    for display in SPECIALIST_DISPLAY:
        assert f"**{display}**" in chief, display
    assert "**ChiefOfStaff**" not in chief.split("Office directory.")[1]  # not itself


# --- routing memory (the top-level agent is the real router) ----------------


def test_office_routing_memory_reaches_global_claude_md(home):
    assert recompile(home).returncode == 0
    corpus = home / ".claude" / "cohort" / "CLAUDE.cohort.md"
    assert "ChiefOfStaff" in corpus.read_text(encoding="utf-8")
    claude_md = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@cohort/CLAUDE.cohort.md" in claude_md  # imported into top-level memory


def test_office_guide_skill_places(home):
    # the one surface the Claude Desktop app can read — must never be empty
    assert recompile(home).returncode == 0
    placed = home / ".claude" / "skills" / "office-guide" / "SKILL.md"
    assert placed.exists()
    assert "ChiefOfStaff" in placed.read_text(encoding="utf-8")


# --- #300 item 3: --layer/--to are hidden aliases of each other -------------


@pytest.fixture
def office_source(tmp_path):
    """A throwaway clone of canonical/ so an office-layer write never touches
    this checkout's real tree."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(REPO_ROOT / "canonical", src / "canonical")
    return src


def test_add_agent_layer_alias_writes_office_like_to(office_source, home):
    proc = run_cli(
        "add-agent", "--name", "layer-alias-check", "--department", "Ops",
        "--description", "Alias smoke test.", "--layer", "office",
        "--source", str(office_source), home=home,
    )
    assert proc.returncode == 0, proc.stderr
    assert (office_source / "canonical" / "agents" / "layer-alias-check.md").exists()


def test_edit_to_alias_edits_office_like_layer(office_source, home):
    add = run_cli(
        "add-agent", "--name", "edit-alias-check", "--department", "Ops",
        "--description", "Original description.", "--to", "office",
        "--source", str(office_source), home=home,
    )
    assert add.returncode == 0, add.stderr

    edited = run_cli(
        "edit", "agent", "edit-alias-check", "--description", "Edited via --to alias.",
        "--to", "office", "--source", str(office_source), home=home,
    )
    assert edited.returncode == 0, edited.stderr
    text = (office_source / "canonical" / "agents" / "edit-alias-check.md").read_text()
    assert "Edited via --to alias." in text


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_add_agent_help_lists_only_the_canonical_to_spelling():
    proc = run_cli("add-agent", "--help", home=Path.home())
    text = _ANSI.sub("", proc.stdout)  # CI terminals colour --help; strip before matching
    assert "--to" in text
    assert "--layer" not in text


def test_edit_help_lists_only_the_canonical_layer_spelling():
    proc = run_cli("edit", "--help", home=Path.home())
    text = _ANSI.sub("", proc.stdout)
    assert "--layer" in text
    assert "--to" not in text


def test_add_agent_layer_alias_rejects_the_same_way_as_to(office_source, home):
    via_to = run_cli(
        "add-agent", "--name", "x", "--department", "Ops", "--description", "d",
        "--to", "bogus", "--source", str(office_source), home=home,
    )
    via_layer = run_cli(
        "add-agent", "--name", "x", "--department", "Ops", "--description", "d",
        "--layer", "bogus", "--source", str(office_source), home=home,
    )
    assert via_to.returncode == via_layer.returncode == 1
    assert via_to.stderr.strip() == via_layer.stderr.strip()

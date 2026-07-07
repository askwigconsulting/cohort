"""Project-level authoring of any kind — the project analogue of the global
add-<kind> commands. A skill/command/hook/agent can be created at project scope
(`<repo>/.cohort/canonical/<kind>/`) and compiles+places into the repo's IDE tree.
Memory is excluded (the project tier has no memory compile target)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.install_model import CohortPaths
from cohort.specialists import AddSpecialistError, do_add_project_artifact

COHORT_SRC = Path(__file__).resolve().parents[1]


def _run_cli(*args, home, cwd):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def repo(tmp_path, home):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    proc = _run_cli("init", "--source", str(COHORT_SRC), home=home, cwd=r)
    assert proc.returncode == 0, proc.stderr
    return r


def _src(repo: Path, kind_dir: str, name: str) -> Path:
    return CohortPaths.for_project(repo).canonical / kind_dir / f"{name}.md"


def test_create_project_skill_places_and_is_project_scoped(repo, home):
    res = do_add_project_artifact(repo, home, "skill", "data-quality", "Data quality checks.")
    assert res["kind"] == "skill"
    assert (repo / ".claude" / "skills" / "data-quality" / "SKILL.md").exists()
    assert "scope: project" in _src(repo, "skills", "data-quality").read_text(encoding="utf-8")


def test_create_project_command_places(repo, home):
    do_add_project_artifact(repo, home, "command", "ship-it", "Ship the release.")
    assert (repo / ".claude" / "commands" / "ship-it.md").exists()
    assert "scope: project" in _src(repo, "commands", "ship-it").read_text(encoding="utf-8")


def test_create_project_hook_places_into_settings(repo, home):
    do_add_project_artifact(
        repo, home, "hook", "guard", "Guard commits.", event="pre_command", action="cohort status"
    )
    settings = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert "cohort status" in settings  # the hook action reached the repo's settings.json
    assert "scope: project" in _src(repo, "hooks", "guard").read_text(encoding="utf-8")


def test_create_project_agent_routes_through_specialist(repo, home):
    do_add_project_artifact(repo, home, "agent", "data-modeler", "Schema advice.", department="Data")
    assert (repo / ".claude" / "agents" / "data-modeler.md").exists()


def test_memory_is_refused_at_project_scope(repo, home):
    with pytest.raises(AddSpecialistError, match="supported"):
        do_add_project_artifact(repo, home, "memory", "team-context", "x.")


def test_duplicate_is_refused(repo, home):
    do_add_project_artifact(repo, home, "skill", "dup", "First.")
    with pytest.raises(AddSpecialistError, match="already exists"):
        do_add_project_artifact(repo, home, "skill", "dup", "Second.")


def test_hook_without_event_is_refused(repo, home):
    with pytest.raises(AddSpecialistError, match="event and an action"):
        do_add_project_artifact(repo, home, "hook", "bad", "No event.")

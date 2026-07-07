"""Project-level authoring of any kind — the project analogue of the global
add-<kind> commands. A skill/command/hook/agent/memory can be created at project
scope (`<repo>/.cohort/canonical/<kind>/`) and compiles+places into the repo's IDE
tree; project memories compile into the repo's own CLAUDE.md corpus."""

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


def test_create_project_memory_compiles_into_corpus_and_wires_import(repo, home):
    from cohort import merge as _merge

    do_add_project_artifact(
        repo, home, "memory", "repo-conventions", "How this repo works.",
        priority="high", body="PROJECT-MEMORY-MARKER conventions.",
    )
    # source is project-scoped
    assert "scope: project" in _src(repo, "memories", "repo-conventions").read_text(encoding="utf-8")
    # compiled into the repo's own corpus
    corpus = repo / ".claude" / "cohort" / "CLAUDE.cohort.md"
    assert corpus.exists() and "PROJECT-MEMORY-MARKER" in corpus.read_text(encoding="utf-8")
    # the managed CLAUDE.md block now imports both the context and the memory corpus
    inner = _merge.extract_block((repo / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))
    assert "@import ../.cohort/project_context.md" in inner
    assert "@import cohort/CLAUDE.cohort.md" in inner


def test_removing_last_project_memory_unwires_the_import(repo, home):
    from cohort import merge as _merge
    from cohort.install import do_install_project

    do_add_project_artifact(repo, home, "memory", "temp", "Temp memory.")
    # delete the source and recompile — the corpus import should drop back out
    _src(repo, "memories", "temp").unlink()
    do_install_project(repo)
    inner = _merge.extract_block((repo / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))
    assert "@import cohort/CLAUDE.cohort.md" not in inner
    assert "@import ../.cohort/project_context.md" in inner


def test_duplicate_is_refused(repo, home):
    do_add_project_artifact(repo, home, "skill", "dup", "First.")
    with pytest.raises(AddSpecialistError, match="already exists"):
        do_add_project_artifact(repo, home, "skill", "dup", "Second.")


def test_hook_without_event_is_refused(repo, home):
    with pytest.raises(AddSpecialistError, match="event and an action"):
        do_add_project_artifact(repo, home, "hook", "bad", "No event.")

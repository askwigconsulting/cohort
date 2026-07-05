"""Multi-project registry + `cohort projects` + the dashboard switcher (#66 inc 4)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.project import list_projects, resolve_registered

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
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _repo(tmp_path, source, home, name):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    return repo


def test_init_registers_and_deinit_deregisters(tmp_path, source, home):
    repo = _repo(tmp_path, source, home, "alpha")
    projects = list_projects(home)
    assert [p["name"] for p in projects] == ["alpha"]
    run_cli("deinit", "--purge", home=home, cwd=repo)
    assert list_projects(home) == []


def test_multiple_projects_are_listed(tmp_path, source, home):
    _repo(tmp_path, source, home, "alpha")
    _repo(tmp_path, source, home, "beta")
    names = sorted(p["name"] for p in list_projects(home))
    assert names == ["alpha", "beta"]


def test_dead_entry_is_pruned(tmp_path, source, home):
    _repo(tmp_path, source, home, "alpha")
    beta = _repo(tmp_path, source, home, "beta")
    shutil.rmtree(beta)  # repo deleted out from under Cohort
    listed = list_projects(home)
    assert [p["name"] for p in listed] == ["alpha"]  # beta pruned
    reg = json.loads((home / ".cohort" / "state" / "projects.json").read_text(encoding="utf-8"))
    assert all("beta" not in p for p in reg["projects"])  # rewrite removed it


def test_home_is_never_registered(tmp_path, source, home):
    # init at $HOME is refused by the CLI; the registry must also never hold it
    subprocess.run(["git", "init", "-q"], cwd=home, check=True)
    run_cli("init", "--source", str(source), home=home, cwd=home)  # exits 2, no register
    assert list_projects(home) == []


def test_projects_command_lists(tmp_path, source, home):
    _repo(tmp_path, source, home, "alpha")
    proc = run_cli("projects", home=home)
    assert proc.returncode == 0 and "alpha" in proc.stdout
    data = json.loads(run_cli("projects", "--json", home=home).stdout)
    assert data["projects"][0]["name"] == "alpha"


def test_resolve_registered_takes_an_index_not_a_path(tmp_path, source, home):
    repo = _repo(tmp_path, source, home, "alpha")
    assert resolve_registered(home, 0) == repo.resolve()
    assert resolve_registered(home, 99) is None            # out of range
    assert resolve_registered(home, str(repo)) is None     # a path is not an index
    assert resolve_registered(home, "../../etc") is None   # never a client path


# --- dashboard switcher ------------------------------------------------------

from cohort.dashboard import collect_state, run_action  # noqa: E402


def test_collect_state_lists_projects_and_focuses_by_index(tmp_path, source, home, monkeypatch):
    monkeypatch.setenv("COHORT_SOURCE", str(source))
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    alpha = _repo(tmp_path, source, home, "alpha")
    beta = _repo(tmp_path, source, home, "beta")
    run_cli("add-specialist", "--name", "beta-only", "--display-name", "BetaOnly",
            "--department", "X", "--description", "x.", home=home, cwd=beta)
    # focus beta (its index) from a neutral cwd
    order = [p["name"] for p in list_projects(home)]
    beta_idx = order.index("beta")
    state = collect_state(home, tmp_path, None, beta_idx)
    assert state["focused_project"] == str(beta.resolve())
    assert state["project"]["specialists"] == ["beta-only"]
    assert {p["name"] for p in state["projects"]} == {"alpha", "beta"}


def test_action_targets_the_focused_project(tmp_path, source, home, monkeypatch):
    monkeypatch.setenv("COHORT_SOURCE", str(source))
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    _repo(tmp_path, source, home, "alpha")
    beta = _repo(tmp_path, source, home, "beta")
    beta_idx = [p["name"] for p in list_projects(home)].index("beta")
    # add a specialist to beta via a run_action focused on beta, from a neutral cwd
    run_action(home, tmp_path, "add-specialist",
               {"name": "focused", "description": "x.", "project": beta_idx})
    assert (beta / ".cohort" / "canonical" / "agents" / "focused.md").exists()

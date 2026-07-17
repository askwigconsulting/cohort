"""Project-scoped memories (#182): authoring, corpus wiring, and git-state surfacing.

The compile/install half already existed (`install.py`'s `has_memory` →
`project.py`'s second `@import`); what's covered here is the authoring path
(`add-memory --to project`), the wiring round-trip, and the git-state signal the
CLI/dashboard surface so a user can judge a memory that *travels with the repo*.
Cohort reports that state and blocks neither choice — it is the user's call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.gitutil import git_state
from cohort.loader import load_artifact

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True,
        env=env, timeout=120,
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


@pytest.fixture
def repo(tmp_path, source, home):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "D"], cwd=r, check=True)
    (r / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    run_cli("init", "--source", str(source), home=home, cwd=r)
    return r


def add_project_memory(repo, home, source, name="atlas-conventions"):
    return run_cli(
        "add-memory", "--to", "project", "--name", name,
        "--display-name", name.replace("-", " ").title(),
        "--description", "Conventions for this repo.",
        "--source", str(source), home=home, cwd=repo,
    )


def _canonical(repo, name):
    return repo / ".cohort" / "canonical" / "memories" / f"{name}.md"


def test_add_memory_to_project_authors_scope_project(repo, home, source):
    res = add_project_memory(repo, home, source)
    assert res.returncode == 0, res.stderr
    dest = _canonical(repo, "atlas-conventions")
    assert dest.is_file()
    fm = load_artifact(dest).frontmatter
    assert fm["kind"] == "memory"
    assert fm["scope"] == "project"  # never "global" — this is the project tier


def test_project_memory_is_delivered_and_import_wired(repo, home, source):
    add_project_memory(repo, home, source)
    # The corpus carries the memory...
    corpus = repo / ".claude" / "cohort" / "CLAUDE.cohort.md"
    assert corpus.is_file(), "project memory corpus was not placed"
    assert "Atlas Conventions" in corpus.read_text(encoding="utf-8")
    # ...and the repo's CLAUDE.md imports it alongside the project context.
    claude_md = (repo / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@import ../.cohort/project_context.md" in claude_md
    assert "@import cohort/CLAUDE.cohort.md" in claude_md


def test_removing_the_last_project_memory_unwires_the_import(repo, home, source):
    add_project_memory(repo, home, source)
    _canonical(repo, "atlas-conventions").unlink()
    # Any project install path re-runs the wiring decision.
    run_cli("add-specialist", "--name", "x", "--display-name", "X",
            "--department", "Eng", "--description", "y.", home=home, cwd=repo)
    claude_md = (repo / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@import ../.cohort/project_context.md" in claude_md
    assert "@import cohort/CLAUDE.cohort.md" not in claude_md


def test_add_memory_to_project_outside_a_project_errors(home, source, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = run_cli(
        "add-memory", "--to", "project", "--name", "n", "--description", "d.",
        "--source", str(source), home=home, cwd=plain,
    )
    assert res.returncode != 0


def test_add_memory_rejects_an_unknown_layer(home, source, repo):
    res = run_cli(
        "add-memory", "--to", "elsewhere", "--name", "n", "--description", "d.",
        "--source", str(source), home=home, cwd=repo,
    )
    assert res.returncode != 0
    assert "my|office|project" in res.stderr


# --- the git-state signal (surface, never block) ------------------------------


def test_git_state_reports_untracked_then_tracked(repo, home, source):
    add_project_memory(repo, home, source)
    dest = _canonical(repo, "atlas-conventions")

    # Authored but not yet added: no audit trail — the user's call, not a block.
    assert git_state(repo, dest) == {"git": True, "tracked": False, "dirty": False}

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add memory"], cwd=repo, check=True)
    assert git_state(repo, dest) == {"git": True, "tracked": True, "dirty": False}

    dest.write_text(dest.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    assert git_state(repo, dest) == {"git": True, "tracked": True, "dirty": True}


def test_git_state_on_a_non_git_directory_is_unknown_not_an_error(tmp_path):
    plain = tmp_path / "nogit"
    plain.mkdir()
    (plain / "f.md").write_text("x", encoding="utf-8")
    assert git_state(plain, plain / "f.md") == {"git": False, "tracked": False, "dirty": False}


def test_git_states_batches_and_matches_git_state(repo, home, source):
    add_project_memory(repo, home, source, name="a")
    add_project_memory(repo, home, source, name="b")
    paths = [_canonical(repo, "a"), _canonical(repo, "b")]
    from cohort.gitutil import git_states

    batched = git_states(repo, paths)
    assert set(batched) == {str(p) for p in paths}
    # The batched result must agree with the per-file function it replaces.
    for p in paths:
        assert batched[str(p)] == git_state(repo, p)

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)
    after = git_states(repo, paths)
    assert all(s == {"git": True, "tracked": True, "dirty": False} for s in after.values())


def test_git_states_empty_and_non_git_are_safe(tmp_path):
    from cohort.gitutil import git_states

    assert git_states(tmp_path, []) == {}
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    assert git_states(tmp_path, [f]) == {str(f): {"git": False, "tracked": False, "dirty": False}}


def test_dashboard_state_carries_git_for_project_memories_only(repo, home, source):
    """The card needs the signal, so collect_state attaches it — to project
    memories only (office/my memories don't travel with a repo)."""
    from cohort.dashboard import collect_state

    add_project_memory(repo, home, source)
    state = collect_state(home, repo)
    proj_mem = [
        it for it in state["inventory"] if it["layer"] == "project" and it["kind"] == "memory"
    ]
    assert len(proj_mem) == 1
    assert proj_mem[0]["git"] == {"git": True, "tracked": False, "dirty": False}
    # Not attached where it would be meaningless.
    others = [it for it in state["inventory"] if it["layer"] == "office"]
    assert others and all("git" not in it for it in others)


def test_authoring_a_project_memory_surfaces_the_travels_with_repo_note(repo, home, source):
    res = add_project_memory(repo, home, source)
    assert "travels with it" in res.stderr
    assert "untracked" in res.stderr  # the honest state at authoring time

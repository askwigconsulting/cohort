"""Phase 4: tier-aware compile — the scope partition (the leak guard).

A tier only ever compiles its own scope, so a scope:project artifact can never
reach the global office and a scope:global artifact never reaches a project tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cohort.compile import compile_ide

REPO_ROOT = Path(__file__).resolve().parents[1]


def _source_with_project_command(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    shutil.copytree(REPO_ROOT / "canonical", src / "canonical")
    (src / "canonical" / "commands" / "proj-only.md").write_text(
        "---\nname: proj-only\nkind: command\nscope: project\n"
        "description: A project-scoped command.\ntargets: [claude]\n"
        "invocation: proj-only\ndry_run: true\n---\nProject command body.\n",
        encoding="utf-8",
    )
    return src


def test_global_compile_excludes_project_artifacts(tmp_path):
    src = _source_with_project_command(tmp_path)
    staged = [sf.staged_rel for sf in compile_ide(src, "claude", scope="global").staged]
    assert "commands/proj-only.md" not in staged  # the leak guard
    assert "commands/update.md" in staged  # global artifacts still compile


def test_project_compile_includes_only_project_artifacts(tmp_path):
    src = _source_with_project_command(tmp_path)
    staged = [sf.staged_rel for sf in compile_ide(src, "claude", scope="project").staged]
    assert "commands/proj-only.md" in staged
    assert "commands/update.md" not in staged  # global excluded from the project tier


def test_unfiltered_compile_includes_all_scopes(tmp_path):
    src = _source_with_project_command(tmp_path)
    staged = [sf.staged_rel for sf in compile_ide(src, "claude").staged]  # scope=None
    assert "commands/proj-only.md" in staged and "commands/update.md" in staged


# --- increment 2: project-tier compile + place (isolation) ------------------

from cohort.compile import CompileError  # noqa: E402
from cohort.executor import reverse_full  # noqa: E402
from cohort.install import do_install_project  # noqa: E402
from cohort.install_model import CohortPaths  # noqa: E402
from cohort.manifest import Manifest, load_manifest  # noqa: E402
from conftest import requires_symlinks  # noqa: E402

_AGENT = ("---\nname: {n}\nkind: agent\nscope: project\ndescription: A project specialist.\n"
          "targets: [claude]\ndepartment: X\ntopology: specialist\nadvisory: true\ntools: [read]\n"
          "---\nProject agent body.\n")
_CMD = ("---\nname: {n}\nkind: command\nscope: project\ndescription: A project command.\n"
        "targets: [claude]\ninvocation: {n}\ndry_run: true\n---\nProject command body.\n")


def _project(repo: Path) -> CohortPaths:
    ppaths = CohortPaths.for_project(repo)
    ppaths.state.mkdir(parents=True)
    Manifest(install_id="proj00000001", created_at="2026-01-01T00:00:00+00:00",
             mode="link", ides=["project"]).persist(ppaths.manifest)
    return ppaths


def _add(ppaths: CohortPaths, sub: str, name: str, text: str) -> None:
    d = ppaths.cohort_home / "canonical" / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text, encoding="utf-8")


@requires_symlinks
def test_project_compile_places_into_repo_claude(tmp_path):
    repo = tmp_path / "repo"
    ppaths = _project(repo)
    _add(ppaths, "agents", "helper", _AGENT.format(n="helper"))
    _add(ppaths, "commands", "deploy", _CMD.format(n="deploy"))
    report = do_install_project(repo)
    assert report["applied"] >= 2
    assert (repo / ".claude" / "agents" / "helper.md").is_symlink()
    assert (repo / ".claude" / "commands" / "deploy.md").is_symlink()


@requires_symlinks
def test_project_artifacts_isolated_across_repos(tmp_path):
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    pa = _project(repo_a)
    _project(repo_b)
    _add(pa, "commands", "only-a", _CMD.format(n="only-a"))
    do_install_project(repo_a)
    assert (repo_a / ".claude" / "commands" / "only-a.md").exists()
    assert not (repo_b / ".claude").exists()  # the other repo is untouched


@requires_symlinks
def test_project_reverse_removes_artifacts(tmp_path):
    repo = tmp_path / "repo"
    ppaths = _project(repo)
    _add(ppaths, "commands", "deploy", _CMD.format(n="deploy"))
    do_install_project(repo)
    placed = repo / ".claude" / "commands" / "deploy.md"
    assert placed.exists()
    reverse_full(load_manifest(ppaths.manifest), ppaths)
    assert not placed.exists()  # reverse-by-tier removes the project placement


def test_project_tier_rejects_a_generalist(tmp_path):
    repo = tmp_path / "repo"
    ppaths = _project(repo)
    _add(ppaths, "agents", "gen", _AGENT.format(n="gen").replace("specialist", "generalist"))
    with pytest.raises(CompileError):
        do_install_project(repo)

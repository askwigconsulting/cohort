"""Phase 4: tier-aware compile — the scope partition (the leak guard).

A tier only ever compiles its own scope, so a scope:project artifact can never
reach the global office and a scope:global artifact never reaches a project tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

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

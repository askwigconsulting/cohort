"""Phase 4: project home, project_context + index, snapshot, staleness.

Covers P4-T1..T4. Behavioral/integration tests drive the real CLI in a temp git
repo; unit tests call the project functions directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort import project
from cohort.adapters.claude import render_hook_entry
from cohort.install_model import CohortPaths
from cohort.ir import build_ir
from cohort.loader import load_artifact
from cohort.merge import BLOCK_BEGIN, extract_block

COHORT_SRC = Path(__file__).resolve().parents[1]


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@test.com"], cwd=path, check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def run_cli(*args, repo: Path, home: Path):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=repo, capture_output=True, text=True, env=env
    )


@pytest.fixture
def repo(tmp_path):
    return make_git_repo(tmp_path / "repo")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def init(repo, home, *extra):
    return run_cli("init", "--source", str(COHORT_SRC), *extra, repo=repo, home=home)


# === P4-T1: project home, executor extension, gitignore split ===============


def test_init_creates_layout_with_gitignore_split(repo, home):
    assert init(repo, home).returncode == 0
    c = repo / ".cohort"
    assert (c / "project_context.md").exists()
    assert (c / "cohort.toml").exists()
    assert (c / "sessions").is_dir()
    assert (c / "state" / "manifest.json").exists()
    gitignore = (c / ".gitignore").read_text()
    assert "state/" in gitignore and "compiled/" in gitignore


def _is_ignored(repo: Path, rel: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode == 0


def test_gitignore_actually_ignores_bookkeeping(repo, home):
    init(repo, home)
    assert _is_ignored(repo, ".cohort/state")  # bookkeeping ignored
    assert _is_ignored(repo, ".cohort/compiled")
    assert not _is_ignored(repo, ".cohort/project_context.md")  # content tracked
    assert not _is_ignored(repo, ".cohort/cohort.toml")


def test_init_is_idempotent(repo, home):
    init(repo, home)
    second = init(repo, home)
    assert "applied 0" in second.stdout


def test_init_dry_run_writes_nothing(repo, home):
    proc = init(repo, home, "--dry-run")
    assert proc.returncode == 0
    assert not (repo / ".cohort").exists()


def test_scaffold_does_not_overwrite_existing_context(repo, home):
    init(repo, home)
    ctx = repo / ".cohort" / "project_context.md"
    ctx.write_text("MY HAND-EDITED CONTEXT\n", encoding="utf-8")
    init(repo, home)  # re-init
    assert ctx.read_text() == "MY HAND-EDITED CONTEXT\n"  # create-if-absent


def test_deinit_preserves_content_removes_wiring(repo, home):
    init(repo, home)
    assert run_cli("deinit", repo=repo, home=home).returncode == 0
    c = repo / ".cohort"
    assert (c / "project_context.md").exists()  # preserved
    assert (c / "cohort.toml").exists()
    assert not (c / "state").exists()  # bookkeeping gone
    assert not (c / ".gitignore").exists()  # wiring gone
    assert not (repo / ".claude" / "CLAUDE.md").exists()  # created-only, [L] removed


def test_deinit_purge_returns_to_fresh_repo(repo, home):
    init(repo, home)
    run_cli("snapshot", repo=repo, home=home)  # untracked session content too
    assert run_cli("deinit", "--purge", repo=repo, home=home).returncode == 0
    assert not (repo / ".cohort").exists()
    assert not (repo / ".claude").exists()


def test_deinit_dry_run_writes_nothing(repo, home):
    init(repo, home)
    proc = run_cli("deinit", "--dry-run", repo=repo, home=home)
    assert proc.returncode == 0
    assert (repo / ".cohort" / "state").exists()  # nothing removed


# === P4-T2: template, managed index, Claude wiring ==========================


def test_context_template_has_sections_and_managed_block(repo, home):
    init(repo, home)
    text = (repo / ".cohort" / "project_context.md").read_text()
    for section in ("## Purpose", "## Architecture", "## Decisions", "## Glossary", "## Recent sessions"):
        assert section in text
    assert BLOCK_BEGIN in text  # the (empty) managed index block


def test_import_wiring_path_is_correct(repo, home):
    init(repo, home)
    claude_md = (repo / ".claude" / "CLAUDE.md").read_text()
    assert "@import ../.cohort/project_context.md" in claude_md
    # the path resolves to the real file
    assert (repo / ".claude" / ".." / ".cohort" / "project_context.md").resolve().exists()


def test_wiring_preserves_user_claude_md(repo, home):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "CLAUDE.md").write_text("# my project memory\n- a rule\n", encoding="utf-8")
    init(repo, home)
    text = (repo / ".claude" / "CLAUDE.md").read_text()
    assert "my project memory" in text  # preserved (K)
    assert "@import ../.cohort/project_context.md" in text
    run_cli("deinit", repo=repo, home=home)
    after = (repo / ".claude" / "CLAUDE.md").read_text()
    assert "my project memory" in after  # file kept (L), block removed
    assert "@import" not in after


def test_context_refresh_is_deterministic_and_idempotent(repo, home):
    init(repo, home)
    run_cli("snapshot", repo=repo, home=home)
    assert run_cli("context", "refresh", repo=repo, home=home).returncode == 0
    first = (repo / ".cohort" / "project_context.md").read_text()
    second_run = run_cli("context", "refresh", repo=repo, home=home)
    assert "no change" in second_run.stdout  # idempotent
    assert (repo / ".cohort" / "project_context.md").read_text() == first


def test_refresh_leaves_stable_sections_untouched(repo, home):
    init(repo, home)
    ctx = repo / ".cohort" / "project_context.md"
    edited = ctx.read_text().replace("_What this project is and why it exists._", "Our actual purpose.")
    ctx.write_text(edited, encoding="utf-8")
    run_cli("snapshot", repo=repo, home=home)
    run_cli("context", "refresh", repo=repo, home=home)
    assert "Our actual purpose." in ctx.read_text()  # stable section survives refresh


def test_refresh_skips_user_edited_managed_block(repo, home):
    init(repo, home)
    run_cli("snapshot", repo=repo, home=home)
    run_cli("context", "refresh", repo=repo, home=home)
    ctx = repo / ".cohort" / "project_context.md"
    inner = extract_block(ctx.read_text())
    ctx.write_text(ctx.read_text().replace(inner, inner + "\nUSER EDIT INSIDE"), encoding="utf-8")
    run_cli("snapshot", repo=repo, home=home)
    proc = run_cli("context", "refresh", repo=repo, home=home)
    assert "USER EDIT INSIDE" in ctx.read_text()  # divergence: not overwritten
    assert "warning" in proc.stderr.lower()
    assert "--force" in proc.stderr  # the warning names the restore path


def test_refresh_force_restores_removed_block(repo, home):
    init(repo, home)
    run_cli("snapshot", repo=repo, home=home)
    run_cli("context", "refresh", repo=repo, home=home)
    ctx = repo / ".cohort" / "project_context.md"
    # user removes the managed block entirely
    from cohort.merge import BLOCK_END
    text = ctx.read_text()
    start = text.index(BLOCK_BEGIN)
    end = text.index(BLOCK_END) + len(BLOCK_END)
    ctx.write_text(text[:start] + text[end:], encoding="utf-8")
    assert BLOCK_BEGIN not in ctx.read_text()
    # plain refresh respects the removal (won't re-add)
    run_cli("context", "refresh", repo=repo, home=home)
    assert BLOCK_BEGIN not in ctx.read_text()
    # --force restores it
    assert run_cli("context", "refresh", "--force", repo=repo, home=home).returncode == 0
    assert BLOCK_BEGIN in ctx.read_text()


def test_init_force_restores_removed_import_wiring(repo, home):
    init(repo, home)
    claude_md = repo / ".claude" / "CLAUDE.md"
    claude_md.write_text("# just my stuff\n", encoding="utf-8")  # user wiped the @import block
    # plain re-init respects the removal + warns toward --force
    proc = init(repo, home)
    assert "@import" not in claude_md.read_text()
    assert "--force" in proc.stderr
    # --force restores the wiring without nuking user content
    assert init(repo, home, "--force").returncode == 0
    restored = claude_md.read_text()
    assert "@import ../.cohort/project_context.md" in restored
    assert "just my stuff" in restored


# === P4-T3: snapshot, conflict-free sessions ================================


def test_snapshot_writes_one_file_and_leaves_context(repo, home):
    init(repo, home)
    ctx_before = (repo / ".cohort" / "project_context.md").read_text()
    proc = run_cli("snapshot", repo=repo, home=home)
    assert proc.returncode == 0
    sessions = list((repo / ".cohort" / "sessions").glob("*.md"))
    assert len(sessions) == 1
    # snapshot does not modify project_context.md
    assert (repo / ".cohort" / "project_context.md").read_text() == ctx_before


def test_snapshot_filename_pattern_and_content(repo, home):
    init(repo, home)
    run_cli("snapshot", repo=repo, home=home)
    f = next((repo / ".cohort" / "sessions").glob("*.md"))
    assert f.name[8] == "T" and f.name.endswith(".md")  # YYYYMMDDThhmmssZ-<id>.md
    fm = load_artifact(f).frontmatter
    assert {"timestamp", "author", "branch"} <= set(fm)


def test_snapshot_dry_run_writes_nothing(repo, home):
    init(repo, home)
    proc = run_cli("snapshot", "--dry-run", repo=repo, home=home)
    assert proc.returncode == 0
    assert list((repo / ".cohort" / "sessions").glob("*.md")) == []


def test_snapshot_refresh_index_updates_context(repo, home):
    init(repo, home)
    proc = run_cli("snapshot", "--refresh-index", repo=repo, home=home)
    assert proc.returncode == 0
    f = next((repo / ".cohort" / "sessions").glob("*.md"))
    assert f.name in (repo / ".cohort" / "project_context.md").read_text()


def test_concurrent_snapshots_merge_without_conflict(repo, home):
    init(repo, home)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "cohort init"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    def snap_on(branch):
        subprocess.run(["git", "checkout", "-q", "-b", branch, base], cwd=repo, check=True)
        run_cli("snapshot", repo=repo, home=home)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", f"snap {branch}"], cwd=repo, check=True)

    snap_on("dev-a")
    snap_on("dev-b")
    subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
    subprocess.run(["git", "merge", "-q", "--no-edit", "dev-a"], cwd=repo, check=True)
    merge = subprocess.run(
        ["git", "merge", "--no-edit", "dev-b"], cwd=repo, capture_output=True, text=True
    )
    assert merge.returncode == 0, merge.stdout + merge.stderr  # zero conflict
    assert len(list((repo / ".cohort" / "sessions").glob("*.md"))) == 2  # both entries


# === P4-T4: staleness hook =================================================


def test_staleness_warns_when_stale(repo, home):
    init(repo, home)
    paths = CohortPaths.for_project(repo)
    old = (project._utc_now().timestamp()) - 100 * 3600
    os.utime(paths.cohort_home / "project_context.md", (old, old))
    msg = project.staleness_check(repo)
    assert msg is not None and "stale" in msg


def test_staleness_silent_when_fresh(repo, home):
    init(repo, home)
    assert project.staleness_check(repo) is None  # just created → fresh


def test_staleness_throttled_per_day(repo, home):
    init(repo, home)
    paths = CohortPaths.for_project(repo)
    old = project._utc_now().timestamp() - 100 * 3600
    os.utime(paths.cohort_home / "project_context.md", (old, old))
    assert project.staleness_check(repo) is not None  # first warns
    assert project.staleness_check(repo) is None  # throttled same day
    # backdate the marker → warns again
    (paths.state / ".staleness-warned").write_text("2000-01-01", encoding="utf-8")
    assert project.staleness_check(repo) is not None


def test_staleness_honors_config(repo, home):
    init(repo, home)
    paths = CohortPaths.for_project(repo)
    (paths.cohort_home / "cohort.toml").write_text("staleness_hours = 100000\n", encoding="utf-8")
    old = project._utc_now().timestamp() - 100 * 3600
    os.utime(paths.cohort_home / "project_context.md", (old, old))
    assert project.staleness_check(repo) is None  # threshold huge → not stale


def test_staleness_noop_outside_cohort_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert project.staleness_check(plain) is None


def test_staleness_hook_invokes_cli_not_a_script():
    r = load_artifact(COHORT_SRC / "canonical" / "hooks" / "staleness-warn.md")
    event, entry = render_hook_entry(build_ir(r.frontmatter, r.body))
    assert event == "SessionStart"
    assert entry["hooks"][0]["command"] == "cohort staleness-check"  # CLI, not a script

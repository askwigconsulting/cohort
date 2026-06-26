"""Phase 2: the explicit ``cohort update`` command — ff-only pull, conditional
pip reinstall, and recompile of the installed IDEs. Refuses dirty/diverged trees."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.update import _recompile_installed, do_update

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {name}")


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repo (with a canonical/ dir so it reads as a source root) and a
    fresh clone whose ``origin`` points at it. Local remotes need no network."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@e.st")
    _git(up, "config", "user.name", "T")
    (up / "canonical").mkdir()
    _commit(up, "canonical/x.md", "x\n")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(up), str(src))
    _git(src, "config", "user.email", "t@e.st")
    _git(src, "config", "user.name", "T")
    return up, src


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _no_pip(args: list) -> int:
    raise AssertionError(f"pip must not run here: {args}")


def test_update_up_to_date_is_a_noop(tmp_path):
    _, src = _make_upstream_and_clone(tmp_path)
    res = do_update(src, tmp_path / "home", pip_run=_no_pip)
    assert res.status == "up_to_date" and res.behind == 0


def test_update_dry_run_previews_without_changing(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    _commit(up, "b.txt", "2\n")
    before = _head(src)
    res = do_update(src, tmp_path / "home", dry_run=True, pip_run=_no_pip)
    assert res.status == "dry_run" and res.behind == 2
    assert res.commits and res.changed_files  # summary was built
    assert _head(src) == before  # nothing pulled


def test_update_clean_pull_advances_head(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    res = do_update(src, tmp_path / "home", pip_run=_no_pip)
    assert res.status == "updated"
    assert res.recompiled_ides == []  # no install manifest → nothing to recompile
    assert _head(src) == _head(up)  # fast-forwarded onto the upstream tip


def test_update_refuses_dirty_tree(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    (src / "canonical" / "x.md").write_text("uncommitted edit\n", encoding="utf-8")
    before = _head(src)
    res = do_update(src, tmp_path / "home", pip_run=_no_pip)
    assert res.status == "dirty" and _head(src) == before


def test_update_refuses_diverged_history(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "u.txt", "u\n")  # upstream advances
    _commit(src, "l.txt", "l\n")  # and we have our own commit
    res = do_update(src, tmp_path / "home", pip_run=_no_pip)
    assert res.status == "diverged"


def test_update_unavailable_when_no_remote(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "f.txt", "1\n")
    res = do_update(repo, tmp_path / "home", pip_run=_no_pip)
    assert res.status == "unavailable"


def test_update_reinstalls_package_only_when_pyproject_changes(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "pyproject.toml", "[build-system]\n")
    calls: list = []
    res = do_update(src, tmp_path / "home", pip_run=lambda a: calls.append(a) or 0)
    assert res.status == "updated" and res.pip_reinstalled
    assert len(calls) == 1 and calls[0][-2:] == ["-e", str(src)]


def test_update_skips_pip_without_pyproject_change(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "README.md", "hi\n")
    res = do_update(src, tmp_path / "home", pip_run=_no_pip)  # asserts pip not run
    assert res.status == "updated" and res.pip_reinstalled is False


def test_update_pip_failure_surfaces_and_does_not_recompile(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "pyproject.toml", "[build-system]\n")
    res = do_update(src, tmp_path / "home", pip_run=lambda a: 1)  # non-zero → failure
    assert res.status == "pip_failed" and res.recompiled_ides == []


def test_recompile_installed_recompiles_manifest_ides(tmp_path):
    """With an install on disk, recompile re-places that IDE's artifacts (incl. the
    new /update command)."""
    from cohort.install import do_install

    home = tmp_path / "home"
    home.mkdir()
    do_install(home=home, selection=["claude"], mode="copy", force=False, source=REPO_ROOT, dry_run=False)
    ides, refused = _recompile_installed(REPO_ROOT, home)
    assert ides == ["claude"] and refused is None
    assert (home / ".claude" / "commands" / "update.md").exists()


def test_recompile_installed_is_noop_without_manifest(tmp_path):
    ides, refused = _recompile_installed(REPO_ROOT, tmp_path / "home")
    assert ides == [] and refused is None


def test_recompile_refused_returns_guidance_not_force(tmp_path, monkeypatch):
    """A foreign file at a managed dest must surface as guidance — update never
    silently --forces over a user's file."""
    from types import SimpleNamespace

    import cohort.install as inst
    from cohort.executor import ClobberRefused
    from cohort.install import do_install

    home = tmp_path / "home"
    home.mkdir()
    do_install(home=home, selection=["claude"], mode="copy", force=False, source=REPO_ROOT, dry_run=False)

    clobber = SimpleNamespace(op=SimpleNamespace(dest=str(home / ".claude" / "x.md")))

    def boom(**kwargs):
        raise ClobberRefused([clobber])

    monkeypatch.setattr(inst, "do_install", boom)
    ides, refused = _recompile_installed(REPO_ROOT, home)
    assert ides == ["claude"] and refused and "overwrite" in refused


def test_update_command_renders_for_claude_and_cursor_not_codex():
    from cohort.compile import compile_ide

    claude = [sf.staged_rel for sf in compile_ide(REPO_ROOT, "claude").staged]
    cursor = [sf.staged_rel for sf in compile_ide(REPO_ROOT, "cursor").staged]
    codex = [sf.staged_rel for sf in compile_ide(REPO_ROOT, "codex").staged]
    assert "commands/update.md" in claude
    assert ".cursor/commands/update.md" in cursor
    assert "commands/update.md" not in codex  # declared command parity gap


def test_update_cli_dry_run_exits_0_with_preview(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["COHORT_SOURCE"] = str(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cohort", "update", "--dry-run"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0 and "Would update" in proc.stdout


def test_update_cli_dirty_tree_exits_1(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    (src / "canonical" / "x.md").write_text("uncommitted\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["COHORT_SOURCE"] = str(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cohort", "update"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1

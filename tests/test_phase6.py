"""Phase 6: project specialists, the isolation boundary, and promote."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


def tree_hash(root: Path) -> str:
    if not root.exists():
        return "MISSING"
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
        elif p.is_symlink():
            h.update(os.readlink(p).encode())
    return h.hexdigest()


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


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
    run_cli("recompile", "--ide", "claude", "--source", str(tmp_path / "src"), home=h)
    return h


def inited_repo(tmp_path, source, home, name="repo") -> Path:
    repo = make_git_repo(tmp_path / name)
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    return repo


def add_specialist(repo, home, name="data-modeler", **kw):
    return run_cli(
        "add-specialist", "--name", name, "--display-name", kw.get("display", name.title()),
        "--department", kw.get("dept", "Data"), "--description", kw.get("desc", "x."),
        home=home, cwd=repo,
    )


# === P6-T1: add-specialist + project-scope compilation ======================


def test_add_specialist_requires_init(tmp_path, source, home):
    repo = make_git_repo(tmp_path / "uninited")
    proc = add_specialist(repo, home)
    assert proc.returncode == 1
    assert "cohort init" in proc.stderr
    assert not (repo / ".cohort").exists()


def test_add_specialist_scaffolds_and_compiles(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    assert add_specialist(repo, home).returncode == 0
    canonical = repo / ".cohort" / "agents" / "data-modeler.md"
    assert "scope: project" in canonical.read_text()
    assert "topology: specialist" in canonical.read_text()  # never a project generalist
    assert (repo / ".claude" / "agents" / "data-modeler.md").exists()  # compiled in


def test_add_specialist_emits_only_project_ops(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    before_global_claude = tree_hash(home / ".claude" / "agents")
    before_global_canon = tree_hash(source / "canonical" / "agents")
    add_specialist(repo, home)
    assert tree_hash(home / ".claude" / "agents") == before_global_claude  # global untouched
    assert tree_hash(source / "canonical" / "agents") == before_global_canon


def test_add_specialist_collision_refused(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    proc = add_specialist(repo, home)  # same name again
    assert proc.returncode == 1
    assert "already exists" in proc.stderr


def test_specialist_marker_is_compile_error(tmp_path, source, home):
    from cohort.install_model import CohortPaths
    from cohort.specialists import AddSpecialistError, compile_specialists
    repo = inited_repo(tmp_path, source, home)
    bad = repo / ".cohort" / "agents" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "---\nname: bad\nkind: agent\nscope: project\ndescription: x\ntargets: [all]\n"
        "department: D\ntopology: specialist\nadvisory: true\ntools: [read]\n---\n"
        "body\n<!-- cohort:office-directory -->\n", encoding="utf-8",
    )
    with pytest.raises(AddSpecialistError):
        compile_specialists(CohortPaths.for_project(repo))


def test_add_specialist_shadow_warns(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    proc = add_specialist(repo, home, name="counsel")  # shadows a global agent
    assert proc.returncode == 0
    assert "precedence" in proc.stderr.lower()


def test_add_specialist_dry_run_writes_nothing(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    proc = run_cli("add-specialist", "--name", "data-modeler", "--display-name", "DM",
                   "--department", "Data", "--description", "x", "--dry-run", home=home, cwd=repo)
    assert proc.returncode == 0
    assert not (repo / ".cohort" / "agents").exists()


def test_specialist_compile_byte_stable(tmp_path, source, home):
    from cohort.compile import staging_tree_hash
    from cohort.install_model import CohortPaths
    from cohort.specialists import compile_specialists
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    paths = CohortPaths.for_project(repo)
    compile_specialists(paths)
    h1 = staging_tree_hash(paths, "claude")
    compile_specialists(paths)
    assert staging_tree_hash(paths, "claude") == h1


# === P6-T2: isolation boundary + status + deinit ============================


def test_isolation_specialist_in_a_invisible_to_b(tmp_path, source, home):
    repo_a = inited_repo(tmp_path, source, home, "repo-a")
    repo_b = inited_repo(tmp_path, source, home, "repo-b")
    add_specialist(repo_a, home, name="a-only")
    assert (repo_a / ".claude" / "agents" / "a-only.md").exists()
    # invisible to repo B and to the global roster
    assert not (repo_b / ".claude" / "agents" / "a-only.md").exists()
    assert not (home / ".claude" / "agents" / "a-only.md").exists()
    assert not (repo_b / ".cohort" / "agents" / "a-only.md").exists()


def test_status_groups_and_flags_shadow(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home, name="counsel")  # shadow
    add_specialist(repo, home, name="data-modeler")
    data = json.loads(run_cli("status", "--json", home=home, cwd=repo).stdout)
    assert data["global"]["roster"]["count"] == 15  # separate group
    assert set(data["project"]["specialists"]) == {"counsel", "data-modeler"}
    assert data["project"]["shadowed"] == ["counsel"]


def test_deinit_removes_compiled_preserves_sources_keeps_global(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    before_global = tree_hash(home / ".claude" / "agents")
    assert run_cli("deinit", home=home, cwd=repo).returncode == 0
    assert not (repo / ".claude" / "agents" / "data-modeler.md").exists()  # compiled removed
    assert (repo / ".cohort" / "agents" / "data-modeler.md").exists()  # source preserved
    assert tree_hash(home / ".claude" / "agents") == before_global  # global untouched


def test_deinit_purge_removes_specialist_sources(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    run_cli("deinit", "--purge", home=home, cwd=repo)
    assert not (repo / ".cohort").exists()


# === P6-T3: promote (proposal-gated, no silent copy) ========================


def test_promote_stages_proposal(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    assert run_cli("promote", "data-modeler", home=home, cwd=repo).returncode == 0
    proposal = repo / ".cohort" / "proposals" / "data-modeler.md"
    assert proposal.exists()
    fm = proposal.read_text()
    assert "target: global" in fm and "name: data-modeler" in fm and "requested_at:" in fm


def test_promote_never_writes_global(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    before_canon = tree_hash(source / "canonical")
    before_global = tree_hash(home / ".cohort" / "canonical")
    run_cli("promote", "data-modeler", home=home, cwd=repo)
    assert tree_hash(source / "canonical") == before_canon  # source untouched
    assert tree_hash(home / ".cohort" / "canonical") == before_global


def test_promote_invalid_specialist_errors(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    proc = run_cli("promote", "nonexistent", home=home, cwd=repo)
    assert proc.returncode == 1
    assert not (repo / ".cohort" / "proposals").exists()  # no proposal written


def test_promote_dry_run_writes_nothing(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    proc = run_cli("promote", "data-modeler", "--dry-run", home=home, cwd=repo)
    assert proc.returncode == 0
    assert not (repo / ".cohort" / "proposals").exists()


def test_proposals_is_git_tracked(tmp_path, source, home):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    run_cli("promote", "data-modeler", home=home, cwd=repo)
    ignored = subprocess.run(["git", "check-ignore", "-q", ".cohort/proposals"], cwd=repo)
    assert ignored.returncode == 1  # not ignored

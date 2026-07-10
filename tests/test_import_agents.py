"""`cohort adopt` importer: bring pre-existing native Claude agents into the
office. `--to project` PRESERVES a write-capable source as a real doer (tools
kept); `--to my` forces advisory (a synced tier) and flags the downgrade. Bulk
directories, --advisory-only, dry-run, and fail-closed on a non-project repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort.adopt import AdoptError, do_import_agents
from cohort.install_model import CohortPaths
from cohort.manifest import Manifest
from cohort.loader import load_artifact

from conftest import requires_symlinks  # noqa: E402

HOME = "home"


def _project(repo: Path) -> CohortPaths:
    ppaths = CohortPaths.for_project(repo)
    ppaths.state.mkdir(parents=True)
    Manifest(install_id="proj00000001", created_at="2026-01-01T00:00:00+00:00",
             mode="link", ides=["project"]).persist(ppaths.manifest)
    return ppaths


def _native(repo: Path, name: str, tools_line: str, desc: str = "A native agent.") -> Path:
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    f.write_text(f"---\nname: {name}\ndescription: {desc}\n{tools_line}---\nBody of {name}.\n",
                 encoding="utf-8")
    return f


def _canon_fm(repo: Path, name: str) -> dict:
    src = repo / ".cohort" / "canonical" / "agents" / f"{name}.md"
    return load_artifact(src).frontmatter or {}


@requires_symlinks
def test_project_import_preserves_a_doer(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    f = _native(repo, "deployer", "tools: Read, Edit, Bash\n")
    report = do_import_agents(home, tmp_path, f, to="project", department="Ops", repo=repo)

    assert report["imported"] == [{"name": "deployer", "as_doer": True}]
    fm = _canon_fm(repo, "deployer")
    assert fm["advisory"] is False and fm["scope"] == "project"
    assert "edit" in fm["tools"] and "bash" in fm["tools"]
    placed = (repo / ".claude" / "agents" / "deployer.md").read_text(encoding="utf-8")
    assert "Edit" in placed and "Bash" in placed  # write tools survived to the placement
    assert any(d["name"] == "deployer" and d["bash"] for d in report["doers"])


@requires_symlinks
def test_project_import_of_a_readonly_agent_is_advisory(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    f = _native(repo, "reviewer", "tools: [Read, Grep]\n")
    report = do_import_agents(home, tmp_path, f, to="project", department="Eng", repo=repo)
    assert report["imported"] == [{"name": "reviewer", "as_doer": False}]
    assert _canon_fm(repo, "reviewer")["advisory"] is True
    assert report["doers"] == []


@requires_symlinks
def test_bulk_directory_import(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    _native(repo, "deployer", "tools: Read, Bash\n")
    _native(repo, "reviewer", "tools: [Read, Grep]\n")
    report = do_import_agents(
        home, tmp_path, repo / ".claude" / "agents", to="project", department="X", repo=repo)
    names = {a["name"]: a["as_doer"] for a in report["imported"]}
    assert names == {"deployer": True, "reviewer": False}
    assert (repo / ".claude" / "agents" / "deployer.md").exists()


@requires_symlinks
def test_advisory_only_skips_doers(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    _native(repo, "deployer", "tools: Read, Bash\n")
    _native(repo, "reviewer", "tools: [Read, Grep]\n")
    report = do_import_agents(
        home, tmp_path, repo / ".claude" / "agents", to="project",
        department="X", advisory_only=True, repo=repo)
    assert [a["name"] for a in report["imported"]] == ["reviewer"]
    assert report["skipped"] and report["skipped"][0]["name"] == "deployer"
    assert not (repo / ".cohort" / "canonical" / "agents" / "deployer.md").exists()


@requires_symlinks
def test_no_tools_source_is_imported_advisory(tmp_path):
    # an implicit all-tools grant (no `tools` key) is NOT treated as a doer — we
    # can't infer a safe write set, so it comes in read-only.
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    f = _native(repo, "helper", "")  # no tools line
    report = do_import_agents(home, tmp_path, f, to="project", department="X", repo=repo)
    assert report["imported"][0]["as_doer"] is False
    assert _canon_fm(repo, "helper")["advisory"] is True


def test_dry_run_changes_nothing(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    f = _native(repo, "deployer", "tools: Read, Bash\n")
    report = do_import_agents(
        home, tmp_path, f, to="project", department="X", dry_run=True, repo=repo)
    assert report["dry_run"] is True and report["imported"][0]["as_doer"] is True
    assert not (repo / ".cohort" / "canonical" / "agents" / "deployer.md").exists()
    assert f.exists() and not f.is_symlink()  # source untouched


def test_import_to_project_refuses_non_project(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    f = _native(repo, "deployer", "tools: Read, Bash\n")  # no cohort init
    with pytest.raises(AdoptError, match="not a Cohort project"):
        do_import_agents(home, tmp_path, f, to="project", department="X", repo=repo)


@requires_symlinks
def test_project_import_maps_concrete_model_to_tier(tmp_path):
    # a concrete model name found in the wild (#143) maps to its nearest tier
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "reviewer.md"
    f.write_text(
        "---\nname: reviewer\ndescription: A native agent.\nmodel: claude-opus-4\n"
        "tools: [Read, Grep]\n---\nBody.\n",
        encoding="utf-8",
    )
    do_import_agents(home, tmp_path, f, to="project", department="Eng", repo=repo)
    assert _canon_fm(repo, "reviewer")["model"] == "top"


@requires_symlinks
def test_project_import_drops_unrecognized_model(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / HOME
    _project(repo)
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "reviewer.md"
    f.write_text(
        "---\nname: reviewer\ndescription: A native agent.\nmodel: gpt-5\n"
        "tools: [Read, Grep]\n---\nBody.\n",
        encoding="utf-8",
    )
    do_import_agents(home, tmp_path, f, to="project", department="Eng", repo=repo)
    assert "model" not in _canon_fm(repo, "reviewer")  # dropped, never guessed


def test_import_to_my_downgrades_a_doer(tmp_path):
    # --to my is a synced tier → advisory-only; a doer source is flagged as
    # downgraded. Dry-run so we don't need a full global recompile.
    repo, home = tmp_path / "repo", tmp_path / HOME
    f = _native(repo, "deployer", "tools: Read, Edit, Bash\n")
    report = do_import_agents(home, tmp_path, f, to="my", dry_run=True)
    assert report["to"] == "my"
    assert report["doers_downgraded"] == ["deployer"]

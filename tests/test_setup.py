"""`cohort setup` — the wiring interview: roster subsets, company upstream, stale cleanup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None, stdin=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True,
        env=env, input=stdin, timeout=120,
    )


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    return src


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def manifest(home):
    return json.loads((home / ".cohort" / "state" / "manifest.json").read_text())


def placed_agents(home):
    d = home / ".claude" / "agents"
    return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


# === roster subset ===========================================================


def test_setup_subset_installs_only_selected_agents(home, source):
    proc = run_cli(
        "setup", "--ide", "claude", "--agents", "counsel,security-engineer,chief-of-staff",
        "--source", str(source), home=home,
    )
    assert proc.returncode == 0
    assert placed_agents(home) == ["chief-of-staff", "counsel", "security-engineer"]
    assert manifest(home)["roster"] == ["counsel", "security-engineer", "chief-of-staff"]


def test_setup_subset_shrinks_office_directory(home, source):
    run_cli("setup", "--ide", "claude", "--agents", "counsel,chief-of-staff",
            "--source", str(source), home=home)
    chief = (home / ".claude" / "agents" / "chief-of-staff.md").read_text(encoding="utf-8")
    assert "counsel" in chief.lower()
    assert "hr-partner" not in chief.lower()  # excluded agent absent from the directory


def test_setup_unknown_agent_errors(home, source):
    proc = run_cli("setup", "--ide", "claude", "--agents", "counsel,not-an-agent",
                   "--source", str(source), home=home)
    assert proc.returncode == 2
    assert "not-an-agent" in proc.stderr
    assert not (home / ".claude" / "agents").exists()  # nothing placed


def test_setup_without_chief_warns(home, source):
    proc = run_cli("setup", "--ide", "claude", "--agents", "counsel",
                   "--source", str(source), home=home)
    assert proc.returncode == 0
    assert "chief-of-staff" in proc.stderr


def test_recompile_honors_persisted_roster(home, source):
    run_cli("setup", "--ide", "claude", "--agents", "counsel,chief-of-staff",
            "--source", str(source), home=home)
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 0
    assert placed_agents(home) == ["chief-of-staff", "counsel"]  # subset survived


def test_recompile_agents_all_restores_full_roster(home, source):
    run_cli("setup", "--ide", "claude", "--agents", "counsel,chief-of-staff",
            "--source", str(source), home=home)
    proc = run_cli("recompile", "--ide", "claude", "--agents", "all",
                   "--source", str(source), home=home)
    assert proc.returncode == 0
    assert len(placed_agents(home)) == 15
    assert "roster" not in manifest(home)


def test_shrinking_roster_removes_stale_placed_agents(home, source):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert len(placed_agents(home)) == 15
    proc = run_cli("recompile", "--ide", "claude", "--agents", "counsel,chief-of-staff",
                   "--source", str(source), home=home)
    assert proc.returncode == 0
    assert placed_agents(home) == ["chief-of-staff", "counsel"]  # no dangling links
    dests = [op["dest"] for op in manifest(home)["ops"]]
    assert not any("hr-partner" in d for d in dests)  # manifest records pruned too


def test_setup_dry_run_writes_nothing(home, source):
    proc = run_cli("setup", "--ide", "claude", "--agents", "counsel", "--dry-run",
                   "--source", str(source), home=home)
    assert proc.returncode == 0
    assert not (home / ".claude").exists()
    assert not (home / ".cohort").exists()


def test_setup_non_interactive_defaults_to_full_roster(home, source):
    proc = run_cli("setup", "--non-interactive", "--ide", "claude",
                   "--source", str(source), home=home)
    assert proc.returncode == 0
    assert len(placed_agents(home)) == 15
    assert "roster" not in manifest(home)


# === company office wiring ===================================================


def test_setup_company_url_wires_remote_and_config(home, source):
    company = source.parent / "company-cohort"
    company.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=company, check=True)
    proc = run_cli(
        "setup", "--ide", "claude", "--company-url", str(company),
        "--source", str(source), home=home,
    )
    assert proc.returncode == 0
    remotes = subprocess.run(["git", "remote", "get-url", "company"], cwd=source,
                             capture_output=True, text=True)
    assert remotes.stdout.strip() == str(company)
    cfg = (home / ".cohort" / "cohort.toml").read_text(encoding="utf-8")
    assert 'upstream_remote = "company"' in cfg


def test_setup_company_url_rewires_existing_remote(home, source):
    for url in ("https://example.com/old.git", "https://example.com/new.git"):
        proc = run_cli("setup", "--ide", "claude", "--company-url", url,
                       "--source", str(source), home=home)
        assert proc.returncode == 0
    remotes = subprocess.run(["git", "remote", "get-url", "company"], cwd=source,
                             capture_output=True, text=True)
    assert remotes.stdout.strip() == "https://example.com/new.git"


def test_setup_company_branch_lands_in_config(home, source):
    run_cli("setup", "--ide", "claude", "--company-url", "https://example.com/co.git",
            "--company-branch", "trunk", "--source", str(source), home=home)
    cfg = (home / ".cohort" / "cohort.toml").read_text(encoding="utf-8")
    assert 'upstream_branch = "trunk"' in cfg


def test_setup_option_shaped_company_url_refused(home, source):
    proc = run_cli("setup", "--ide", "claude", "--company-url", "--upload-pack=evil",
                   "--source", str(source), home=home)
    assert proc.returncode == 2
    assert not (home / ".cohort" / "cohort.toml").exists()


# === interview (stdin) =======================================================


def test_setup_prompts_are_flag_skippable(home, source):
    # No TTY in CI: flagged runs must never block on stdin.
    proc = run_cli("setup", "--non-interactive", "--ide", "claude",
                   "--source", str(source), home=home, stdin="")
    assert proc.returncode == 0


# === add-specialist --body-file ==============================================


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_add_specialist_body_file_replaces_template(home, source, tmp_path):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    repo = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    body = tmp_path / "body.md"
    body.write_text(
        "**Role.** Owns the ETL schema.\n\n**Advises on.** Airflow DAGs, dbt models.\n",
        encoding="utf-8",
    )
    proc = run_cli(
        "add-specialist", "--name", "etl-advisor", "--display-name", "ETLAdvisor",
        "--department", "Data", "--description", "ETL guidance.",
        "--body-file", str(body), home=home, cwd=repo,
    )
    assert proc.returncode == 0
    content = (repo / ".cohort" / "agents" / "etl-advisor.md").read_text(encoding="utf-8")
    assert "Airflow DAGs" in content
    assert "edit me" not in content
    assert "scope: project" in content  # frontmatter still generated, not user-supplied


def test_add_specialist_missing_body_file_errors(home, source, tmp_path):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    repo = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    proc = run_cli(
        "add-specialist", "--name", "x", "--body-file", str(tmp_path / "nope.md"),
        home=home, cwd=repo,
    )
    assert proc.returncode == 2
    assert not (repo / ".cohort" / "agents" / "x.md").exists()


# === interview commands (canonical) ==========================================


def test_interview_commands_compile_into_claude(home, source):
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 0
    for command in ("office-setup", "project-setup"):
        placed = home / ".claude" / "commands" / f"{command}.md"
        assert placed.exists(), command


def test_interview_commands_survive_roster_subset(home, source):
    run_cli("setup", "--ide", "claude", "--agents", "chief-of-staff",
            "--source", str(source), home=home)
    assert (home / ".claude" / "commands" / "office-setup.md").exists()
    assert (home / ".claude" / "commands" / "project-setup.md").exists()

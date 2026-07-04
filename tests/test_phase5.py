"""Phase 5: add-agent, status, reporting.

Behavioral/integration tests drive the real CLI; report goldens regenerate with
COHORT_REGEN=1 and are asserted otherwise. add-agent always runs against a temp
source copy so the real roster is never mutated (R3).
"""

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
GOLDEN_REPORTS = COHORT_SRC / "tests" / "golden" / "reports"


def run_cli(*args, home, cwd=None, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
    env.pop("COHORT_SOURCE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def source(tmp_path):
    """A temp copy of the real roster source for add-agent (never the real tree)."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    return src


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


# === P5-T1: add-agent =======================================================


def test_add_agent_scaffolds_and_installs(source, home):
    proc = run_cli(
        "add-agent", "--name", "devrel", "--display-name", "DevRel",
        "--department", "Marketing", "--description", "Dev relations.",
        "--source", str(source), home=home,
    )
    assert proc.returncode == 0, proc.stderr
    art = home / ".cohort" / "my" / "canonical" / "agents" / "devrel.md"
    assert art.exists()  # my office by default (#84) — the clone stays clean
    assert "advisory: true" in art.read_text()
    assert not (source / "canonical" / "agents" / "devrel.md").exists()
    assert (home / ".claude" / "agents" / "devrel.md").exists()  # recompiled in


def test_add_specialist_appears_in_chief_directory(source, home):
    run_cli("add-agent", "--name", "devrel", "--display-name", "DevRel",
            "--department", "Marketing", "--description", "Dev relations.",
            "--source", str(source), home=home)
    chief = (home / ".claude" / "agents" / "chief-of-staff.md").read_text()
    assert "**DevRel**" in chief  # Phase-3 injection auto-wired it


def test_add_agent_real_roster_untouched(source, home):
    before = len(list((COHORT_SRC / "canonical" / "agents").glob("*.md")))
    run_cli("add-agent", "--name", "devrel", "--display-name", "DevRel",
            "--department", "Marketing", "--description", "x", "--source", str(source), home=home)
    assert len(list((COHORT_SRC / "canonical" / "agents").glob("*.md"))) == before


def test_add_agent_collision_refused(source, home):
    proc = run_cli("add-agent", "--name", "counsel", "--display-name", "X",
                   "--department", "Y", "--description", "z", "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "already exists" in proc.stderr


def test_add_agent_second_generalist_refused(source, home):
    proc = run_cli("add-agent", "--name", "boss", "--display-name", "Boss", "--department", "Exec",
                   "--topology", "generalist", "--description", "z", "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "generalist" in proc.stderr


def test_add_agent_dry_run_writes_nothing(source, home):
    proc = run_cli("add-agent", "--name", "devrel", "--display-name", "DevRel",
                   "--department", "Marketing", "--description", "x",
                   "--source", str(source), "--dry-run", home=home)
    assert proc.returncode == 0
    assert not (source / "canonical" / "agents" / "devrel.md").exists()


def test_add_agent_interactive(source, home, monkeypatch):
    from cohort import roster
    inputs = {"name": "devrel", "display_name": "DevRel", "department": "Marketing",
              "topology": "specialist", "description": "Dev relations."}
    monkeypatch.setattr(roster, "prompt_add_agent_inputs", lambda: inputs)
    # call the layer directly (interactive path is patched)
    report = roster.do_add_agent(source, home, **inputs, dry_run=True)
    assert report["name"] == "devrel"


# === P5-T2: status ==========================================================


def _install_global(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_status_global_outside_repo(source, home, tmp_path):
    _install_global(source, home)
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = run_cli("status", "--json", home=home, cwd=plain)
    data = json.loads(proc.stdout)
    assert data["global"]["roster"]["count"] == 19
    assert "claude" in data["global"]["ides"]
    assert "project" not in data  # outside a cohort repo


def test_status_project_section(source, home, tmp_path):
    _install_global(source, home)
    repo = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    proc = run_cli("status", "--json", home=home, cwd=repo)
    data = json.loads(proc.stdout)
    assert data["project"]["wiring"]["state"] == "present"
    assert data["project"]["staleness"]["stale"] is False  # just inited


def test_status_reports_missing_wiring_with_restore_hint(source, home, tmp_path):
    _install_global(source, home)
    repo = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    (repo / ".claude" / "CLAUDE.md").write_text("# mine only\n", encoding="utf-8")  # wiring removed
    data = json.loads(run_cli("status", "--json", home=home, cwd=repo).stdout)
    assert data["project"]["wiring"]["state"] == "missing"
    assert data["project"]["wiring"]["restore"] == "cohort init --force"


def test_status_never_writes(source, home, tmp_path):
    _install_global(source, home)
    repo = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    # make it stale so the staleness path is exercised
    paths_state = repo / ".cohort" / "state"
    before_home, before_repo = tree_hash(home), tree_hash(repo)
    run_cli("status", home=home, cwd=repo)
    run_cli("status", home=home, cwd=repo)
    assert tree_hash(home) == before_home  # no marker churn
    assert tree_hash(repo) == before_repo


# === P5-T3: reporting =======================================================


def _build_report_repo(repo: Path, source: Path, home: Path) -> None:
    """Deterministic report-repo: fixed sessions + pinned-date commits (R2)."""
    make_git_repo(repo)
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    sessions = repo / ".cohort" / "sessions"
    # two in-window sessions + one out-of-window
    entries = {
        "20260615T120000Z-aaaaaa.md": "2026-06-15T12:00:00+00:00",
        "20260618T120000Z-bbbbbb.md": "2026-06-18T12:00:00+00:00",
        "20260601T120000Z-cccccc.md": "2026-06-01T12:00:00+00:00",  # out of weekly window
    }
    for fname, ts in entries.items():
        (sessions / fname).write_text(
            f"---\ntimestamp: {ts}\nauthor: Dev <d@e.com>\nbranch: main\n---\n"
            f"## Decisions\n- decided {fname[:8]}\n\n## Open items\n- open {fname[:8]}\n",
            encoding="utf-8",
        )
    # commits with pinned dates (subjects/authors stable; dates drive the window only)
    for subject, date in [("in-window commit", "2026-06-16T12:00:00"),
                          ("out-window commit", "2026-06-01T12:00:00")]:
        (repo / f"{subject.replace(' ', '_')}.txt").write_text("x\n", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
            "GIT_AUTHOR_NAME": "Dev", "GIT_COMMITTER_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "d@e.com", "GIT_COMMITTER_EMAIL": "d@e.com",
        })
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", subject], cwd=repo, env=env, check=True)


def test_weekly_report_matches_golden(source, home, tmp_path):
    repo = tmp_path / "repo"
    _build_report_repo(repo, source, home)
    proc = run_cli("weekly-report", "--until", "2026-06-20", "--dry-run", home=home, cwd=repo)
    assert proc.returncode == 0, proc.stderr
    body = proc.stdout
    golden = GOLDEN_REPORTS / "weekly-2026-06-20.md"
    if os.environ.get("COHORT_REGEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(body, encoding="utf-8")
    assert body == golden.read_text(encoding="utf-8")
    # window correctness + R2: no SHAs/dates, only subjects
    assert "in-window commit" in body
    assert "out-window commit" not in body
    assert "decided 20260615" in body and "decided 20260601" not in body


def test_monthly_report_includes_wider_window(source, home, tmp_path):
    repo = tmp_path / "repo"
    _build_report_repo(repo, source, home)
    body = run_cli("monthly-report", "--until", "2026-06-20", "--dry-run", home=home, cwd=repo).stdout
    golden = GOLDEN_REPORTS / "monthly-2026-06-20.md"
    if os.environ.get("COHORT_REGEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(body, encoding="utf-8")
    assert body == golden.read_text(encoding="utf-8")
    assert "decided 20260601" in body  # the 30-day window includes the older session


def test_report_writes_to_reports_dir(source, home, tmp_path):
    repo = tmp_path / "repo"
    _build_report_repo(repo, source, home)
    proc = run_cli("weekly-report", "--until", "2026-06-20", home=home, cwd=repo)
    assert proc.returncode == 0
    out = repo / ".cohort" / "reports" / "weekly-2026-06-20.md"
    assert out.exists()
    # reports/ is tracked (not ignored)
    assert subprocess.run(["git", "check-ignore", "-q", ".cohort/reports"], cwd=repo).returncode == 1


def test_report_idempotent(source, home, tmp_path):
    repo = tmp_path / "repo"
    _build_report_repo(repo, source, home)
    run_cli("weekly-report", "--until", "2026-06-20", home=home, cwd=repo)
    first = (repo / ".cohort" / "reports" / "weekly-2026-06-20.md").read_bytes()
    run_cli("weekly-report", "--until", "2026-06-20", home=home, cwd=repo)
    assert (repo / ".cohort" / "reports" / "weekly-2026-06-20.md").read_bytes() == first


def test_empty_window_is_well_formed(source, home, tmp_path):
    repo = tmp_path / "repo"
    _build_report_repo(repo, source, home)
    body = run_cli("weekly-report", "--until", "2020-01-01", "--dry-run", home=home, cwd=repo).stdout
    assert "Snapshots: 0" in body and "Commits: 0" in body
    assert "_none_" in body  # zero-count sections, not an error


# === add-memory ==============================================================


def test_add_memory_scaffolds_and_lands_in_corpus(source, home):
    proc = run_cli(
        "add-memory", "--name", "review-cadence", "--description", "Reviews happen weekly.",
        "--source", str(source), home=home,
    )
    assert proc.returncode == 0, proc.stderr
    art = home / ".cohort" / "my" / "canonical" / "memories" / "review-cadence.md"
    assert art.exists()  # my office by default (#84)
    assert "scope: global" in art.read_text(encoding="utf-8")  # memories are global-only
    corpus = home / ".claude" / "cohort" / "CLAUDE.cohort.md"
    assert "review-cadence" in corpus.read_text(encoding="utf-8")  # recompiled into the corpus


def test_add_memory_collision_refused(source, home):
    proc = run_cli("add-memory", "--name", "office-routing", "--description", "x",
                   "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "already exists" in proc.stderr


def test_add_memory_body_file_replaces_template(source, home, tmp_path):
    draft = tmp_path / "m.md"
    draft.write_text("Ship on Fridays only, with sign-off.\n", encoding="utf-8")
    proc = run_cli("add-memory", "--name", "ship-window", "--description", "Ship window.",
                   "--body-file", str(draft), "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    corpus = (home / ".claude" / "cohort" / "CLAUDE.cohort.md").read_text(encoding="utf-8")
    assert "Ship on Fridays only" in corpus


def test_add_memory_bad_priority_refused(source, home):
    proc = run_cli("add-memory", "--name", "x-mem", "--description", "d",
                   "--priority", "urgent", "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "priority" in proc.stderr


def test_add_memory_real_source_untouched(source, home):
    before = sorted((COHORT_SRC / "canonical" / "memories").glob("*.md"))
    run_cli("add-memory", "--name", "scratch-mem", "--description", "d",
            "--source", str(source), home=home)
    assert sorted((COHORT_SRC / "canonical" / "memories").glob("*.md")) == before

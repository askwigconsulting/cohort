"""Phase 8: the self-improvement loop — feedback, propose, submit (human gate).

The spine is the safety boundary: the loop structurally cannot edit canonical or
auto-merge. Those two invariants are proven behaviorally (tree-hash + a recording
fake git/gh), not by static import checks.
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

from cohort import improve
from cohort.install_model import CohortPaths

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


@pytest.fixture
def repo(tmp_path, source, home):
    r = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=r)
    return r


# === P8-T1: feedback ========================================================


def test_feedback_writes_unique_file(repo, home):
    run_cli("feedback", "--rating", "down", "--agent", "counsel", "--note", "x", home=home, cwd=repo)
    run_cli("feedback", "--rating", "up", "--agent", "security-engineer", home=home, cwd=repo)
    files = list((repo / ".cohort" / "feedback").glob("*.md"))
    assert len(files) == 2  # two unique files, conflict-free
    fm = (files[0]).read_text()
    assert "rating:" in fm


def test_feedback_rating_enum_enforced(repo, home):
    proc = run_cli("feedback", "--rating", "meh", home=home, cwd=repo)
    assert proc.returncode == 1
    assert "rating" in proc.stderr


def test_feedback_git_tracked_and_dry_run(repo, home):
    run_cli("feedback", "--rating", "up", "--agent", "comms", home=home, cwd=repo)
    ignored = subprocess.run(["git", "check-ignore", "-q", ".cohort/feedback"], cwd=repo)
    assert ignored.returncode == 1  # not ignored
    proc = run_cli("feedback", "--rating", "up", "--dry-run", home=home, cwd=repo)
    assert proc.returncode == 0
    assert len(list((repo / ".cohort" / "feedback").glob("*.md"))) == 1  # dry-run wrote nothing


# === P8-T2: propose-improvement (deterministic core + seam) =================


def _seed_feedback(repo):
    fb = repo / ".cohort" / "feedback"
    fb.mkdir(parents=True, exist_ok=True)
    (fb / "20260601T120000Z-a.md").write_text(
        "---\nrating: down\nagent: counsel\ntimestamp: 2026-06-01T12:00:00+00:00\n---\nmissed\n",
        encoding="utf-8",
    )
    (fb / "20260601T120001Z-b.md").write_text(
        "---\nrating: up\nagent: counsel\ntimestamp: 2026-06-01T12:00:01+00:00\n---\ngood\n",
        encoding="utf-8",
    )
    (fb / "20260601T120002Z-c.md").write_text(
        "---\nrating: down\nagent: counsel\ntimestamp: 2026-06-01T12:00:02+00:00\n---\nbad\n",
        encoding="utf-8",
    )


def test_propose_improvement_deterministic_no_llm(repo, home):
    _seed_feedback(repo)
    # deterministic core: no enrich seam, no network
    report = improve.do_propose_improvement(repo, dry_run=True)
    body = report["body"]
    assert "kind: improvement" in body
    assert "Feedback entries: 3" in body
    assert "counsel" in body  # low-rated (2 down vs 1 up)


def test_propose_improvement_enrichment_seam(repo, home):
    _seed_feedback(repo)
    report = improve.do_propose_improvement(repo, dry_run=True, enrich=lambda ev: "ENRICHED RATIONALE")
    assert "ENRICHED RATIONALE" in report["body"]  # seam output replaces the summary


def test_propose_improvement_never_writes_canonical(repo, home, source):
    _seed_feedback(repo)
    before_src = tree_hash(source / "canonical")
    before_global = tree_hash(home / ".cohort" / "canonical")
    run_cli("propose-improvement", home=home, cwd=repo)
    assert tree_hash(source / "canonical") == before_src  # source canonical untouched
    assert tree_hash(home / ".cohort" / "canonical") == before_global
    assert list((repo / ".cohort" / "proposals").glob("improvement-*.md"))  # only proposals/ written


# === P8-T3: submit-proposals — the safety spine =============================


class RecordingRunner:
    """A fake git/gh that records every invocation (and never really runs)."""

    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return None


def _stage_proposals(repo, home, source):
    # one promotion (via promote) + one improvement
    run_cli("add-specialist", "--name", "data-modeler", "--display-name", "DM",
            "--department", "Data", "--description", "x", home=home, cwd=repo)
    run_cli("promote", "data-modeler", home=home, cwd=repo)
    _seed_feedback(repo)
    run_cli("propose-improvement", home=home, cwd=repo)


def test_submit_no_auto_edit_canonical(repo, home, source):
    _stage_proposals(repo, home, source)
    before_src = tree_hash(source / "canonical")
    before_global = tree_hash(home / ".cohort" / "canonical")
    improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(), gh_ok=True)
    assert tree_hash(source / "canonical") == before_src  # invariant 1
    assert tree_hash(home / ".cohort" / "canonical") == before_global


def test_submit_no_auto_merge_no_default_push(repo, home, source):
    _stage_proposals(repo, home, source)
    runner = RecordingRunner()
    improve.do_submit_proposals(repo, source, dry_run=False, run=runner, gh_ok=True)
    flat = [" ".join(c) for c in runner.calls]
    assert flat, "expected git/gh invocations"
    # invariant 2: never a merge; never a push to a default branch
    assert not any("pr merge" in c or "merge" in c.split() for c in flat)
    for c in flat:
        if c.startswith("git") and "push" in c:
            assert "cohort/proposal-" in c  # only feature-branch pushes
            assert " main" not in c and " master" not in c
    # invariant 3: every PR is a draft
    pr_creates = [c for c in flat if "pr create" in c]
    assert pr_creates and all("--draft" in c for c in pr_creates)


def test_submit_is_idempotent(repo, home, source):
    _stage_proposals(repo, home, source)
    improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(), gh_ok=True)
    r2 = improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(), gh_ok=True)
    assert r2["submitted"] == []  # already stamped → skipped, no duplicate PRs
    assert len(r2["skipped"]) >= 2


def test_submit_handles_both_kinds(repo, home, source):
    _stage_proposals(repo, home, source)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(repo, source, dry_run=False, run=runner, gh_ok=True)
    assert len(result["submitted"]) == 2  # promotion + improvement, one submit path


def test_submit_no_gh_degrades_cleanly(repo, home, source):
    _stage_proposals(repo, home, source)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(repo, source, dry_run=False, run=runner, gh_ok=False)
    assert result["degraded"] is True
    assert result["submitted"] == []
    assert runner.calls == []  # nothing attempted
    # proposals remain as files, unstamped
    assert not any("submitted_at" in p.read_text() for p in (repo / ".cohort" / "proposals").glob("*.md"))


def test_submit_dry_run_creates_nothing(repo, home, source):
    _stage_proposals(repo, home, source)
    runner = RecordingRunner()
    improve.do_submit_proposals(repo, source, dry_run=True, run=runner, gh_ok=True)
    assert runner.calls == []
    assert not any("submitted_at" in p.read_text() for p in (repo / ".cohort" / "proposals").glob("*.md"))


def test_promotion_proposal_carries_kind(repo, home, source):
    run_cli("add-specialist", "--name", "data-modeler", "--display-name", "DM",
            "--department", "Data", "--description", "x", home=home, cwd=repo)
    run_cli("promote", "data-modeler", home=home, cwd=repo)
    text = (repo / ".cohort" / "proposals" / "data-modeler.md").read_text()
    assert "kind: promotion" in text  # R7 retrofit


def test_submit_degrades_on_git_gh_failure(repo, home, source):
    # gh "available" but the actual push/PR fails (no push access, not a GitHub
    # remote, …) → degrade cleanly, never crash or half-submit.
    _stage_proposals(repo, home, source)

    def failing(cmd):
        raise RuntimeError("simulated git/gh failure")

    result = improve.do_submit_proposals(repo, source, dry_run=False, run=failing, gh_ok=True)
    assert result["degraded"] is True
    assert result["submitted"] == []
    # proposals remain as files, unstamped → recoverable for manual PR creation
    assert not any(
        "submitted_at" in p.read_text() for p in (repo / ".cohort" / "proposals").glob("*.md")
    )

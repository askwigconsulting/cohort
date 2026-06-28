"""Phase 3: cross-project upstream learning.

The generality heuristic flags a proposal as an upstream candidate (advisory);
`submit-proposals --upstream` then submits only candidates, to the upstream Cohort
repo (push-remote and PR-target are the same repo — no fork-head mismatch), each
body scrubbed of project markers. The Phase-8 safety invariants still hold.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohort import improve
from cohort.improve import (
    ProjectMarkers,
    _derive_slug,
    project_markers,
    sanitize_for_upstream,
    score_generality,
)
from cohort.loader import load_artifact

COHORT_SRC = Path(__file__).resolve().parents[1]

M_NONE = ProjectMarkers(slug=None, specialists=())
M_PROJ = ProjectMarkers(slug="acme/widgets", specialists=("data-modeler", "fraud-analyst"))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# === pure heuristic =========================================================


def test_derive_slug_ssh_and_https():
    assert _derive_slug("git@github.com:askwigconsulting/cohort.git") == "askwigconsulting/cohort"
    assert _derive_slug("https://github.com/askwigconsulting/cohort.git") == "askwigconsulting/cohort"
    assert _derive_slug("https://github.com/askwigconsulting/cohort") == "askwigconsulting/cohort"
    assert _derive_slug("definitely not a url") is None


def test_derive_slug_rejects_nested_path():
    # GitLab subgroup / GHE sub-org → ambiguous 3-segment path → fail closed
    assert _derive_slug("https://gitlab.com/group/subgroup/repo.git") is None
    assert _derive_slug("git@github.com:owner/repo.git") == "owner/repo"


def test_score_generality_generic_is_candidate():
    ok, why = score_generality(
        {"kind": "improvement"}, "Revisit low-rated agent 'steward'. Reduce friction.", M_PROJ
    )
    assert ok is True and "generic" in why


def test_score_generality_flags_project_specialist():
    ok, why = score_generality({"kind": "improvement"}, "Revisit agent 'data-modeler'.", M_PROJ)
    assert ok is False and "data-modeler" in why


def test_score_generality_flags_project_slug():
    ok, why = score_generality({"kind": "improvement"}, "Context lives in acme/widgets.", M_PROJ)
    assert ok is False and "acme/widgets" in why


def test_score_generality_flags_user_home_path():
    ok, why = score_generality({"kind": "improvement"}, "see /home/alice/notes.md", M_PROJ)
    assert ok is False and "user-home" in why


def test_score_generality_non_improvement_kind_is_not_candidate():
    ok, why = score_generality({"kind": "promotion"}, "anything generic", M_NONE)
    assert ok is False and "canonical-shaped" in why


def test_score_generality_ignores_github_urls():
    # the upstream URL must not be misread as a local path
    ok, _ = score_generality({"kind": "improvement"}, "see https://github.com/x/y", M_PROJ)
    assert ok is True


def test_score_generality_specialist_regex_metachars_are_escaped():
    # an unescaped 'a.*' pattern would match everything (over-redact / ReDoS); escaped, it doesn't
    markers = ProjectMarkers(slug=None, specialists=("a.*",))
    ok, _ = score_generality({"kind": "improvement"}, "totally generic improvement text", markers)
    assert ok is True


def test_score_generality_flags_a_secret_token():
    token = "ghp_" + "A" * 36
    ok, why = score_generality({"kind": "improvement"}, f"pasted token {token}", M_NONE)
    assert ok is False and "secret" in why


def test_local_path_regex_does_not_match_var_home():
    # /var/home/... is not a user-home path — the anchored regex must not flag it
    ok, _ = score_generality({"kind": "improvement"}, "config under /var/home/svc/data", M_NONE)
    assert ok is True


def test_email_regex_is_bounded_against_redos():
    # a pathological near-email must not catastrophically backtrack
    import time

    payload = ("a." * 20000) + "@"
    start = time.perf_counter()
    score_generality({"kind": "improvement"}, payload, M_NONE)
    assert time.perf_counter() - start < 1.0  # linear, not quadratic


def test_sanitize_removes_all_markers():
    text = "Improve data-modeler and see acme/widgets at /home/bob/x/y."
    clean, removed = sanitize_for_upstream(text, M_PROJ)
    assert "data-modeler" not in clean and "acme/widgets" not in clean and "/home/bob/x/y" not in clean
    assert "[project specialist]" in clean and "[project repo]" in clean and "[user path]" in clean
    assert "data-modeler" in removed and "acme/widgets" in removed


def test_sanitize_is_a_noop_on_clean_text():
    clean, removed = sanitize_for_upstream("totally generic improvement", M_PROJ)
    assert removed == [] and clean == "totally generic improvement"


def test_project_markers_reads_slug_and_specialists(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    agents = repo / ".cohort" / "agents"
    agents.mkdir(parents=True)
    (agents / "data-modeler.md").write_text("---\nname: data-modeler\n---\nbody\n", encoding="utf-8")
    markers = project_markers(repo)
    assert markers.slug == "acme/widgets" and "data-modeler" in markers.specialists


# === integration (mirror Phase-8 fixtures + recording fake) =================


class RecordingRunner:
    """A fake git/gh that records every invocation and never really runs."""

    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return None


def tree_hash(root: Path) -> str:
    if not root.exists():
        return "MISSING"
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def _run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    _git(src, "init", "-q")
    _git(src, "config", "user.name", "D")
    _git(src, "config", "user.email", "d@e.com")
    _git(src, "remote", "add", "origin", "https://github.com/askwigconsulting/cohort.git")
    return src


@pytest.fixture
def home(tmp_path, source):
    h = tmp_path / "home"
    h.mkdir()
    _run_cli("recompile", "--ide", "claude", "--source", str(source), home=h)
    return h


@pytest.fixture
def repo(tmp_path, source, home):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "D")
    _git(r, "config", "user.email", "d@e.com")
    (r / "README.md").write_text("# r\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    _run_cli("init", "--source", str(source), home=home, cwd=r)
    return r


def _proposal(repo: Path) -> Path:
    return next((repo / ".cohort" / "proposals").glob("*.md"))


def test_propose_stamps_general_as_upstream_candidate(repo, home):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", "--note", "slow", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    fm = load_artifact(_proposal(repo)).frontmatter
    assert fm["upstream_candidate"] is True  # identity, not truthiness — guards a stringify regression
    assert "generic" in fm["upstream_rationale"]


def test_propose_stamps_project_specialist_proposal_as_non_candidate(repo, home):
    _run_cli("add-specialist", "--name", "data-modeler", "--display-name", "DM",
             "--department", "Data", "--description", "x", home=home, cwd=repo)
    _run_cli("feedback", "--rating", "down", "--agent", "data-modeler", "--note", "x", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    fm = load_artifact(_proposal(repo)).frontmatter
    assert fm["upstream_candidate"] is False
    assert "data-modeler" in fm["upstream_rationale"]


def test_submit_upstream_filters_to_candidates_and_targets_upstream(repo, home, source):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(
        repo, source, dry_run=False, run=runner, gh_ok=True, home=home, upstream=True
    )
    assert len(result["submitted"]) == 1
    flat = [" ".join(c) for c in runner.calls]
    pr = [c for c in flat if "pr create" in c]
    assert pr and all("--repo askwigconsulting/cohort" in c for c in pr)
    # coherence: the branch is pushed to the same repo the PR targets (resolved upstream remote)
    pushes = [c for c in flat if c.startswith("git") and " push " in c]
    assert pushes and all("push -- origin cohort/proposal-" in c for c in pushes)


def test_submit_upstream_skips_non_candidates(repo, home, source):
    _run_cli("add-specialist", "--name", "data-modeler", "--display-name", "DM",
             "--department", "Data", "--description", "x", home=home, cwd=repo)
    _run_cli("feedback", "--rating", "down", "--agent", "data-modeler", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(
        repo, source, dry_run=False, run=runner, gh_ok=True, home=home, upstream=True
    )
    assert result["submitted"] == [] and runner.calls == []  # nothing attempted


def test_submit_upstream_sanitizes_staged_body(repo, home, source):
    pdir = repo / ".cohort" / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "improvement-leaky.md").write_text(
        improve.dump_frontmatter([("kind", "improvement"), ("upstream_candidate", True)])
        + "Body mentions acme/widgets and a path /home/alice/secret/x.\n",
        encoding="utf-8",
    )
    improve.do_submit_proposals(
        repo, source, dry_run=False, run=RecordingRunner(), gh_ok=True, home=home, upstream=True
    )
    staged = (source / "proposals" / "improvement-leaky.md").read_text(encoding="utf-8")
    assert "acme/widgets" not in staged and "/home/alice/secret" not in staged
    assert "[project repo]" in staged and "[user path]" in staged


def test_submit_upstream_preserves_phase8_invariants(repo, home, source):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    before = tree_hash(source / "canonical")
    runner = RecordingRunner()
    improve.do_submit_proposals(
        repo, source, dry_run=False, run=runner, gh_ok=True, home=home, upstream=True
    )
    assert tree_hash(source / "canonical") == before  # never edits canonical
    flat = [" ".join(c) for c in runner.calls]
    assert not any("merge" in c.split() for c in flat)  # never merges
    assert all("--draft" in c for c in flat if "pr create" in c)  # PRs are drafts


def test_submit_upstream_degrades_when_upstream_unresolvable(repo, home, tmp_path):
    src = tmp_path / "src-no-remote"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    _git(src, "init", "-q")  # no origin remote → no upstream slug
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(
        repo, src, dry_run=False, run=runner, gh_ok=True, home=home, upstream=True
    )
    assert result["degraded"] is True and runner.calls == []
    assert "could not resolve" in result.get("detail", "")


def test_non_upstream_submit_is_unchanged(repo, home, source):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(repo, source, dry_run=False, run=runner, gh_ok=True)
    assert len(result["submitted"]) == 1  # plain mode submits regardless of candidate flag
    flat = [" ".join(c) for c in runner.calls]
    assert any("push -- origin" in c for c in flat)
    assert not any("--repo" in c for c in flat)  # no PR target in plain mode


def test_submit_cleanup_on_failure_enables_retry(repo, home, source):
    """A mid-flight submit failure cleans up the leftover branch + staged file so a
    retry isn't wedged and a later `cohort update` isn't blocked by a dirty tree."""
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    name = _proposal(repo).name

    class FailingPush:
        def __init__(self):
            self.calls: list[list] = []

        def __call__(self, cmd):
            self.calls.append(list(cmd))
            if "push" in cmd:
                raise RuntimeError("no push access")
            return None

    runner = FailingPush()
    result = improve.do_submit_proposals(
        repo, source, dry_run=False, run=runner, gh_ok=True, home=home, upstream=True
    )
    assert result["degraded"] is True
    flat = [" ".join(c) for c in runner.calls]
    assert any("branch -D cohort/proposal-" in c for c in flat)  # leftover branch deleted
    assert not (source / "proposals" / name).exists()  # staged file removed (no dirty-tree wedge)


def test_submit_invalid_repo_target_degrades(repo, home, source):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(
        repo, source, dry_run=False, run=runner, gh_ok=True, target_repo="../evil"
    )
    assert result["degraded"] is True and "OWNER/REPO" in result.get("detail", "")
    assert runner.calls == []  # validated before any git/gh ran


def test_feedback_rejects_overlong_field(repo, home):
    r = _run_cli("feedback", "--rating", "up", "--agent", "x" * 5000, home=home, cwd=repo)
    assert r.returncode == 1 and "too long" in (r.stderr + r.stdout)


def test_cli_upstream_and_repo_are_mutually_exclusive(repo, home):
    r = _run_cli("submit-proposals", "--upstream", "--repo", "me/fork", home=home, cwd=repo)
    assert r.returncode == 2 and "not used with" in (r.stderr + r.stdout)


def test_propose_dry_run_classifies_without_writing(repo, home):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    r = _run_cli("propose-improvement", "--dry-run", "--json", home=home, cwd=repo)
    import json
    report = json.loads(r.stdout)
    assert report["upstream_candidate"] is True and "generic" in report["upstream_rationale"]
    assert not list((repo / ".cohort" / "proposals").glob("*.md"))  # nothing written


def test_submit_upstream_stamps_destination_and_is_idempotent(repo, home, source):
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(),
                                gh_ok=True, home=home, upstream=True)
    fm = load_artifact(_proposal(repo)).frontmatter
    assert fm["submitted_upstream"] == "askwigconsulting/cohort"
    assert "submitted_at" not in fm  # upstream submit must not bar a later local submit
    # re-run upstream → skipped (already submitted upstream), no duplicate PR
    r2 = improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(),
                                     gh_ok=True, home=home, upstream=True)
    assert r2["submitted"] == [] and len(r2["skipped"]) == 1


def test_local_then_upstream_both_submit(repo, home, source):
    """A proposal useful both ways can go local AND upstream — per-destination keys."""
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    local = improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(), gh_ok=True)
    upstream = improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(),
                                           gh_ok=True, home=home, upstream=True)
    assert len(local["submitted"]) == 1 and len(upstream["submitted"]) == 1
    fm = load_artifact(_proposal(repo)).frontmatter
    assert "submitted_at" in fm and fm.get("submitted_upstream") == "askwigconsulting/cohort"


def test_submit_upstream_redacts_email_and_token(repo, home, source):
    pdir = repo / ".cohort" / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    secret = "ghp_" + "A" * 36
    (pdir / "improvement-pii.md").write_text(
        improve.dump_frontmatter([("kind", "improvement"), ("upstream_candidate", True)])
        + f"Reported by jane@acme-internal.com with token {secret}.\n",
        encoding="utf-8",
    )
    result = improve.do_submit_proposals(repo, source, dry_run=False, run=RecordingRunner(),
                                         gh_ok=True, home=home, upstream=True)
    staged = (source / "proposals" / "improvement-pii.md").read_text(encoding="utf-8")
    assert "jane@acme-internal.com" not in staged and secret not in staged
    assert "[email]" in staged and "[redacted token]" in staged
    assert result["redacted"]  # surfaced for visibility


def test_propose_flags_email_in_body_as_non_candidate(repo, home):
    # a free-text proposal carrying an email must not be flagged upstream-safe
    ok, why = score_generality(
        {"kind": "improvement"}, "raised by jane@acme-internal.com on host db-prod-01", M_NONE
    )
    assert ok is False and "email" in why


def test_submit_upstream_rejects_option_like_remote(repo, home, tmp_path, monkeypatch):
    """A tampered [update] upstream_remote that looks like a git option must not
    reach `git push` — it fails to resolve a valid target and degrades."""
    src = tmp_path / "src-evil"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    _git(src, "init", "-q")
    _git(src, "remote", "add", "origin", "https://github.com/askwigconsulting/cohort.git")
    cohort_toml = home / ".cohort" / "cohort.toml"
    cohort_toml.parent.mkdir(parents=True, exist_ok=True)
    cohort_toml.write_text(
        '[update]\nupstream_remote = "--receive-pack=touch /tmp/PWNED"\n', encoding="utf-8"
    )
    _run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo)
    _run_cli("propose-improvement", home=home, cwd=repo)
    runner = RecordingRunner()
    result = improve.do_submit_proposals(repo, src, dry_run=False, run=runner,
                                         gh_ok=True, home=home, upstream=True)
    assert result["degraded"] is True and runner.calls == []  # no git/gh ever ran

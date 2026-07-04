"""Phase 9: frontmatter-safety, docs==test, and the full-system e2e (capstone)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cohort import improve
from cohort.frontmatter import check_frontmatter_safety, dump_frontmatter
from cohort.loader import load_artifact_text
from cohort.quickstart import QUICKSTART_STEPS, quickstart_verbs

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


def make_git_repo(path: Path, user_name: str = "Dev") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", user_name], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


# === P9-T1: frontmatter safety ==============================================


def _parse_fm(block: str) -> dict:
    return load_artifact_text(block + "body\n", name_stem="x").frontmatter


def test_dump_frontmatter_handles_adversarial_values():
    pairs = [("a", "Foo: Bar"), ("b", "[note]"), ("c", "trailing # hash"),
             ("d", 'has "quotes"'), ("e", "line1\nline2"), ("rating", "up"), ("n", 3)]
    parsed = _parse_fm(dump_frontmatter(pairs))
    assert parsed["a"] == "Foo: Bar"
    assert parsed["b"] == "[note]"
    assert parsed["d"] == 'has "quotes"'
    assert parsed["e"] == "line1\nline2"
    assert parsed["rating"] == "up"  # safe value stays plain + parses


def test_safe_values_stay_unquoted():
    # round-trip rule: readable metadata isn't needlessly quoted
    assert "kind: promotion" in dump_frontmatter([("kind", "promotion")])


def test_snapshot_author_residual_fixed(tmp_path):
    repo = make_git_repo(tmp_path / "repo", user_name="Foo: Bar")  # the residual trigger
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    run_cli("init", "--source", str(src), home=home, cwd=repo)
    run_cli("snapshot", home=home, cwd=repo)
    session = next((repo / ".cohort" / "sessions").glob("*.md"))
    result = load_artifact_text(session.read_text(), name_stem=session.stem)
    assert result.load_error is None  # parses despite the ':' in author
    assert "Foo: Bar" in result.frontmatter["author"]


def test_feedback_agent_residual_fixed(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    run_cli("init", "--source", str(src), home=home, cwd=repo)
    run_cli("feedback", "--rating", "down", "--agent", "a: b", home=home, cwd=repo)
    fb = next((repo / ".cohort" / "feedback").glob("*.md"))
    result = load_artifact_text(fb.read_text(), name_stem=fb.stem)
    assert result.load_error is None and result.frontmatter["agent"] == "a: b"


def test_yaml_safety_lint_catches_bad_and_passes_good(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".cohort" / "feedback").mkdir(parents=True)
    # a good (safe-emitted) entry
    (repo / ".cohort" / "feedback" / "good.md").write_text(
        dump_frontmatter([("rating", "up"), ("agent", "Foo: Bar")]) + "note\n", encoding="utf-8"
    )
    assert check_frontmatter_safety(repo) == []  # passes on safe output
    # a seeded BAD entry (raw unsafe value — the Phase-8 class)
    (repo / ".cohort" / "feedback" / "bad.md").write_text(
        "---\nauthor: Foo: Bar\n---\nx\n", encoding="utf-8"
    )
    bad = check_frontmatter_safety(repo)
    assert any("bad.md" in p for p in bad)  # the lint catches it


# === P9-T2: docs == test ====================================================


def test_readme_quickstart_matches_source_of_truth():
    readme = (COHORT_SRC / "README.md").read_text()
    block = re.search(r"```bash\n(.*?)```", readme, re.DOTALL).group(1)
    lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    # strip trailing inline comments so README lines may be annotated
    cohort_lines = [ln.split(" #", 1)[0].strip() for ln in lines if ln.startswith("cohort ")]
    assert cohort_lines == QUICKSTART_STEPS  # the journey can't drift from the e2e source

    # …and the quickstart must actually make `cohort` runnable from a bare clone:
    # a package-install step (pip install / bootstrap) has to precede the first
    # `cohort` command. (Regression guard for the "cohort: command not found" gap.)
    first_cohort = next(i for i, ln in enumerate(lines) if ln.startswith("cohort "))
    setup = " ".join(lines[:first_cohort])
    assert "pip install" in setup or "bootstrap.sh" in setup, (
        "quickstart runs `cohort` before any step that installs the CLI"
    )


# === P9-T3/T4: full-system end-to-end across all three IDEs ==================


class _RecordingRunner:
    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return None


def _tree_hash(root: Path) -> str:
    import hashlib

    if not root.exists():
        return "MISSING"
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_full_system_e2e_all_three_ides(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "src"
    shutil.copytree(COHORT_SRC / "canonical", source / "canonical")
    repo = make_git_repo(tmp_path / "repo")
    other = make_git_repo(tmp_path / "other")  # for the isolation check
    run = lambda *a: run_cli(*a, home=home, cwd=repo)
    verbs: list[str] = []

    # recompile = compile + install all three offices (the clone-and-go command)
    verbs.append("recompile")
    assert run("recompile", "--ide", "claude,codex,cursor", "--source", str(source)).returncode == 0
    for ide, ext in [(".claude", ".md"), (".codex", ".toml"), (".cursor", ".md")]:
        assert len(list((home / ide / "agents").glob(f"*{ext}"))) == 19
    assert "applied: 0" in run("recompile", "--ide", "claude,codex,cursor", "--source", str(source)).stdout

    verbs.append("init")
    assert run("init", "--source", str(source)).returncode == 0

    verbs.append("add-specialist")
    assert run("add-specialist", "--name", "data-modeler", "--display-name", "DataModeler",
               "--department", "Data", "--description", "Schema and data modeling.").returncode == 0
    assert (repo / ".claude" / "agents" / "data-modeler.md").exists()
    # isolation: invisible to the other repo and to the global roster
    assert not (other / ".claude" / "agents" / "data-modeler.md").exists()
    assert not (home / ".claude" / "agents" / "data-modeler.md").exists()

    verbs.append("snapshot")
    assert run("snapshot").returncode == 0
    verbs.append("weekly-report")
    assert run("weekly-report").returncode == 0
    verbs.append("feedback")
    assert run("feedback", "--rating", "up", "--agent", "data-modeler").returncode == 0
    verbs.append("propose-improvement")
    assert run("propose-improvement").returncode == 0

    # submit via the direct API with a fake git/gh (no remote needed) — human gate proof
    before_canon = _tree_hash(source / "canonical")
    before_global = _tree_hash(home / ".cohort" / "canonical")
    runner = _RecordingRunner()
    verbs.append("submit-proposals")
    improve.do_submit_proposals(repo, source, dry_run=False, run=runner, gh_ok=True)
    flat = [" ".join(c) for c in runner.calls]
    assert _tree_hash(source / "canonical") == before_canon  # no auto-edit canonical
    assert _tree_hash(home / ".cohort" / "canonical") == before_global
    assert not any("merge" in c.split() or "pr merge" in c for c in flat)  # no auto-merge
    assert all("--draft" in c for c in flat if "pr create" in c)  # drafts only

    # the e2e exercised exactly the quickstart's verb sequence (binding to the doc)
    assert verbs == quickstart_verbs()
    # every generated frontmatter parses (the systemic YAML-safety guard)
    assert check_frontmatter_safety(repo) == []

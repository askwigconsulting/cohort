"""`cohort try` — preview a compiled agent before install; optional sandbox (#68)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.trial import TryError, do_try

COHORT_SRC = Path(__file__).resolve().parents[1]

DRAFT = (
    "---\nname: risk-draft\nkind: agent\nscope: global\ndescription: A draft risk advisor.\n"
    "targets: [claude]\ndepartment: Risk\ntopology: specialist\nadvisory: true\n"
    "tools: [read, bash, write]\ndisplay_name: RiskDraft\n---\nYou advise on risk. (draft body)\n"
)


def run_cli(*args, home, cwd=None):
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
    return src


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_preview_office_agent_shows_rendered_bytes(source, home):
    report = do_try(source, home, "counsel")
    assert report["layer"] == "office"
    assert report["name"] == "counsel"
    # the rendered preview is the exact staged artifact (frontmatter + header + body)
    assert "name: counsel" in report["rendered"]
    assert "advisory office agent" in report["rendered"]
    assert "placed" not in report  # preview installs nothing


def test_preview_strips_write_tools_visibly(source, home, tmp_path):
    draft = tmp_path / "risk-draft.md"
    draft.write_text(DRAFT, encoding="utf-8")
    report = do_try(source, home, str(draft))
    assert report["layer"] == "file"
    # advisory enforcement is visible in the preview: bash/write never survive
    assert report["tools"] == "Read"
    assert "Bash" not in report["rendered"] and "Write" not in report["rendered"]


def test_preview_accepts_a_scratch_filename(source, home, tmp_path):
    # a draft named draft.md (name != stem) previews fine — the file-match rule is
    # an authoring concern, not a preview concern
    draft = tmp_path / "draft.md"
    draft.write_text(DRAFT, encoding="utf-8")
    report = do_try(source, home, str(draft))
    assert report["name"] == "risk-draft"


def test_preview_my_office_agent(source, home):
    my = home / ".cohort" / "my" / "canonical" / "agents"
    my.mkdir(parents=True)
    (my / "trading-compliance.md").write_text(
        DRAFT.replace("risk-draft", "trading-compliance").replace("RiskDraft", "TradingCompliance"),
        encoding="utf-8",
    )
    report = do_try(source, home, "trading-compliance")
    assert report["layer"] == "my"


def test_invalid_draft_is_refused(source, home, tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("---\nname: bad\nkind: agent\nscope: global\n---\nno required fields\n",
                   encoding="utf-8")
    with pytest.raises(TryError, match="invalid"):
        do_try(source, home, str(bad))


def test_non_agent_is_refused(source, home):
    # update is a command in canonical/commands — try only previews agents
    cmd = source / "canonical" / "commands" / "update.md"
    with pytest.raises(TryError, match="not an agent"):
        do_try(source, home, str(cmd))


def test_unknown_name_is_refused(source, home):
    with pytest.raises(TryError, match="no agent"):
        do_try(source, home, "not-a-real-agent")


def test_place_sandboxes_into_the_project(source, home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    draft = repo / "risk-draft.md"
    draft.write_text(DRAFT, encoding="utf-8")
    report = do_try(source, home, str(draft), place=True, repo=repo)
    placed = repo / ".claude" / "agents" / "risk-draft.md"
    assert placed.exists()  # sandboxed as a project specialist (overrides user-level)
    assert report["placed"]
    # reversible via the normal command
    assert run_cli("remove-specialist", "risk-draft", home=home, cwd=repo).returncode == 0
    assert not placed.exists()


def test_cli_preview_is_read_only(source, home):
    proc = run_cli("try", "counsel", "--source", str(source), home=home)
    assert proc.returncode == 0
    assert "counsel" in proc.stdout
    assert "preview only" in proc.stderr
    assert not (home / ".claude").exists()  # nothing installed


def test_cli_json_output(source, home):
    proc = run_cli("try", "counsel", "--json", "--source", str(source), home=home)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["name"] == "counsel" and data["layer"] == "office" and "rendered" in data

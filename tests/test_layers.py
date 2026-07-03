"""#84 increment 1: the my-office layer — layered global compile, additions-only.

Three config layers: the office (the source clone), my office (~/.cohort/my,
machine-local, never touched by update), and the project. This file covers the
compile core: my additions merge over the office layer, collisions refuse, the
scope leak-guard runs per layer, the roster subset filters the office layer
only, and update/uninstall leave my/ alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.compile import CompileError, compile_ide
from cohort.install_model import CohortPaths

COHORT_SRC = Path(__file__).resolve().parents[1]

MY_AGENT = (
    "---\nname: trading-compliance\nkind: agent\nscope: global\n"
    "description: Trading-desk compliance advice.\ntargets: [claude]\n"
    "department: MyDesk\ntopology: specialist\nadvisory: true\ntools: [read]\n"
    "display_name: TradingCompliance\n---\nPersonal advisor body.\n"
)


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
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


def _my(home: Path, sub: str, name: str, text: str) -> Path:
    d = home / ".cohort" / "my" / "canonical" / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_my_agent_compiles_places_and_joins_the_directory(source, home):
    _my(home, "agents", "trading-compliance", MY_AGENT)
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".claude" / "agents" / "trading-compliance.md").exists()
    chief = (home / ".claude" / "agents" / "chief-of-staff.md").read_text(encoding="utf-8")
    assert "**TradingCompliance**" in chief  # merged into the injected office directory


def test_my_office_collision_is_refused_not_masked(source, home):
    _my(home, "agents", "counsel", MY_AGENT.replace("trading-compliance", "counsel"))
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "collide" in proc.stderr and "counsel" in proc.stderr
    assert not (home / ".claude" / "agents").exists()  # nothing placed on refusal


def test_scope_leak_guard_runs_per_layer(source, home):
    _my(home, "commands", "proj-cmd",
        "---\nname: proj-cmd\nkind: command\nscope: project\ndescription: x.\n"
        "targets: [claude]\ninvocation: proj-cmd\ndry_run: true\n---\nbody\n")
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert not (home / ".claude" / "commands" / "proj-cmd.md").exists()
    assert "[my]" in proc.stderr  # the tier drop is reported with its layer


def test_roster_subset_filters_office_layer_only(source, home):
    _my(home, "agents", "trading-compliance", MY_AGENT)
    proc = run_cli("setup", "--ide", "claude", "--agents", "counsel,chief-of-staff",
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    placed = sorted(p.stem for p in (home / ".claude" / "agents").glob("*.md"))
    # the subset tailored the company roster; the personal agent still installs
    assert placed == ["chief-of-staff", "counsel", "trading-compliance"]


def test_update_recompile_never_prunes_a_my_agent(source, home):
    """The headline guarantee: a tailored roster + an update-driven recompile
    must not prune a personal agent (the update.py only_agents prune trap)."""
    from cohort.update import _recompile_installed

    run_cli("setup", "--ide", "claude", "--agents", "counsel,chief-of-staff",
            "--source", str(source), home=home)
    _my(home, "agents", "trading-compliance", MY_AGENT)
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    placed = home / ".claude" / "agents" / "trading-compliance.md"
    assert placed.exists()
    recompiled, refused = _recompile_installed(source, home)
    assert refused is None
    assert placed.exists()  # survived the update-path recompile with a subset roster


def test_uninstall_preserves_my_office(source, home):
    _my(home, "agents", "trading-compliance", MY_AGENT)
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    proc = run_cli("uninstall", "--ide", "claude", home=home)
    assert proc.returncode == 0, proc.stderr
    kept = home / ".cohort" / "my" / "canonical" / "agents" / "trading-compliance.md"
    assert kept.exists()  # my office is user content — uninstall never deletes it


def test_empty_or_absent_overlay_is_a_byte_noop(source, tmp_path):
    plain = compile_ide(source, "claude", scope="global")
    with_absent = compile_ide(source, "claude", scope="global", overlay=tmp_path / "nowhere")
    assert [s.staged_rel for s in plain.staged] == [s.staged_rel for s in with_absent.staged]
    for a, b in zip(plain.staged, with_absent.staged):
        assert a.content == b.content  # the layer machinery cannot move existing bytes


def test_office_artifact_bytes_unchanged_by_a_my_addition(source, tmp_path, home):
    """The layer field and the merge must never alter how an existing office
    artifact renders — only the generalist's injected directory may change."""
    _my(home, "agents", "trading-compliance", MY_AGENT)
    overlay = CohortPaths.for_global(home).my
    plain = {s.staged_rel: s.content for s in compile_ide(source, "claude", scope="global").staged}
    layered = {s.staged_rel: s.content
               for s in compile_ide(source, "claude", scope="global", overlay=overlay).staged}
    assert "agents/trading-compliance.md" in layered
    for rel, content in plain.items():
        if rel == "agents/chief-of-staff.md":
            continue  # the office directory legitimately gains the new row
        assert layered[rel] == content, rel


# === increment 2: the authoring surface targets my office ====================


def test_add_agent_defaults_to_my_office_and_says_so(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    proc = run_cli("add-agent", "--name", "clinical-data", "--display-name", "ClinicalData",
                   "--department", "Health", "--description", "Clinical data advice.",
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".cohort" / "my" / "canonical" / "agents" / "clinical-data.md").exists()
    assert not (source / "canonical" / "agents" / "clinical-data.md").exists()  # clone clean
    assert "my office" in proc.stderr  # destination said out loud
    assert "not version-controlled" in proc.stderr  # first-write notice
    assert (home / ".claude" / "agents" / "clinical-data.md").exists()


def test_add_agent_to_office_writes_the_clone(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    proc = run_cli("add-agent", "--name", "clinical-data", "--display-name", "ClinicalData",
                   "--department", "Health", "--description", "x.", "--to", "office",
                   "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (source / "canonical" / "agents" / "clinical-data.md").exists()
    assert "office layer" in proc.stderr


def test_add_agent_cross_layer_collision_refused(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    # counsel lives in the office layer; a my-layer twin must refuse early
    proc = run_cli("add-agent", "--name", "counsel", "--display-name", "X",
                   "--department", "Y", "--description", "z.",
                   "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "office layer" in proc.stderr and "already exists" in proc.stderr


def test_second_generalist_refused_across_layers(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    proc = run_cli("add-agent", "--name", "boss", "--display-name", "Boss",
                   "--department", "Exec", "--description", "x.", "--topology", "generalist",
                   "--source", str(source), home=home)
    assert proc.returncode == 1
    assert "generalist" in proc.stderr  # chief-of-staff already holds the seat


def test_status_reports_the_my_layer(source, home):
    import json as _json

    _my(home, "agents", "trading-compliance", MY_AGENT)
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    report = _json.loads(run_cli("status", "--json", home=home).stdout)
    assert report["global"]["roster"]["my"] == ["trading-compliance"]
    assert report["global"]["roster"]["count"] == 18  # 17 office + 1 my


# === increment 3: sharing affordances ========================================


def _project_with_specialist(tmp_path, source, home, name="data-modeler"):
    import subprocess as sp
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@e.st"], cwd=repo, check=True)
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    run_cli("add-specialist", "--name", name, "--display-name", name.title(),
            "--department", "Data", "--description", "x.", home=home, cwd=repo)
    return repo


def test_promote_defaults_to_my_office_direct_copy(tmp_path, source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    repo = _project_with_specialist(tmp_path, source, home)
    proc = run_cli("promote", "data-modeler", "--source", str(source), home=home, cwd=repo)
    assert proc.returncode == 0, proc.stderr
    lifted = home / ".cohort" / "my" / "canonical" / "agents" / "data-modeler.md"
    assert lifted.exists()
    assert "scope: global" in lifted.read_text(encoding="utf-8")  # re-scoped for the tier
    assert (home / ".claude" / "agents" / "data-modeler.md").exists()  # placed globally
    assert (repo / ".cohort" / "canonical" / "agents" / "data-modeler.md").exists()  # copy stays
    assert not (repo / ".cohort" / "proposals").exists()  # no proposal for a personal lift


def test_promote_to_my_refuses_office_name(tmp_path, source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    repo = _project_with_specialist(tmp_path, source, home, name="counsel")  # shadow name
    proc = run_cli("promote", "counsel", "--source", str(source), home=home, cwd=repo)
    assert proc.returncode == 1
    assert "office layer" in proc.stderr and "already exists" in proc.stderr


def test_status_flags_local_only_office_content(tmp_path, monkeypatch):
    import json as _json
    import subprocess as sp

    from cohort.status import do_status

    up = tmp_path / "up"
    up.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=up, check=True)
    sp.run(["git", "config", "user.name", "T"], cwd=up, check=True)
    sp.run(["git", "config", "user.email", "t@e.st"], cwd=up, check=True)
    (up / "canonical" / "agents").mkdir(parents=True)
    (up / "canonical" / "agents" / "shared.md").write_text("x\n", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=up, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=up, check=True)
    src = tmp_path / "clone"
    sp.run(["git", "clone", "-q", str(up), str(src)], check=True)
    (src / "canonical" / "agents" / "personal.md").write_text("mine\n", encoding="utf-8")
    monkeypatch.setenv("COHORT_SOURCE", str(src))
    h = tmp_path / "h"
    h.mkdir()
    report = do_status(h, h)
    assert "canonical/agents/personal.md" in report["global"]["office_local_only"]


# === increment 4: personalize + deliberate overrides =========================


def test_personalize_copies_and_overrides(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    proc = run_cli("personalize", "agent", "counsel", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    copy = home / ".cohort" / "my" / "canonical" / "agents" / "counsel.md"
    text = copy.read_text(encoding="utf-8")
    assert "overrides: true" in text and "office_sha256:" in text
    # edit the personal copy; the recompiled office must carry MY version
    copy.write_text(text + "\n**House rule.** Always cite the clause.\n", encoding="utf-8")
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert "my office overrides: counsel" in proc.stderr
    placed = (home / ".claude" / "agents" / "counsel.md").read_text(encoding="utf-8")
    assert "Always cite the clause" in placed  # my copy won
    chief = (home / ".claude" / "agents" / "chief-of-staff.md").read_text(encoding="utf-8")
    assert chief.count("**Counsel**") == 1  # one directory row, not two


def test_personalize_refusals(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    proc = run_cli("personalize", "agent", "not-real", "--source", str(source), home=home)
    assert proc.returncode == 1 and "no office agent" in proc.stderr
    assert run_cli("personalize", "agent", "counsel", "--source", str(source),
                   home=home).returncode == 0
    proc = run_cli("personalize", "agent", "counsel", "--source", str(source), home=home)
    assert proc.returncode == 1 and "already personalized" in proc.stderr


def test_status_flags_stale_and_dangling_overrides(source, home, monkeypatch):
    import json as _json

    from cohort.status import do_status

    monkeypatch.setenv("COHORT_SOURCE", str(source))
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    run_cli("personalize", "agent", "counsel", "--source", str(source), home=home)
    report = do_status(home, home)
    assert report["global"]["overrides"] == []  # fresh override: healthy
    assert report["global"]["roster"]["count"] == 17  # override never double-counts
    office = source / "canonical" / "agents" / "counsel.md"
    office.write_text(office.read_text(encoding="utf-8") + "\nupstream improved this\n",
                      encoding="utf-8")
    report = do_status(home, home)
    assert report["global"]["overrides"] == [{"name": "counsel", "state": "stale"}]
    office.unlink()
    report = do_status(home, home)
    assert report["global"]["overrides"] == [{"name": "counsel", "state": "dangling"}]


def test_dangling_override_still_compiles(source, home):
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    run_cli("personalize", "agent", "counsel", "--source", str(source), home=home)
    (source / "canonical" / "agents" / "counsel.md").unlink()  # upstream removed it
    proc = run_cli("recompile", "--ide", "claude", "--source", str(source), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".claude" / "agents" / "counsel.md").exists()  # the user's copy survives

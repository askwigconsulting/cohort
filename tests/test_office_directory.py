"""P3-T2: generalist office-directory injection."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cohort.adapters.claude import (
    MarkerError,
    OFFICE_DIRECTORY_MARKER,
    render_agent,
    render_office_directory,
)
from cohort.compile import CompileError, compile_ide
from cohort.ir import IRArtifact, build_ir
from cohort.loader import load_artifact

REPO_ROOT = Path(__file__).resolve().parents[1]
CANON_AGENTS = REPO_ROOT / "canonical" / "agents"


def agent_ir(name: str) -> IRArtifact:
    r = load_artifact(CANON_AGENTS / f"{name}.md")
    return build_ir(r.frontmatter, r.body, CANON_AGENTS / f"{name}.md")


def fake_agent(name, topology, body, department="Dept", display=None) -> IRArtifact:
    return IRArtifact(
        kind="agent", name=name, scope="global", targets=["all"],
        description=f"{name} desc", version="0.1.0", body=body,
        display_name=display or name, owner=None,
        fields={"department": department, "topology": topology, "advisory": True, "tools": []},
    )


# --- directory generation ---------------------------------------------------


def test_directory_orders_by_department_then_name():
    specialists = [
        fake_agent("b-two", "specialist", "x", department="Bravo"),
        fake_agent("a-one", "specialist", "x", department="Bravo"),
        fake_agent("c-three", "specialist", "x", department="Alpha"),
    ]
    out = render_office_directory(specialists)
    lines = out.splitlines()
    assert lines[0].startswith("- **c-three** (Alpha)")  # Alpha first
    assert lines[1].startswith("- **a-one** (Bravo)")  # then Bravo, name order
    assert lines[2].startswith("- **b-two** (Bravo)")


# --- marker replacement -----------------------------------------------------


def test_generalist_marker_replaced_with_directory():
    directory = render_office_directory([fake_agent("sec", "specialist", "x", display="Sec")])
    rendered = render_agent(agent_ir("chief-of-staff"), directory).content.decode("utf-8")
    assert OFFICE_DIRECTORY_MARKER not in rendered  # marker consumed
    assert "**Sec**" in rendered


def test_generalist_missing_marker_raises():
    ir = fake_agent("rogue-generalist", "generalist", "No marker here.")
    with pytest.raises(MarkerError):
        render_agent(ir, "directory")


def test_specialist_with_marker_raises():
    ir = fake_agent("sneaky", "specialist", f"body {OFFICE_DIRECTORY_MARKER}")
    with pytest.raises(MarkerError):
        render_agent(ir, "directory")


def test_no_raw_marker_survives_to_output():
    directory = render_office_directory([fake_agent("sec", "specialist", "x")])
    rendered = render_agent(agent_ir("chief-of-staff"), directory).content.decode("utf-8")
    assert "cohort:office-directory" not in rendered


# --- directory tracks the roster -------------------------------------------


def _staged_chief(staged):
    return next(s for s in staged if s.staged_rel == "agents/chief-of-staff.md").content.decode()


def test_directory_excludes_generalist_and_equals_specialist_set():
    result = compile_ide(REPO_ROOT, "claude")
    chief = _staged_chief(result.staged)
    assert "**ChiefOfStaff**" not in chief.split("Office directory.")[1]  # never lists itself
    # all 14 specialists are listed
    for name, display, *_ in [
        ("counsel", "Counsel"), ("aws-architect", "AWSArchitect"), ("steward", "Steward"),
    ]:
        assert f"**{display}**" in chief


def test_adding_and_removing_a_specialist_changes_directory(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(REPO_ROOT / "canonical", src / "canonical")
    before = _staged_chief(compile_ide(src, "claude").staged)
    assert "**Counsel**" in before
    (src / "canonical" / "agents" / "counsel.md").unlink()  # remove a specialist
    after = _staged_chief(compile_ide(src, "claude").staged)
    assert "**Counsel**" not in after


def test_byte_stable_directory(tmp_path):
    a = _staged_chief(compile_ide(REPO_ROOT, "claude").staged)
    b = _staged_chief(compile_ide(REPO_ROOT, "claude").staged)
    assert a == b


def test_missing_marker_is_compile_error(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(REPO_ROOT / "canonical", src / "canonical")
    chief = src / "canonical" / "agents" / "chief-of-staff.md"
    chief.write_text(chief.read_text().replace(OFFICE_DIRECTORY_MARKER, ""), encoding="utf-8")
    with pytest.raises(CompileError):
        compile_ide(src, "claude")

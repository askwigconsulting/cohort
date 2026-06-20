"""P3-T1: roster-invariant tests (automated gate) for the office roster v1.

The expected roster is a hardcoded literal (decision B) — never derived from the
directory under test, or "no missing / no extra" would be vacuous. Required body
elements are detected by a structural anchor + an exact canonical phrase (E).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort.ir import build_ir
from cohort.loader import load_artifact
from cohort.schema import validate_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[1]
CANON_AGENTS = REPO_ROOT / "canonical" / "agents"

# (name, display_name, department, topology) — the §1.2 contract, encoded here.
EXPECTED_ROSTER = [
    ("chief-of-staff", "ChiefOfStaff", "Orchestration", "generalist"),
    ("hr-partner", "HRPartner", "People", "specialist"),
    ("counsel", "Counsel", "Legal", "specialist"),
    ("compliance", "Compliance", "Risk", "specialist"),
    ("security-engineer", "SecurityEngineer", "Security", "specialist"),
    ("finance-analyst", "FinanceAnalyst", "Finance", "specialist"),
    ("it-support", "ITSupport", "IT", "specialist"),
    ("comms", "Comms", "Communications", "specialist"),
    ("procurement", "Procurement", "Operations", "specialist"),
    ("privacy-officer", "PrivacyOfficer", "Governance", "specialist"),
    ("program-manager", "ProgramManager", "PMO", "specialist"),
    ("aws-architect", "AWSArchitect", "Cloud", "specialist"),
    ("azure-architect", "AzureArchitect", "Cloud", "specialist"),
    ("gcp-architect", "GCPArchitect", "Cloud", "specialist"),
    ("steward", "Steward", "Continuous Improvement", "specialist"),
]
CLOUD_AGENTS = {"aws-architect", "azure-architect", "gcp-architect"}

# Required-element detection contract (E): structural anchor + canonical phrase.
BOUNDARY_ANCHOR = "**Boundaries.**"
BOUNDARY_PHRASE = "advisory only"
DISCLAIMER_ANCHOR = "**Disclaimer.**"
VERIFY_ANCHOR = "**Verify live.**"


def load_agent(name: str):
    result = load_artifact(CANON_AGENTS / f"{name}.md")
    return result, build_ir(result.frontmatter, result.body, CANON_AGENTS / f"{name}.md")


# --- element detectors (also unit-tested below) -----------------------------


def has_boundary(body: str) -> bool:
    return BOUNDARY_ANCHOR in body and BOUNDARY_PHRASE in body.lower()


def has_disclaimer(body: str, phrase: str) -> bool:
    return DISCLAIMER_ANCHOR in body and phrase.lower() in body.lower()


def has_verify_live(body: str) -> bool:
    low = body.lower()
    return VERIFY_ANCHOR in body and "current" in low and "live documentation" in low


# --- roster set & frontmatter ----------------------------------------------


def test_all_agents_validate():
    for path in sorted(CANON_AGENTS.glob("*.md")):
        result = load_artifact(path)
        assert result.load_error is None, path
        errors = validate_frontmatter(result.frontmatter, path.stem)
        assert errors == [], (path, [e.to_dict() for e in errors])


def test_roster_set_matches_expected_exactly():
    on_disk = {p.stem for p in CANON_AGENTS.glob("*.md")}
    expected = {name for name, *_ in EXPECTED_ROSTER}
    assert on_disk == expected, {"missing": expected - on_disk, "extra": on_disk - expected}


@pytest.mark.parametrize("name,display_name,department,topology", EXPECTED_ROSTER)
def test_each_agent_frontmatter_matches_contract(name, display_name, department, topology):
    _result, ir = load_agent(name)
    assert ir.display_name == display_name
    assert ir.fields["department"] == department
    assert ir.fields["topology"] == topology
    assert ir.fields["advisory"] is True
    assert ir.targets == ["all"]


def test_exactly_one_generalist():
    generalists = []
    for path in CANON_AGENTS.glob("*.md"):
        _r, ir = load_agent(path.stem)
        if ir.fields["topology"] == "generalist":
            generalists.append(ir.name)
    assert generalists == ["chief-of-staff"]


# --- required body elements -------------------------------------------------


def test_every_body_states_the_advisory_boundary():
    for path in sorted(CANON_AGENTS.glob("*.md")):
        result = load_artifact(path)
        assert has_boundary(result.body), path.stem


def test_disclaimers_present():
    assert has_disclaimer(load_agent("counsel")[0].body, "not legal advice")
    assert has_disclaimer(load_agent("finance-analyst")[0].body, "not financial advice")


@pytest.mark.parametrize("name", sorted(CLOUD_AGENTS))
def test_cloud_agents_have_verify_live_note(name):
    assert has_verify_live(load_agent(name)[0].body)


def test_cloud_agents_have_web_tools():
    # the verify-live instruction has teeth only with read-only web tools (D)
    for name in CLOUD_AGENTS:
        _r, ir = load_agent(name)
        assert "webfetch" in ir.fields["tools"]
        assert "websearch" in ir.fields["tools"]


# --- unit: the detectors themselves -----------------------------------------


def test_boundary_detector():
    assert has_boundary("**Boundaries.** Advisory only — never acts.")
    assert not has_boundary("Advisory only, but no anchor header.")
    assert not has_boundary("**Boundaries.** but missing the phrase.")


def test_disclaimer_detector():
    assert has_disclaimer("**Disclaimer.** not legal advice", "not legal advice")
    assert not has_disclaimer("not legal advice without anchor", "not legal advice")


def test_verify_live_detector():
    body = "**Verify live.** confirm current values against the live documentation"
    assert has_verify_live(body)
    assert not has_verify_live("**Verify live.** but missing keywords")

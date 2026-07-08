"""Project-scope doers: `advisory: false` (write/exec tools) is allowed ONLY at
`scope: project`. Synced tiers (office, my-office = scope: global) stay
advisory-only, enforced fail-closed in the schema and, as a render-time backstop,
in every renderer via the shared `is_doer` helper. Promotion of a doer to a synced
tier is refused, and a project install discloses its write-capable agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort.adapters.claude import claude_tools
from cohort.ir import build_ir, is_doer
from cohort.schema import E060_SAFETY_INVARIANT, validate_frontmatter

from conftest import requires_symlinks  # noqa: E402

DOER = (
    "---\nname: {n}\nkind: agent\nscope: project\ndescription: Runs deploys.\n"
    "targets: [claude]\ndepartment: Ops\ntopology: specialist\nadvisory: false\n"
    "tools: [read, edit, bash]\n---\nDeploy body.\n"
)


def _agent_fm(*, scope="project", advisory=False, tools=("read", "edit", "bash"), name="deployer"):
    fm = {
        "name": name, "kind": "agent", "scope": scope, "description": "A doer.",
        "targets": ["claude"], "department": "Ops", "topology": "specialist",
        "advisory": advisory,
    }
    if tools is not None:
        fm["tools"] = list(tools)
    return fm


def _codes(fm, name="deployer"):
    return {e.code for e in validate_frontmatter(fm, name)}


# --- schema: fail-closed, project-only ---------------------------------------

def test_project_doer_with_tools_is_valid():
    assert E060_SAFETY_INVARIANT not in _codes(_agent_fm(scope="project", advisory=False))


def test_global_doer_is_rejected():
    assert E060_SAFETY_INVARIANT in _codes(_agent_fm(scope="global", advisory=False))


def test_missing_scope_doer_is_rejected_fail_closed():
    fm = _agent_fm(advisory=False)
    del fm["scope"]  # a malformed/absent scope must NOT slip a doer through
    assert E060_SAFETY_INVARIANT in _codes(fm)


def test_project_doer_without_tools_is_rejected():
    assert E060_SAFETY_INVARIANT in _codes(_agent_fm(scope="project", advisory=False, tools=[]))


def test_advisory_agent_is_unaffected():
    assert E060_SAFETY_INVARIANT not in _codes(
        _agent_fm(scope="global", advisory=True, tools=["read"]))


# --- is_doer: the single source of truth -------------------------------------

def test_is_doer_requires_project_scope_and_non_advisory():
    assert is_doer(build_ir(_agent_fm(scope="project", advisory=False), "b"))
    assert not is_doer(build_ir(_agent_fm(scope="project", advisory=True, tools=["read"]), "b"))
    assert not is_doer(build_ir(_agent_fm(scope="global", advisory=True, tools=["read"]), "b"))


# --- renderer: doer keeps write tools; everyone else read-only ---------------

def test_claude_project_doer_keeps_write_tools():
    ir = build_ir(_agent_fm(scope="project", advisory=False, tools=["read", "edit", "bash"]), "b")
    tools = claude_tools(ir)
    assert "Edit" in tools and "Bash" in tools


def test_claude_forces_readonly_for_a_non_project_agent_even_if_non_advisory():
    # A mis-scoped global agent that somehow carries advisory:false + write tools
    # must still render read-only — the renderer keys off is_doer, not advisory.
    ir = build_ir(_agent_fm(scope="global", advisory=False, tools=["read", "edit", "bash"]), "b")
    tools = claude_tools(ir)
    assert "Edit" not in tools and "Bash" not in tools


# --- promote: a doer cannot reach a synced tier ------------------------------

def _project(repo: Path):
    from cohort.install_model import CohortPaths
    from cohort.manifest import Manifest

    ppaths = CohortPaths.for_project(repo)
    ppaths.state.mkdir(parents=True)
    Manifest(install_id="proj00000001", created_at="2026-01-01T00:00:00+00:00",
             mode="link", ides=["project"]).persist(ppaths.manifest)
    return ppaths


def _write_agent(ppaths, name: str, text: str) -> None:
    d = ppaths.cohort_home / "canonical" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text.format(n=name), encoding="utf-8")


def test_promote_refuses_a_project_doer(tmp_path):
    from cohort.specialists import PromoteError, do_promote

    repo = tmp_path / "repo"
    ppaths = _project(repo)
    _write_agent(ppaths, "deployer", DOER)
    with pytest.raises(PromoteError, match="doer"):
        do_promote(repo, tmp_path / "home", "deployer", dry_run=False, to="my")


# --- install: loud disclosure + the doer actually renders write tools --------

@requires_symlinks
def test_install_discloses_doers_and_places_write_tools(tmp_path):
    from cohort.install import do_install_project

    repo = tmp_path / "repo"
    ppaths = _project(repo)
    _write_agent(ppaths, "deployer", DOER)
    report = do_install_project(repo)

    doers = report["doers"]
    assert len(doers) == 1
    assert doers[0]["name"] == "deployer"
    assert doers[0]["bash"] is True
    assert set(doers[0]["tools"]) == {"read", "edit", "bash"}  # canonical names

    placed = (repo / ".claude" / "agents" / "deployer.md").read_text(encoding="utf-8")
    assert "Edit" in placed and "Bash" in placed  # write tools survived to the placed file

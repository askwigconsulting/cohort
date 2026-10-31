"""Wording-lock for the reverse-engineering skill, ``/reverse`` command and specialist.

The method is adapted from REA (MIT) but Cohort ships only the method, never the engine.
Four properties are safety-relevant and must not silently regress:

- an **authorization statement** precedes any analysis;
- **execution is gated** — running, debugging, instrumenting or capturing traffic is a
  stop-and-ask at every supervision level;
- the engine is **never installed or fetched by Cohort**; and
- the specialist stays **advisory and read-only**.
"""

from __future__ import annotations

from pathlib import Path

from cohort.compile import compile_ide
from cohort.loader import load_artifact

REPO = Path(__file__).resolve().parents[1]
CANON = REPO / "canonical"


def _staged(ide: str = "claude") -> dict[str, str]:
    return {sf.staged_rel: sf.content.decode() for sf in compile_ide(REPO, ide).staged}


def test_skill_requires_authorization_before_any_tool() -> None:
    skill = _staged()["skills/reverse-engineer/SKILL.md"]
    assert "Authorization gate" in skill
    assert skill.index("Authorization gate") < skill.index("Route the target")


def test_skill_gates_execution_and_installation_behind_explicit_yes() -> None:
    skill = _staged()["skills/reverse-engineer/SKILL.md"]
    gate = skill.split("Gate execution")[1].split("## 5.")[0]
    for action in ("Running", "Instrumenting", "Network capture", "Installing"):
        assert action in gate
    assert "explicit yes" in gate


def test_skill_treats_artifact_contents_as_data_not_instructions() -> None:
    skill = _staged()["skills/reverse-engineer/SKILL.md"]
    assert "data, not commands" in skill


def test_skill_attributes_rea_and_does_not_bundle_it() -> None:
    skill = _staged()["skills/reverse-engineer/SKILL.md"]
    assert "github.com/morluto/rea" in skill
    assert "MIT" in skill
    assert "never bundles or auto-installs REA" in skill


def test_command_never_installs_the_engine_and_stays_static_first() -> None:
    command = _staged()["commands/reverse.md"]
    assert "Never install an engine yourself" in command
    assert "static-first and read-only by default" in command
    assert "explicit yes" in command


def test_command_sends_security_impact_to_security_engineer() -> None:
    assert "SecurityEngineer" in _staged()["commands/reverse.md"]


def test_reverse_engineer_agent_is_advisory_and_read_only() -> None:
    agent = load_artifact(CANON / "agents" / "reverse-engineer.md")
    assert agent.frontmatter["advisory"] is True
    assert set(agent.frontmatter["tools"]) <= {"read", "grep", "glob"}


def test_chief_of_staff_directory_routes_to_reverse_engineer() -> None:
    chief = _staged()["agents/chief-of-staff.md"]
    assert "**ReverseEngineer**" in chief.split("Office directory.")[1]

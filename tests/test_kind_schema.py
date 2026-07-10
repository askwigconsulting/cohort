"""P0-T2 per-kind schemas: required fields, enums, safety/scope invariants.

Behavioral (REVIEW GATE) + integration + unit tests for the six kinds.
"""

from __future__ import annotations

import pytest

from cohort.errors import (
    E010_MISSING_FIELD,
    E020_BAD_ENUM,
    E050_TYPE,
    E060_SAFETY_INVARIANT,
    E070_SCOPE_CONSTRAINT,
)
from cohort.schema import apply_defaults, validate_file
from conftest import INVALID, VALID, code_set, validate_text


def _doc(fields: dict[str, str]) -> str:
    body = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"---\n{body}\n---\nbody\n"


def _agent(**o) -> str:
    f = {
        "name": "artifact",
        "kind": "agent",
        "scope": "global",
        "description": "valid",
        "targets": "[all]",
        "department": "Ops",
        "advisory": "true",
    }
    f.update(o)
    return _doc(f)


def _command(**o) -> str:
    f = {
        "name": "artifact",
        "kind": "command",
        "scope": "project",
        "description": "valid",
        "targets": "[claude]",
        "invocation": "do-thing",
        "dry_run": "true",
    }
    f.update(o)
    return _doc(f)


def _hook(**o) -> str:
    f = {
        "name": "artifact",
        "kind": "hook",
        "scope": "global",
        "description": "valid",
        "targets": "[all]",
        "event": "session_start",
        "action": "do-thing",
    }
    f.update(o)
    return _doc(f)


def _context(**o) -> str:
    f = {
        "name": "project-context",
        "kind": "context",
        "scope": "project",
        "description": "valid",
        "targets": "[all]",
    }
    f.update(o)
    return _doc(f)


def _drop(builder, field, **o) -> str:
    """Build a doc then remove a line for ``field`` to simulate omission."""
    text = builder(**o)
    return "\n".join(ln for ln in text.split("\n") if not ln.startswith(f"{field}:")) + ""


# --- agent ------------------------------------------------------------------


def test_agent_without_department_is_e010():
    result = validate_text(_drop(_agent, "department"))
    assert any(e.code == E010_MISSING_FIELD and e.field == "department" for e in result.errors)


def test_agent_bad_topology_is_e020():
    assert E020_BAD_ENUM in code_set(_agent(topology="overlord"))


def test_agent_advisory_false_is_e060():
    assert E060_SAFETY_INVARIANT in code_set(_agent(advisory="false"))


def test_agent_omitting_advisory_defaults_true_and_passes():
    result = validate_text(_drop(_agent, "advisory"))
    assert result.status == "pass", [e.to_dict() for e in result.errors]


# --- model tier (#143): fail-closed schema validation ------------------------


def test_agent_bad_model_is_e020():
    assert E020_BAD_ENUM in code_set(_agent(model="opus"))


@pytest.mark.parametrize("tier", ["fast", "default", "top"])
def test_agent_valid_model_tier_passes(tier):
    result = validate_text(_agent(model=tier))
    assert result.status == "pass", [e.to_dict() for e in result.errors]


def test_agent_omitting_model_defaults_and_passes():
    result = validate_text(_drop(_agent, "model"))
    assert result.status == "pass", [e.to_dict() for e in result.errors]


# --- command ----------------------------------------------------------------


def test_command_without_invocation_is_e010():
    result = validate_text(_drop(_command, "invocation"))
    assert any(e.code == E010_MISSING_FIELD and e.field == "invocation" for e in result.errors)


def test_command_dry_run_false_is_e060():
    assert E060_SAFETY_INVARIANT in code_set(_command(dry_run="false"))


# --- hook -------------------------------------------------------------------


def test_hook_bad_event_is_e020():
    assert E020_BAD_ENUM in code_set(_hook(event="on_explosion"))


def test_hook_missing_action_is_e010():
    result = validate_text(_drop(_hook, "action"))
    assert any(e.code == E010_MISSING_FIELD and e.field == "action" for e in result.errors)


# --- context ----------------------------------------------------------------


def test_context_scope_global_is_e070():
    result = validate_text(_context(scope="global"), stem="project-context")
    assert any(e.code == E070_SCOPE_CONSTRAINT for e in result.errors)


def test_context_wrong_name_is_e070():
    # name must equal stem (so change both) but not equal 'project-context'.
    result = validate_text(_context(name="other-context"), stem="other-context")
    assert any(e.code == E070_SCOPE_CONSTRAINT and e.field == "name" for e in result.errors)


# --- valid fixtures ---------------------------------------------------------


def test_valid_agent_and_command_fixtures_clean():
    for path in [VALID / "agents" / "security-engineer.md", VALID / "commands" / "snapshot.md"]:
        result = validate_file(path)
        assert result.status == "pass", [e.to_dict() for e in result.errors]


def test_one_valid_fixture_of_every_kind_validates_clean():
    found_kinds = set()
    for path in sorted(VALID.rglob("*.md")):
        result = validate_file(path)
        assert result.status == "pass", (path, [e.to_dict() for e in result.errors])
        found_kinds.add(result.kind)
    assert found_kinds == {"agent", "skill", "command", "hook", "memory", "context"}


# --- integration: invalid fixtures → exactly their expected code ------------

EXPECTED_INVALID_CODES = {
    "non-advisory-agent.md": "E060_SAFETY_INVARIANT",
    "bad-targets-agent.md": "E040_TARGETS_INVALID",
    "Foo.md": "E030_NAME_MISMATCH",
    "mismatch-name.md": "E030_NAME_MISMATCH",
    "unknown-field.md": "E090_UNKNOWN_FIELD",
    "missing-department.md": "E010_MISSING_FIELD",
    "bad-kind.md": "E020_BAD_ENUM",
    "context-global.md": "E070_SCOPE_CONSTRAINT",
    "command-no-dryrun.md": "E060_SAFETY_INVARIANT",
    "broken-yaml.md": "E001_FRONTMATTER_PARSE",
    "targets-string-agent.md": "E050_TYPE",
}


@pytest.mark.parametrize("filename,expected", sorted(EXPECTED_INVALID_CODES.items()))
def test_each_invalid_fixture_yields_exactly_its_code(filename, expected):
    result = validate_file(INVALID / filename)
    produced = {e.code for e in result.errors}
    assert produced == {expected}, (filename, [e.to_dict() for e in result.errors])


# --- unit: defaults ---------------------------------------------------------


def test_defaults_applied_for_agent():
    out = apply_defaults({"name": "a", "kind": "agent", "department": "Ops"})
    assert out["topology"] == "specialist"
    assert out["advisory"] is True
    assert out["tools"] == []
    assert out["model"] == "default"
    assert out["version"] == "0.1.0"


def test_defaults_applied_for_command_and_memory():
    cmd = apply_defaults({"name": "a", "kind": "command", "invocation": "x"})
    assert cmd["dry_run"] is True
    mem = apply_defaults({"name": "a", "kind": "memory"})
    assert mem["priority"] == "normal"


def test_default_required_false_applied_per_arg():
    out = apply_defaults(
        {"name": "a", "kind": "command", "invocation": "x", "args": [{"name": "target"}]}
    )
    assert out["args"][0]["required"] is False


# --- unit: args shape, matcher optionality, R6 type-before-invariant -------


def test_command_arg_missing_name_is_e010():
    text = (
        "---\nname: artifact\nkind: command\nscope: project\ndescription: v\n"
        "targets: [claude]\ninvocation: do\nargs:\n  - description: no name here\n---\nbody\n"
    )
    assert E010_MISSING_FIELD in code_set(text)


def test_command_arg_wrong_type_is_e050():
    text = (
        "---\nname: artifact\nkind: command\nscope: project\ndescription: v\n"
        "targets: [claude]\ninvocation: do\nargs:\n  - name: 123\n---\nbody\n"
    )
    assert E050_TYPE in code_set(text)


def test_hook_matcher_is_optional():
    result = validate_text(_hook(matcher="Write|Edit"))
    assert result.status == "pass"
    result_no_matcher = validate_text(_hook())
    assert result_no_matcher.status == "pass"


def test_advisory_wrong_type_is_e050_not_e060():
    cs = code_set(_agent(advisory='"false"'))
    assert E050_TYPE in cs
    assert E060_SAFETY_INVARIANT not in cs


def test_dry_run_wrong_type_is_e050_not_e060():
    cs = code_set(_command(dry_run='"false"'))
    assert E050_TYPE in cs
    assert E060_SAFETY_INVARIANT not in cs

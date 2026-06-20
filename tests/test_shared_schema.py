"""P0-T1 shared schema: required fields, name↔stem, enums, targets, unknown, type.

Behavioral (REVIEW GATE) + integration tests for the shared-field contract.
"""

from __future__ import annotations

import pytest

from cohort.errors import (
    E010_MISSING_FIELD,
    E011_FIELD_LENGTH,
    E020_BAD_ENUM,
    E030_NAME_MISMATCH,
    E040_TARGETS_INVALID,
    E050_TYPE,
    E090_UNKNOWN_FIELD,
)
from cohort.schema import apply_defaults, validate_file
from conftest import VALID, code_set, codes, validate_text


def _agent(**overrides) -> str:
    fields = {
        "name": "artifact",
        "kind": "skill",
        "scope": "global",
        "description": "a valid description",
        "targets": "[all]",
    }
    fields.update(overrides)
    lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"---\n{lines}\n---\nbody\n"


# --- Required shared fields -------------------------------------------------


@pytest.mark.parametrize("field", ["name", "kind", "scope", "description", "targets"])
def test_missing_required_shared_field_is_e010(field):
    text = _agent()
    # Rebuild without the named field.
    base = {
        "name": "artifact",
        "kind": "skill",
        "scope": "global",
        "description": "a valid description",
        "targets": "[all]",
    }
    del base[field]
    body = "\n".join(f"{k}: {v}" for k, v in base.items())
    result = validate_text(f"---\n{body}\n---\nbody\n")
    e010 = [e for e in result.errors if e.code == E010_MISSING_FIELD]
    assert any(e.field == field for e in e010), result.errors


# --- name ↔ stem (E030, two variants) --------------------------------------


def test_name_fails_slug_pattern_is_e030_slug_variant():
    result = validate_text(_agent(name="Foo"), stem="Foo")
    e030 = [e for e in result.errors if e.code == E030_NAME_MISMATCH]
    assert len(e030) == 1
    assert e030[0].variant == "slug"


def test_name_not_equal_to_stem_is_e030_stem_variant():
    result = validate_text(_agent(name="other-name"), stem="mismatch-name")
    e030 = [e for e in result.errors if e.code == E030_NAME_MISMATCH]
    assert len(e030) == 1
    assert e030[0].variant == "stem"


# --- enums ------------------------------------------------------------------


def test_bad_kind_enum_is_e020():
    assert E020_BAD_ENUM in code_set(_agent(kind="wizard"))


def test_bad_scope_enum_is_e020():
    assert E020_BAD_ENUM in code_set(_agent(scope="universe"))


# --- targets ----------------------------------------------------------------


@pytest.mark.parametrize(
    "targets",
    ["[]", "[all, claude]", "[vscode]", "[claude, claude]"],
)
def test_invalid_targets_are_e040(targets):
    assert E040_TARGETS_INVALID in code_set(_agent(targets=targets))


def test_targets_as_string_is_e050_not_e040():
    cs = code_set(_agent(targets="all"))
    assert E050_TYPE in cs
    assert E040_TARGETS_INVALID not in cs


def test_targets_array_of_non_strings_is_e050():
    cs = code_set(_agent(targets="[1, 2]"))
    assert E050_TYPE in cs
    assert E040_TARGETS_INVALID not in cs


# --- unknown field & description --------------------------------------------


def test_unknown_top_level_field_is_e090():
    assert E090_UNKNOWN_FIELD in code_set(_agent(colour="blue"))


def test_empty_description_is_e010():
    assert E010_MISSING_FIELD in code_set(_agent(description='""'))


def test_whitespace_description_is_e010():
    assert E010_MISSING_FIELD in code_set(_agent(description='"   "'))


def test_overlong_description_is_e011():
    long = '"' + ("x" * 1025) + '"'
    assert E011_FIELD_LENGTH in code_set(_agent(description=long))


# --- valid fixtures pass; version defaults ---------------------------------


def test_valid_fixtures_produce_zero_errors():
    for path in sorted(VALID.rglob("*.md")):
        result = validate_file(path)
        assert result.status == "pass", (path, [e.to_dict() for e in result.errors])


def test_version_defaults_to_0_1_0_when_omitted():
    result = validate_text(_agent())
    assert result.status == "pass"
    normalized = apply_defaults({"name": "artifact", "kind": "skill"})
    assert normalized["version"] == "0.1.0"

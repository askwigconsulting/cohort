"""Unit tests for the JSON Schema files and the kind→directory map (R1)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from cohort.schema import KIND_DIRS, KINDS, discover_artifacts, schema_dir


@pytest.mark.parametrize("stem", ["shared", *KINDS])
def test_schema_files_are_valid_draft_2020_12(stem):
    schema = json.loads((schema_dir() / f"{stem}.json").read_text(encoding="utf-8"))
    # Raises SchemaError if the document is not a valid draft 2020-12 schema.
    Draft202012Validator.check_schema(schema)


def test_kind_dir_map_is_explicit_not_naive_plural():
    # memory must resolve to 'memories', never 'memorys'.
    assert KIND_DIRS["memory"] == "memories"
    assert set(KIND_DIRS) == set(KINDS)


def test_discovery_resolves_memory_under_memories(tmp_path):
    mem_dir = tmp_path / KIND_DIRS["memory"]
    mem_dir.mkdir(parents=True)
    target = mem_dir / "house-style.md"
    target.write_text(
        "---\nname: house-style\nkind: memory\nscope: global\n"
        "description: x\ntargets: [all]\n---\nbody\n",
        encoding="utf-8",
    )
    found = discover_artifacts(tmp_path)
    assert target in found
    assert str(found[0]).endswith("memories/house-style.md")

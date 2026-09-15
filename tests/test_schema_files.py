"""Unit tests for the JSON Schema files and the kind→directory map (R1)."""

from __future__ import annotations

import json
import os

import pytest
from conftest import requires_symlinks
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
    assert found[0].as_posix().endswith("memories/house-style.md")  # posix sep (Windows uses \)


# --- discovery layout (#297 stray .md) and symlink refusal (#285) -------------


def _hook(path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\nkind: hook\nscope: global\ndescription: x\n"
        "targets: [claude]\nevent: session_start\naction: cohort x\n---\nbody\n",
        encoding="utf-8",
    )


def test_discovery_is_restricted_to_known_kind_dirs_and_warns_about_strays(tmp_path, capsys):
    # A README in the personal layer, a notes dir, and a nested file inside a kind
    # dir are all strays: skipped with a stderr warning, never a compile failure.
    _hook(tmp_path / "hooks" / "kept.md", "kept")
    (tmp_path / "README.md").write_text("# my notes\n", encoding="utf-8")
    (tmp_path / "notes" / "todo.md").parent.mkdir()
    (tmp_path / "notes" / "todo.md").write_text("- x\n", encoding="utf-8")
    _hook(tmp_path / "hooks" / "nested" / "deep.md", "deep")
    found = discover_artifacts(tmp_path)
    assert found == [tmp_path / "hooks" / "kept.md"]
    err = capsys.readouterr().err
    for stray in ("README.md", "todo.md", "deep.md"):
        assert stray in err and "warning" in err
    assert "kept.md" not in err


def test_discovery_of_a_missing_root_is_empty(tmp_path):
    assert discover_artifacts(tmp_path / "absent") == []


@requires_symlinks
def test_discovery_refuses_a_symlinked_entry_but_follows_a_symlinked_root(tmp_path, capsys):
    # Link-mode installs make ~/.cohort/canonical itself a symlink: that ancestor
    # must keep working. The artifact's OWN entry being a symlink is refused — its
    # bytes live somewhere the pull delta never covers (#285).
    real = tmp_path / "real"
    _hook(real / "hooks" / "kept.md", "kept")
    _hook(tmp_path / "outside" / "evil.md", "evil")
    os.symlink(tmp_path / "outside" / "evil.md", real / "hooks" / "evil.md")
    link = tmp_path / "link"
    os.symlink(real, link)
    found = discover_artifacts(link)
    assert found == [link / "hooks" / "kept.md"]
    err = capsys.readouterr().err
    assert "evil.md" in err and "symlink" in err


def test_discovery_matches_the_md_suffix_case_insensitively(tmp_path):
    # The old rglob("*.md") was case-insensitive on Windows; a .MD artifact keeps loading.
    _hook(tmp_path / "hooks" / "upper.MD", "upper")
    assert discover_artifacts(tmp_path) == [tmp_path / "hooks" / "upper.MD"]

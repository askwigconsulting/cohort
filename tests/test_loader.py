"""P0-T1 loader: frontmatter/body split and E001 parse failures.

Behavioral (REVIEW GATE) tests for loading, plus unit tests for splitter edge
cases (CRLF, trailing whitespace, body containing '---', empty body, BOM).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cohort.errors import E001_FRONTMATTER_PARSE
from cohort.frontmatter import dump_frontmatter
from cohort.loader import (
    FrontmatterError,
    StrictSafeLoader,
    load_artifact_text,
    parse_frontmatter,
    split_frontmatter,
)

REPO = Path(__file__).resolve().parents[1]

WELL_FORMED = """---
name: x
kind: skill
scope: global
description: hi
targets: [all]
---
body line one
body line two
"""


# --- Behavioral (REVIEW GATE) ----------------------------------------------


def test_well_formed_file_splits_into_mapping_and_body():
    result = load_artifact_text(WELL_FORMED, name_stem="x")
    assert result.load_error is None
    assert isinstance(result.frontmatter, dict)
    assert result.frontmatter["name"] == "x"
    assert result.body == "body line one\nbody line two\n"


def test_no_delimiters_fails_e001():
    result = load_artifact_text("just text, no frontmatter\n", name_stem="x")
    assert result.load_error is not None
    assert result.load_error.code == E001_FRONTMATTER_PARSE


def test_frontmatter_that_is_a_list_fails_e001():
    text = "---\n- a\n- b\n---\nbody\n"
    result = load_artifact_text(text, name_stem="x")
    assert result.load_error is not None
    assert result.load_error.code == E001_FRONTMATTER_PARSE


def test_unterminated_frontmatter_fails_e001():
    text = "---\nname: x\nkind: skill\n\nbody without closing delimiter\n"
    result = load_artifact_text(text, name_stem="x")
    assert result.load_error is not None
    assert result.load_error.code == E001_FRONTMATTER_PARSE


def test_invalid_yaml_fails_e001():
    text = "---\nname: : : bad\n: -\n---\nbody\n"
    result = load_artifact_text(text, name_stem="x")
    assert result.load_error is not None
    assert result.load_error.code == E001_FRONTMATTER_PARSE


# --- Unit: splitter edge cases ---------------------------------------------


def test_split_handles_crlf():
    fm, body = split_frontmatter("---\r\nname: x\r\n---\r\nbody\r\n")
    assert "name: x" in fm
    assert body == "body\n"


def test_split_handles_leading_bom():
    fm, body = split_frontmatter("﻿---\nname: x\n---\nbody\n")
    assert "name: x" in fm
    assert body == "body\n"


def test_split_tolerates_trailing_whitespace_on_delimiters():
    fm, body = split_frontmatter("---   \nname: x\n---  \nbody\n")
    assert "name: x" in fm
    assert body == "body\n"


def test_body_may_contain_triple_dash():
    text = "---\nname: x\n---\nintro\n---\nstill body\n"
    fm, body = split_frontmatter(text)
    assert "name: x" in fm
    assert body == "intro\n---\nstill body\n"


def test_frontmatter_only_yields_empty_body():
    fm, body = split_frontmatter("---\nname: x\n---\n")
    assert "name: x" in fm
    assert body == ""


def test_frontmatter_only_no_trailing_newline_yields_empty_body():
    fm, body = split_frontmatter("---\nname: x\n---")
    assert body == ""


def test_missing_opening_delimiter_raises():
    with pytest.raises(FrontmatterError):
        split_frontmatter("name: x\n---\nbody\n")


def test_empty_frontmatter_is_not_a_mapping():
    with pytest.raises(FrontmatterError):
        parse_frontmatter("")


# --- Strict loader: duplicate keys, parse parity, C parser ------------------


def test_duplicate_top_level_key_fails_e001_naming_the_key():
    text = "---\nname: x\nkind: hook\naction: first\naction: second\n---\nbody\n"
    result = load_artifact_text(text, name_stem="x")
    assert result.load_error is not None
    assert result.load_error.code == E001_FRONTMATTER_PARSE
    assert "'action'" in result.load_error.message
    assert result.frontmatter is None and result.body is None


def test_duplicate_nested_key_fails_e001():
    text = "---\nname: x\nargs:\n  - name: a\n    name: b\n---\nbody\n"
    result = load_artifact_text(text, name_stem="x")
    assert result.load_error is not None
    assert result.load_error.code == E001_FRONTMATTER_PARSE
    assert "'name'" in result.load_error.message


def test_merge_key_override_is_not_a_duplicate():
    # YAML merge (`<<`) legitimately re-states a key; only explicit repeats are rejected.
    fm = parse_frontmatter("base: &b\n  a: 1\n  c: 3\nderived:\n  <<: *b\n  a: 2\n")
    assert fm["derived"] == {"a": 2, "c": 3}


@pytest.mark.skipif(not yaml.__with_libyaml__, reason="libyaml not available")
def test_loader_uses_the_c_parser_when_libyaml_is_available():
    assert issubclass(StrictSafeLoader, yaml.CSafeLoader)


def test_strict_loader_parses_every_canonical_artifact_like_safe_load():
    # The strict loader must change nothing but duplicate handling: dates, yes/no,
    # octal-looking strings and every other scalar resolve exactly as safe_load does.
    for path in sorted((REPO / "canonical").rglob("*.md")):
        fm_text, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        assert parse_frontmatter(fm_text) == yaml.safe_load(fm_text), path


# --- Splitter: the closing delimiter is at column 0 only --------------------


def test_indented_triple_dash_is_not_a_closing_delimiter():
    fm, body = split_frontmatter("---\nnote: 'a\n\n  ---\n\n  b'\n---\nBODY\n")
    assert "---" in fm
    assert body == "BODY\n"


@pytest.mark.parametrize(
    "key,value",
    [
        ("author", "Jonathan --- Askwig"),
        ("branch", "feat/---break"),
        ("agent", "a\n---\nb"),
        ("note", "---"),
        ("ts", "2026-06-01T12:00:00Z"),
        ("author", "O'Brien: the \"boss\""),
    ],
)
def test_dumped_frontmatter_round_trips_through_the_loader(key, value):
    # `cohort feedback --agent $'a\n---\nb'` must produce a loadable artifact: what
    # `dump_frontmatter` emits, the loader reads back unchanged (#299 item 4).
    raw = dump_frontmatter([(key, value), ("x", "1")]) + "BODY\n"
    result = load_artifact_text(raw, name_stem="t")
    assert result.load_error is None
    assert result.frontmatter[key] == value
    assert result.body == "BODY\n"

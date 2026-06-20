"""P0-T1 loader: frontmatter/body split and E001 parse failures.

Behavioral (REVIEW GATE) tests for loading, plus unit tests for splitter edge
cases (CRLF, trailing whitespace, body containing '---', empty body, BOM).
"""

from __future__ import annotations

import pytest

from cohort.errors import E001_FRONTMATTER_PARSE
from cohort.loader import (
    FrontmatterError,
    load_artifact_text,
    parse_frontmatter,
    split_frontmatter,
)

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

"""Parity guard for the quick-reference doc.

The reference is generated from canonical, so it must never be edited by hand and must
never go stale. These tests fail CI when a command or skill is added, renamed, or has its
description changed without regenerating - the fix is always `cohort reference`, which
rewrites docs/quick-reference.html and re-renders the PDF.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from cohort import reference
from cohort.engines import ENGINES
from cohort.loader import load_artifact

REPO = Path(__file__).resolve().parents[1]
_HTML = REPO / "docs" / "quick-reference.html"


def test_reference_html_matches_canonical() -> None:
    committed = _HTML.read_text(encoding="utf-8")
    generated = reference.build_html(REPO)
    assert committed == generated, (
        "docs/quick-reference.html is out of date with canonical — a command or skill was "
        "added, renamed, or re-described. Run `cohort reference` to regenerate the HTML and "
        "PDF, then commit both."
    )


def test_reference_lists_every_command_and_skill() -> None:
    committed = _HTML.read_text(encoding="utf-8")
    missing = [n for n in sorted(reference.canonical_names(REPO)) if n not in committed]
    assert not missing, (
        f"these commands/skills are missing from the quick-reference: {missing} — "
        "run `cohort reference`"
    )


def test_reference_pdf_exists() -> None:
    pdf = REPO / "docs" / "quick-reference.pdf"
    assert pdf.exists() and pdf.stat().st_size > 0, (
        "docs/quick-reference.pdf is missing — run `cohort reference` on a machine with "
        "Chrome to render it"
    )


def test_build_html_is_deterministic() -> None:
    assert reference.build_html(REPO) == reference.build_html(REPO)


_ROW = re.compile(r'<span class="cmd">/([a-z0-9-]+)</span><span class="desc">([^<]*)</span>')


def test_reference_uses_each_commands_own_description_not_an_args() -> None:
    # The old line scanner took the LAST `description:` before the closing `---`, so a
    # nested `args[].description` won for 10 of 21 commands (#297 item 1).
    rendered = {name: html.unescape(desc) for name, desc in _ROW.findall(reference.build_html(REPO))}
    for path in sorted((REPO / "canonical" / "commands").glob("*.md")):
        fm = load_artifact(path).frontmatter
        assert rendered[fm["name"]] == fm["description"], path.name
        for arg in fm.get("args", []):
            assert rendered[fm["name"]] != arg.get("description"), path.name


def test_reference_fails_loudly_on_an_unloadable_artifact(tmp_path: Path) -> None:
    (tmp_path / "canonical" / "commands").mkdir(parents=True)
    (tmp_path / "canonical" / "skills").mkdir()
    (tmp_path / "canonical" / "commands" / "broken.md").write_text("---\nname: : bad\n---\n")
    with pytest.raises(ValueError, match="broken.md"):
        reference.build_html(tmp_path)


def test_reference_html_declares_its_language() -> None:
    assert '<html lang="en">' in reference.build_html(REPO)


def test_reference_tier_list_comes_from_the_engine_registry() -> None:
    registered = [t for spec in ENGINES.values() for t in spec.model_tiers]
    assert list(reference.engine_tiers()) == list(dict.fromkeys(registered))
    assert "Tier: " + " | ".join(reference.engine_tiers()) + "." in reference.build_html(REPO)

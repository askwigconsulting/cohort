"""Parity guard for the quick-reference doc.

The reference is generated from canonical, so it must never be edited by hand and must
never go stale. These tests fail CI when a command or skill is added, renamed, or has its
description changed without regenerating - the fix is always `cohort reference`, which
rewrites docs/quick-reference.html and re-renders the PDF.
"""

from __future__ import annotations

from pathlib import Path

from cohort import reference

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

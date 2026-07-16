"""Doc-parity lint (`cohort lint`): human docs must not drift from canonical.

Two guards:
- the repo's own docs are parity-clean right now (so a future edit that lets a
  count drift — the "18-agent" bug this lint exists to catch — fails CI); and
- the lint logic itself flags a real mismatch and derives counts from the
  filesystem rather than any stored number.
"""

from __future__ import annotations

from pathlib import Path

from cohort.lint import canonical_counts, run_lint

REPO = Path(__file__).resolve().parents[1]


def test_repo_docs_are_parity_clean():
    findings = run_lint(REPO)
    assert findings == [], "doc/canonical drift:\n" + "\n".join(
        f"  {f.file}:{f.line}: {f.message}" for f in findings
    )


def test_counts_are_derived_from_the_filesystem():
    counts = canonical_counts(REPO)
    # Derived, not stored: each equals the actual number of canonical .md files.
    assert counts["agent"] == len(list((REPO / "canonical" / "agents").glob("*.md")))
    assert counts["command"] == len(list((REPO / "canonical" / "commands").glob("*.md")))
    assert set(counts) == {"agent", "skill", "command", "hook", "memory"}


def test_lint_flags_a_wrong_count(tmp_path):
    # A minimal fake repo: one canonical agent, but a README claiming two.
    (tmp_path / "canonical" / "agents").mkdir(parents=True)
    (tmp_path / "canonical" / "agents" / "solo.md").write_text("x", encoding="utf-8")
    for sub in ("skills", "commands", "hooks", "memories"):
        (tmp_path / "canonical" / sub).mkdir(parents=True)
    (tmp_path / "README.md").write_text("The 2-agent roster ships here.\n", encoding="utf-8")

    findings = run_lint(tmp_path)
    assert len(findings) == 1
    assert findings[0].file == "README.md"
    assert findings[0].line == 1
    assert "2-agent" in findings[0].message and "1 agent" in findings[0].message


def test_lint_ignores_spaced_non_count_phrases(tmp_path):
    # "10 agents in flight" is a cap, not a roster count — must NOT be flagged.
    (tmp_path / "canonical" / "agents").mkdir(parents=True)
    (tmp_path / "canonical" / "agents" / "a.md").write_text("x", encoding="utf-8")
    for sub in ("skills", "commands", "hooks", "memories"):
        (tmp_path / "canonical" / sub).mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        "Fan out with max 10 agents in flight; the 1-agent roster is tiny.\n",
        encoding="utf-8",
    )
    assert run_lint(tmp_path) == []

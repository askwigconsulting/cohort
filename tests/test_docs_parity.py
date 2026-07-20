"""Doc-parity lint (`cohort lint`): human docs must not drift from canonical.

Two guards:
- the repo's own docs are parity-clean right now (so a future edit that lets a
  count drift — the "18-agent" bug this lint exists to catch — fails CI); and
- the lint logic itself flags a real mismatch and derives counts from the
  filesystem rather than any stored number.
"""

from __future__ import annotations

from pathlib import Path

from cohort.adapters.claude import _MODEL_MAP
from cohort.lint import _model_tier_findings, _parse_tier_table, canonical_counts, run_lint

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

    counts = [f for f in run_lint(tmp_path) if "canonical has" in f.message]
    assert len(counts) == 1
    assert counts[0].file == "README.md"
    assert counts[0].line == 1
    assert "2-agent" in counts[0].message and "1 agent" in counts[0].message


def test_model_tiers_doc_matches_the_renderer_code():
    # The documented agent-tier table is the single source of truth and must
    # equal the renderer's actual mapping — this is the guard against the doc
    # silently lying about what a tier compiles to.
    doc = (REPO / "docs" / "model-tiers.md").read_text(encoding="utf-8")
    documented = _parse_tier_table(doc, "Agent model tier")
    assert documented == _MODEL_MAP
    # And the repo is clean on the model-tier checks specifically.
    assert _model_tier_findings(REPO) == []


def test_lint_flags_model_tier_doc_drift_from_code(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "canonical" / "commands").mkdir(parents=True)
    (tmp_path / "canonical" / "commands" / "crew.md").write_text(
        "fable opus sonnet haiku", encoding="utf-8"
    )
    # A doc whose agent-tier table disagrees with the real _MODEL_MAP.
    (tmp_path / "docs" / "model-tiers.md").write_text(
        "## Agent model tier\n\n| tier | model |\n|---|---|\n| fast | opus |\n\n"
        "## Orchestration routing tier\n\n| tier | model |\n|---|---|\n| fable | Fable |\n",
        encoding="utf-8",
    )
    findings = _model_tier_findings(tmp_path)
    assert any("disagrees with renderer" in f.message for f in findings)


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
    # No count finding — "10 agents in flight" is a cap, and "1-agent" is correct.
    assert [f for f in run_lint(tmp_path) if "canonical has" in f.message] == []

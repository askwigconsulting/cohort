"""Wording-lock for the operational-hard-limits memory.

Blast-radius limits (no destructive data ops, PR-only changes, no force-push,
secrets never move) are safety-critical and compile into every session's memory
corpus. Subagents don't inherit memories, so an orchestrator must restate the
relevant limits per worker — the memory says so and this locks it.
"""

from __future__ import annotations

from pathlib import Path

from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]


def _corpus() -> str:
    staged = {sf.staged_rel: sf.content for sf in compile_ide(REPO, "claude").staged}
    return staged["cohort/CLAUDE.cohort.md"].decode("utf-8")


def test_hard_limits_compiled_into_the_memory_corpus():
    corpus = _corpus()
    for rule in (
        "No destructive data operations",
        "never `--force`",
        "Secrets never move",
        "Changes land through review",
    ):
        assert rule in corpus, f"hard-limit missing from corpus: {rule}"


def test_hard_limits_require_per_worker_restatement():
    # Subagents don't inherit memories — a coordinator must state the limits
    # in each worker's prompt.
    corpus = _corpus()
    assert "a worker does not inherit this memory" in corpus

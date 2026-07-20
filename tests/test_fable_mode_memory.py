"""Wording-lock for the fable-mode memory and its /crew wiring.

Fable mode is the five-gate operational discipline non-Fable models (chiefly
Opus) apply so they think and act like Fable. Two things must not silently
regress:

- the **five gates** themselves, compiled into every instance's memory corpus;
- the **prompt-embedding rule** — subagents don't inherit memories, so the
  coordinator must state the gates verbatim in each non-fable worker's prompt.

If the wording is reworded, update these assertions deliberately.
"""

from __future__ import annotations

from pathlib import Path

from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]


def _staged() -> dict[str, str]:
    return {
        sf.staged_rel: sf.content.decode("utf-8")
        for sf in compile_ide(REPO, "claude").staged
    }


def test_fable_mode_locks_the_five_gates_in_the_memory_corpus():
    corpus = _staged()["cohort/CLAUDE.cohort.md"]
    for gate in (
        "Scope before you work",
        "Evidence before reasoning",
        "Reason adversarially",
        "Verify before declaring done",
        "Calibrate and report",
    ):
        assert gate in corpus, f"gate missing from memory corpus: {gate}"


def test_fable_mode_requires_gates_embedded_in_worker_prompts():
    # Subagents don't inherit memories — the corpus and the crew command
    # must both carry the embedding rule.
    corpus = _staged()["cohort/CLAUDE.cohort.md"]
    assert "does not inherit this memory automatically" in corpus
    body = _staged()["commands/crew.md"]
    assert "Fable-mode five gates" in body
    assert "does not inherit the `fable-mode`" in body


def test_crew_opus_fallback_coordinator_adopts_fable_mode():
    body = _staged()["commands/crew.md"]
    assert "operating in **Fable\nmode**" in body or "operating in **Fable mode**" in body


def test_fable_mode_scope_gate_carries_the_kickback_rule():
    # Gate 1 is where a worker self-assesses fit: hand the task back with a
    # specific reason rather than ship a plausible-but-uncertain attempt.
    flat = " ".join(_staged()["cohort/CLAUDE.cohort.md"].split())
    assert "check you're the right fit" in flat
    assert "Hand it back to the coordinator with a specific reason" in flat
    assert "never a bare \"too hard\"" in flat  # the abuse guard

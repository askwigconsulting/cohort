"""Compaction memory circuit: pre-compact capture + post-compact recall.

Compaction is harness-side — no model turn runs between "context full" and
"summary replaces context" — so the circuit has two halves:

- ``pre-compact-capture`` (PreCompact → ``cohort session-capture``): the
  deterministic record written before the squeeze, same opt-in as session end;
- ``post-compact-memory`` (SessionStart matcher ``compact`` → ``cohort
  compact-recall``): stdout injected into the fresh context right after
  compaction, instructing the model to commit the session's critical parts to
  durable memory. SessionStart/compact is the doc-verified injection channel;
  PreCompact stdout influencing the summary is NOT documented, which is why the
  recall rides the post-compaction event.

These tests lock the rendered wiring and the recall wording.
"""

from __future__ import annotations

import json
from pathlib import Path

from cohort.cli import COMPACT_RECALL_TEXT
from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]


def _hooks_fragment() -> dict:
    staged = {sf.staged_rel: sf.content for sf in compile_ide(REPO, "claude").staged}
    rel = ".merge/settings.hooks.json"
    assert rel in staged, f"hooks fragment missing; got {sorted(staged)}"
    return json.loads(staged[rel].decode("utf-8"))


def _entries(fragment: dict, event: str) -> list[tuple[str, str]]:
    """Flatten one event's entries to (matcher, command) pairs."""
    return [
        (group.get("matcher", ""), hook["command"])
        for group in fragment.get("hooks", {}).get(event, fragment.get(event, []))
        for hook in group["hooks"]
    ]


def test_pre_compact_capture_renders_on_precompact():
    fragment = _hooks_fragment()
    assert ("", "cohort session-capture") in _entries(fragment, "PreCompact")


def test_post_compact_recall_renders_on_sessionstart_compact_matcher():
    fragment = _hooks_fragment()
    assert ("compact", "cohort compact-recall") in _entries(fragment, "SessionStart")


def test_compact_recall_text_instructs_a_memory_commit():
    assert "commit the critical parts" in COMPACT_RECALL_TEXT
    assert "durable memory" in COMPACT_RECALL_TEXT
    assert "decisions made and why" in COMPACT_RECALL_TEXT
    assert "in-flight work state" in COMPACT_RECALL_TEXT
    # Recall must not re-record what's already saved.
    assert "Skip anything already recorded" in COMPACT_RECALL_TEXT

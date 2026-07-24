"""#23: canonical hook-event → IDE event-name mapping (Cursor / Codex / Copilot).

Doc-verified 2026-07-06 against the official docs (cursor.com/docs/hooks,
developers.openai.com/codex/hooks), and 2026-07-24 for Copilot
(docs.github.com/en/copilot/reference/hooks-reference). Cursor and Copilot events
are both camelCase but are *independently verified, distinct* vocabularies (they
happen to overlap on some names, e.g. ``sessionStart``); Codex events are
PascalCase and a different vocabulary (mirroring Claude Code). These tests lock the
maps so none can be confused for another — the bug this originally closed was the
Codex map reusing Cursor's camelCase names, which Codex would not recognize.
"""

from __future__ import annotations

from cohort.adapters import codex, copilot, cursor
from cohort.ir import build_ir
from cohort.schema import kind_schema

CANONICAL_EVENTS = tuple(kind_schema("hook")["properties"]["event"]["enum"])

# Official valid event names (verified 2026-07-06 against the docs above).
CURSOR_VALID = {
    "sessionStart", "sessionEnd", "preToolUse", "postToolUse", "postToolUseFailure",
    "subagentStart", "subagentStop", "beforeShellExecution", "afterShellExecution",
    "beforeMCPExecution", "afterMCPExecution", "beforeReadFile", "afterFileEdit",
    "beforeSubmitPrompt", "preCompact", "stop", "afterAgentResponse", "afterAgentThought",
    "beforeTabFileRead", "afterTabFileEdit", "workspaceOpen",
}
CODEX_VALID = {
    "SessionStart", "SubagentStart", "PreToolUse", "PermissionRequest", "PostToolUse",
    "PreCompact", "PostCompact", "UserPromptSubmit", "SubagentStop", "Stop",
}
# Verified 2026-07-24 against docs.github.com/en/copilot/reference/hooks-reference.
COPILOT_VALID = {
    "sessionStart", "sessionEnd", "userPromptSubmitted", "preToolUse", "postToolUse",
    "postToolUseFailure", "errorOccurred", "agentStop", "subagentStart", "subagentStop",
    "preCompact", "postCompact", "PermissionRequest",
}


def _hook_ir(event: str, name: str = "h"):
    return build_ir(
        {
            "name": name, "kind": "hook", "scope": "global", "description": "d",
            "targets": ["all"], "event": event, "action": "cohort x",
        },
        "body",
        None,
    )


def test_every_canonical_event_maps_for_both_adapters():
    # No KeyError at render time: the renderer indexes HOOK_EVENT_MAP[event] directly.
    for event in CANONICAL_EVENTS:
        assert event in cursor.HOOK_EVENT_MAP, f"cursor missing {event}"
        assert event in codex.HOOK_EVENT_MAP, f"codex missing {event}"
        assert event in copilot.HOOK_EVENT_MAP, f"copilot missing {event}"


def test_cursor_event_names_are_official_camelcase():
    for event in CANONICAL_EVENTS:
        name = cursor.HOOK_EVENT_MAP[event]
        assert name in CURSOR_VALID, f"cursor {event}->{name} is not an official event"
        assert name[0].islower(), f"cursor {event}->{name} should be camelCase"


def test_codex_event_names_are_official_pascalcase():
    for event in CANONICAL_EVENTS:
        name = codex.HOOK_EVENT_MAP[event]
        assert name in CODEX_VALID, f"codex {event}->{name} is not an official event"
        # Regression guard for the fixed bug: Codex must never carry Cursor camelCase.
        assert name[0].isupper(), f"codex {event}->{name} should be PascalCase"


def test_cursor_fragment_uses_cursors_flat_versioned_schema():
    # Cursor: top-level `version` + a FLAT handler array per event (verified against
    # cursor.com/docs/hooks). Distinct from Codex — the two must not be unified.
    fragment = cursor.render_hooks_fragment(
        [_hook_ir("session_start"), _hook_ir("post_command", "c")]
    )
    assert fragment["version"] == 1
    assert set(fragment["hooks"]) == {"sessionStart", "afterShellExecution"}
    assert fragment["hooks"]["sessionStart"] == [{"type": "command", "command": "cohort x"}]


def test_codex_fragment_uses_codex_matcher_group_schema():
    # Codex: NO `version`; event → matcher groups → nested `hooks` handler array
    # (verified against developers.openai.com/codex/hooks).
    fragment = codex.render_hooks_fragment(
        [_hook_ir("session_start"), _hook_ir("session_end", "e")]
    )
    assert "version" not in fragment
    assert set(fragment["hooks"]) == {"SessionStart", "Stop"}
    assert fragment["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "cohort x"}]}
    ]


def test_copilot_event_names_are_official_camelcase():
    for event in CANONICAL_EVENTS:
        name = copilot.HOOK_EVENT_MAP[event]
        assert name in COPILOT_VALID, f"copilot {event}->{name} is not an official event"
        assert name[0].islower(), f"copilot {event}->{name} should be camelCase"


def test_copilot_fragment_uses_its_own_dedicated_file():
    # Copilot: `{version, hooks: {event: [{type, command}]}}` — but unlike Cursor's
    # single shared file, this fragment IS the whole file (verified against
    # docs.github.com/en/copilot/reference/hooks-reference): Copilot loads every
    # *.json under hooks/ independently, so Cohort's hooks never need a merge op.
    fragment = copilot.render_hooks_fragment(
        [_hook_ir("session_start"), _hook_ir("post_command", "c")]
    )
    assert fragment["version"] == 1
    assert set(fragment["hooks"]) == {"sessionStart", "postToolUse"}
    assert fragment["hooks"]["sessionStart"] == [{"type": "command", "command": "cohort x"}]

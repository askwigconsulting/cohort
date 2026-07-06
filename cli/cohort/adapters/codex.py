"""The Codex renderer (P7-T1).

Verified layout (OpenAI Codex CLI, mid-2026, doc-cited):
- agent  → ``.codex/agents/<name>.toml``        (per-file subagent, TOML)
- skill  → ``.agents/skills/<name>/SKILL.md``    (note ``.agents/``, NOT ``.codex/``)
- command→ **declared gap** (Codex deprecates custom prompts in favor of skills)
- hook   → ``.codex/hooks.json``                 (JSON → key-merge)
- memory → ``.codex/AGENTS.md``                  (markdown → managed-block)

``dest_root = base`` and the full subpath is encoded in each staged_rel, because
Codex's roots aren't uniform (``.codex/`` vs ``.agents/``).

Advisory is enforced mechanically via ``sandbox_mode = "read-only"`` (doc-
confirmed) — the Codex analogue of Claude's tool-strip, not prose-only.

Hook-event names verified 2026-07-06 against the official Codex docs
(developers.openai.com/codex/hooks): Codex events are **PascalCase** and a
different vocabulary than Cursor's camelCase — they mirror Claude Code's scheme
(`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, …). ‹verify› still open
against a real install: the exact `.codex/hooks.json` wrapper and whether Codex
reads per-file subagents at `.codex/agents/<name>.toml` (undocumented — Codex may
route subagents through skills). Codex/Cursor stay experimental; shipped hooks
target `[claude]`, so these paths are latent until a codex/cursor hook is authored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..ir import IRArtifact
from .base import MergeTarget
from .claude import (
    OFFICE_DIRECTORY_MARKER,
    StagedFile,
    _assemble,
    _frontmatter,
    render_office_directory,
    render_memory_corpus,
)

MERGE_SUBDIR = ".merge"
AGENTS_IMPORT_REL = f"{MERGE_SUBDIR}/codex-agents-md.md"
HOOKS_FRAGMENT_REL = f"{MERGE_SUBDIR}/codex-hooks.json"

# canonical hook event → Codex event name (PascalCase). Verified 2026-07-06 against
# developers.openai.com/codex/hooks. Codex's vocabulary has no session-end or shell-
# specific events: session_end maps to Stop (the nearest terminal event) and the
# command events fold into the tool-use events, as Codex has no separate shell hook.
# on_stale is a Cohort concept with no Codex analogue → SessionStart.
HOOK_EVENT_MAP = {
    "session_start": "SessionStart",
    "session_end": "Stop",
    "pre_write": "PreToolUse",
    "post_write": "PostToolUse",
    "pre_command": "PreToolUse",
    "post_command": "PostToolUse",
    "on_stale": "SessionStart",
}


def _toml_basic(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_agent(ir: IRArtifact, directory: Optional[str] = None) -> StagedFile:
    label = ir.display_name or ir.name
    dept = ir.fields.get("department", "")
    topology = ir.fields.get("topology", "specialist")
    header = f"> **{label}** — {dept} · {topology} (advisory office agent)"
    body = ir.body.strip()
    if ir.fields.get("topology") == "generalist":
        body = body.replace(OFFICE_DIRECTORY_MARKER, directory or "")
    instructions = f"{header}\n\n{body}"
    lines = [
        f"name = {_toml_basic(ir.name)}",
        f"description = {_toml_basic(ir.description)}",
    ]
    # Advisory is enforced mechanically, not just in prose: Codex subagents honor
    # sandbox_mode = "read-only" (doc-confirmed), the Codex analogue of Claude's
    # tool-strip. Every roster agent is advisory.
    if ir.fields.get("advisory", True):
        lines.append('sandbox_mode = "read-only"')
    # TOML literal (''') needs no escaping; our bodies never contain '''.
    lines += ["developer_instructions = '''", instructions, "'''"]
    return StagedFile(f".codex/agents/{ir.name}.toml", ("\n".join(lines) + "\n").encode("utf-8"))


def render_skill(ir: IRArtifact) -> StagedFile:
    fm = _frontmatter([("name", ir.name), ("description", ir.description)])
    body = ir.body.strip()
    triggers = ir.fields.get("triggers") or []
    if triggers:
        body = f"{body}\n\n## When to use\nUse when: {', '.join(triggers)}."
    return StagedFile(f".agents/skills/{ir.name}/SKILL.md", _assemble(fm, body).encode("utf-8"))


def render_hooks_fragment(hook_irs: list[IRArtifact]) -> dict:
    hooks: dict[str, list] = {}
    for ir in sorted(hook_irs, key=lambda i: i.name):
        event = HOOK_EVENT_MAP[ir.fields["event"]]
        hooks.setdefault(event, []).append(
            {"type": "command", "command": ir.fields["action"]}
        )
    return {"version": 1, "hooks": hooks}


class CodexRenderer:
    ide = "codex"
    supported_kinds = frozenset({"agent", "skill", "hook", "memory"})  # command → gap
    merge_targets = (
        MergeTarget(AGENTS_IMPORT_REL, ".codex/AGENTS.md", "block"),
        MergeTarget(HOOKS_FRAGMENT_REL, ".codex/hooks.json", "json"),
    )

    def dest_root(self, base: Path) -> Path:
        return base

    def matches(self, ir: IRArtifact) -> bool:
        return ir.targets_ide(self.ide)

    def compile(self, irs: list[IRArtifact], project_tier: bool = False) -> tuple[list[StagedFile], list[str]]:
        matched = [ir for ir in irs if self.matches(ir)]
        specialists = [
            ir for ir in matched if ir.kind == "agent" and ir.fields.get("topology") == "specialist"
        ]
        directory = render_office_directory(specialists)
        staged: list[StagedFile] = []
        skipped: list[str] = []
        hook_irs: list[IRArtifact] = []
        memory_irs: list[IRArtifact] = []
        for ir in irs:
            if not self.matches(ir):
                skipped.append(ir.name)
            elif ir.kind == "agent":
                staged.append(render_agent(ir, directory))
            elif ir.kind == "skill":
                staged.append(render_skill(ir))
            elif ir.kind == "hook":
                hook_irs.append(ir)
            elif ir.kind == "memory":
                memory_irs.append(ir)
            else:  # command (declared gap), context (handled by `cohort init`)
                skipped.append(ir.name)
        if memory_irs:
            corpus = render_memory_corpus(memory_irs)
            staged.append(StagedFile(AGENTS_IMPORT_REL, corpus.encode("utf-8")))
        if hook_irs:
            fragment = render_hooks_fragment(hook_irs)
            staged.append(
                StagedFile(HOOKS_FRAGMENT_REL, (json.dumps(fragment, indent=2) + "\n").encode("utf-8"))
            )
        return staged, skipped

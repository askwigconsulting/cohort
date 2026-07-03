"""The Cursor renderer (P7-T2).

Verified layout (Cursor, mid-2026, doc-cited):
- agent  → ``.cursor/agents/<name>.md``      (MD + frontmatter; ``readonly: true`` for advisory)
- skill  → ``.cursor/skills/<name>/SKILL.md``
- command→ ``.cursor/commands/<name>.md``    (plain markdown, no frontmatter)
- hook   → ``.cursor/hooks.json``            (JSON → key-merge)
- memory → ``.cursor/rules/cohort-memories.mdc`` (Cohort-owned rule file → clean 1:1 link)

Cursor's memory home is a Cohort-OWNED rule file, so it's a clean link (no
merge); only ``hooks.json`` (shared) needs the merge op. ``dest_root = base``;
full subpaths in staged_rel.

‹verify› before golden-lock: the exact Cursor hook-event names, and whether
Cursor commands now accept frontmatter (currently rendered as plain markdown).
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
HOOKS_FRAGMENT_REL = f"{MERGE_SUBDIR}/cursor-hooks.json"
MEMORIES_REL = ".cursor/rules/cohort-memories.mdc"

# canonical hook event → Cursor event name. ‹verify› exact names before golden-lock.
HOOK_EVENT_MAP = {
    "session_start": "sessionStart",
    "session_end": "sessionEnd",
    "pre_write": "preToolUse",
    "post_write": "postToolUse",
    "pre_command": "beforeShellExecution",
    "post_command": "afterFileEdit",
    "on_stale": "sessionStart",
}


def render_agent(ir: IRArtifact, directory: Optional[str] = None) -> StagedFile:
    fm = _frontmatter(
        [("name", ir.name), ("description", ir.description), ("readonly", "true")]
    )
    label = ir.display_name or ir.name
    dept = ir.fields.get("department", "")
    topology = ir.fields.get("topology", "specialist")
    header = f"> **{label}** — {dept} · {topology} (advisory office agent)"
    body = ir.body.strip()
    if topology == "generalist":
        body = body.replace(OFFICE_DIRECTORY_MARKER, directory or "")
    return StagedFile(
        f".cursor/agents/{ir.name}.md", _assemble(fm, f"{header}\n\n{body}").encode("utf-8")
    )


def render_skill(ir: IRArtifact) -> StagedFile:
    fm = _frontmatter([("name", ir.name), ("description", ir.description)])
    body = ir.body.strip()
    triggers = ir.fields.get("triggers") or []
    if triggers:
        body = f"{body}\n\n## When to use\nUse when: {', '.join(triggers)}."
    return StagedFile(f".cursor/skills/{ir.name}/SKILL.md", _assemble(fm, body).encode("utf-8"))


def render_command(ir: IRArtifact) -> StagedFile:
    # Cursor commands are plain markdown (no frontmatter, ‹verify›).
    return StagedFile(f".cursor/commands/{ir.name}.md", (ir.body.strip() + "\n").encode("utf-8"))


def render_memories(memory_irs: list[IRArtifact]) -> StagedFile:
    fm = _frontmatter([("description", "Cohort office memories"), ("alwaysApply", "true")])
    body = render_memory_corpus(memory_irs).strip()
    return StagedFile(MEMORIES_REL, _assemble(fm, body).encode("utf-8"))


def render_hooks_fragment(hook_irs: list[IRArtifact]) -> dict:
    hooks: dict[str, list] = {}
    for ir in sorted(hook_irs, key=lambda i: i.name):
        event = HOOK_EVENT_MAP[ir.fields["event"]]
        hooks.setdefault(event, []).append({"type": "command", "command": ir.fields["action"]})
    return {"version": 1, "hooks": hooks}


class CursorRenderer:
    ide = "cursor"
    supported_kinds = frozenset({"agent", "skill", "command", "hook", "memory"})
    merge_targets = (MergeTarget(HOOKS_FRAGMENT_REL, ".cursor/hooks.json", "json"),)

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
            elif ir.kind == "command":
                staged.append(render_command(ir))
            elif ir.kind == "hook":
                hook_irs.append(ir)
            elif ir.kind == "memory":
                memory_irs.append(ir)
            else:  # context (handled by `cohort init`, not the compile pipeline)
                skipped.append(ir.name)
        if memory_irs:
            staged.append(render_memories(memory_irs))
        if hook_irs:
            fragment = render_hooks_fragment(hook_irs)
            staged.append(
                StagedFile(HOOKS_FRAGMENT_REL, (json.dumps(fragment, indent=2) + "\n").encode("utf-8"))
            )
        return staged, skipped

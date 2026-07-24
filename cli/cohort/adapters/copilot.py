"""The GitHub Copilot CLI renderer.

Layout doc-verified 2026-07-24 against the official Copilot docs
(docs.github.com/en/copilot):

- agent  → ``~/.copilot/agents/<name>.md``  MATCH — personal custom-agent profile;
           frontmatter ``name``/``description``/``tools`` (a list of the documented
           tool *aliases*: ``read``, ``search``, ``edit``, ``execute``, ``web``,
           ``agent``, ``todo``). (/copilot/reference/custom-agents-configuration,
           /copilot/reference/copilot-cli-reference/cli-config-dir-reference)
- skill  → ``~/.copilot/skills/<name>/SKILL.md``  MATCH — ``name``/``description``
           frontmatter, Markdown body. (/copilot/how-tos/copilot-cli/customize-copilot/add-skills)
- command→ **declared gap** (Copilot CLI has no user-definable prompt-file /
           slash-command mechanism; that surface is VS-Code-only)
- hook   → ``~/.copilot/hooks/cohort-hooks.json``  MATCH — Copilot loads *every*
           ``*.json`` file under ``hooks/`` independently (unlike Cursor/Codex's
           single shared file), so Cohort's fragment is its own dedicated file:
           **no merge op needed** here, unlike every other renderer's hooks target.
           (/copilot/reference/hooks-reference)
- memory → ``~/.copilot/copilot-instructions.md``  MATCH for the global tier —
           confirmed the same ``@relative/path`` import syntax Claude's ``CLAUDE.md``
           uses, so the merge shape mirrors Claude's almost exactly (corpus file +
           ``@import`` block merge). (/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions)

``dest_root = base / ".copilot"``: unlike Codex/Cursor, every Copilot path above
lives under one uniform root, so this renderer follows Claude's ``dest_subdir``
shape rather than encoding a full subpath per staged file.

Advisory is enforced mechanically, like Claude's tool-strip: the ``tools:`` alias
list is restricted to the read-only aliases (``read``, ``search``, ``web``) unless
``is_doer(ir)`` — Copilot has no single read-only switch (no Codex ``sandbox_mode``,
no Cursor ``readonly``), just a real tool allow-list, so the strip is a filter over
that list rather than a boolean flag.

``model`` is declined: Copilot's ``model:`` frontmatter key expects a concrete model
identifier (e.g. ``gpt-4o``), and Cohort's abstract cost/latency tier has no
doc-verified mapping to real Copilot model ids — the same "omit gracefully" rule
Codex/Cursor already apply to their own undocumented per-agent model keys.

``matcher`` is declined: no matcher/pattern field is documented for
``preToolUse``/``postToolUse`` command hooks — they fire for every tool
invocation, the same rule Codex/Cursor apply to their hook events.

Hook-event names are Copilot's own camelCase vocabulary (doc-verified against
/copilot/reference/hooks-reference) — it happens to overlap with several Cursor
names (``sessionStart``, ``preToolUse``, ...) but is a *distinct*, independently
verified table, not copied from Cursor's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..ir import IRArtifact, is_doer
from .base import MergeTarget
from .claude import (
    StagedFile,
    _assemble,
    _frontmatter,
    _resolve_marker,
    render_office_directory,
    render_memory_corpus,
)

MERGE_SUBDIR = ".merge"
CORPUS_REL = "cohort/copilot-instructions.cohort.md"
IMPORT_BLOCK_REL = f"{MERGE_SUBDIR}/copilot-instructions.import-block.txt"
IMPORT_LINE = "@cohort/copilot-instructions.cohort.md"
HOOKS_REL = "hooks/cohort-hooks.json"  # NOT a merge payload — its own owned file

# canonical (normalized lower, no separators) → Copilot tool alias. Verified
# 2026-07-24 against /copilot/reference/custom-agents-configuration's tool-alias
# table. Several canonical tools collapse onto one alias (search covers
# grep/glob; edit covers write/edit/multiedit/notebookedit; web covers
# webfetch/websearch), which is coarser than Claude's per-tool list but matches
# Copilot's own documented granularity — there is no finer-grained doc-verified
# mapping to invent.
_TOOL_ALIAS_MAP = {
    "read": "read",
    "grep": "search",
    "glob": "search",
    "webfetch": "web",
    "websearch": "web",
    "write": "edit",
    "edit": "edit",
    "multiedit": "edit",
    "bash": "execute",
    "notebookedit": "edit",
}
_READONLY_ALIASES = frozenset({"read", "search", "web"})
_DEFAULT_READONLY_ALIASES = ("read", "search", "web")
# Stable emit order so output is byte-deterministic.
_ALIAS_ORDER = ("read", "search", "web", "edit", "execute")

# canonical hook event → Copilot event name (camelCase). Verified 2026-07-24
# against docs.github.com/en/copilot/reference/hooks-reference. Copilot has no
# separate shell-only hook (pre_command/post_command fold into the tool-use
# events, same rule Codex applies), and no session-compaction analogue for
# on_stale, which approximates to sessionStart — the same nearest-lifecycle-point
# rule Codex/Cursor use for their own on_stale mapping.
HOOK_EVENT_MAP = {
    "session_start": "sessionStart",
    "session_end": "sessionEnd",
    "pre_write": "preToolUse",
    "post_write": "postToolUse",
    "pre_command": "preToolUse",
    "post_command": "postToolUse",
    "pre_compact": "preCompact",
    "post_compact": "postCompact",
    "on_stale": "sessionStart",
    "stop": "agentStop",  # Copilot's per-turn stop event (added alongside the `stop` canonical event)
}


def _norm_tool(name: str) -> Optional[str]:
    key = name.lower().replace("-", "").replace("_", "")
    return _TOOL_ALIAS_MAP.get(key)


def copilot_tools(ir: IRArtifact) -> list[str]:
    """The Copilot ``tools:`` alias list for an agent (mechanical advisory strip).

    Maps canonical tool names to Copilot's documented aliases. A ``scope: project``
    doer (``advisory: false``) keeps its requested tools; every other agent is
    restricted to the read-only aliases, with a sensible default when nothing
    usable is requested. Keyed off ``is_doer`` (never ``advisory`` alone), same
    invariant as the Claude/Codex/Cursor renderers.
    """
    requested = [m for m in (_norm_tool(str(t)) for t in ir.fields.get("tools", [])) if m]
    if is_doer(ir):
        allowed = set(requested) or set(_DEFAULT_READONLY_ALIASES)
    else:
        allowed = {t for t in requested if t in _READONLY_ALIASES} or set(_DEFAULT_READONLY_ALIASES)
    return [t for t in _ALIAS_ORDER if t in allowed]


def render_agent(ir: IRArtifact, directory: Optional[str] = None) -> StagedFile:
    # model tier (#143): no doc-verified tier→Copilot-model-id mapping (Copilot's
    # `model:` expects a concrete id like "gpt-4o"), so the tier is omitted
    # gracefully — same rule as every other doc-cited mapping in this renderer.
    fm = _frontmatter(
        [("name", ir.name), ("description", ir.description), ("tools", copilot_tools(ir))]
    )
    label = ir.display_name or ir.name
    dept = ir.fields.get("department", "")
    topology = ir.fields.get("topology", "specialist")
    header = f"> **{label}** — {dept} · {topology} (advisory office agent)"
    # Validate/resolve the office-directory marker (generalist ↔ specialist
    # invariant), matching the Claude renderer instead of an unchecked replace.
    body = _resolve_marker(ir, ir.body.strip(), directory)
    return StagedFile(
        f"agents/{ir.name}.md", _assemble(fm, f"{header}\n\n{body}").encode("utf-8")
    )


def render_skill(ir: IRArtifact) -> StagedFile:
    fm = _frontmatter([("name", ir.name), ("description", ir.description)])
    body = ir.body.strip()
    triggers = ir.fields.get("triggers") or []
    if triggers:
        body = f"{body}\n\n## When to use\nUse when: {', '.join(triggers)}."
    return StagedFile(f"skills/{ir.name}/SKILL.md", _assemble(fm, body).encode("utf-8"))


def render_hooks_fragment(hook_irs: list[IRArtifact]) -> dict:
    """Copilot ``hooks.json`` (verified 2026-07-24, /copilot/reference/hooks-reference).

    Copilot loads every ``*.json`` file under ``hooks/`` independently, so — unlike
    Cursor/Codex, which key-merge a fragment into ONE shared file — this fragment IS
    the whole file: Cohort's hooks live entirely in their own dedicated
    ``cohort-hooks.json``, never touching any hooks the user authors alongside it.
    """
    hooks: dict[str, list] = {}
    for ir in sorted(hook_irs, key=lambda i: i.name):
        event = HOOK_EVENT_MAP[ir.fields["event"]]
        hooks.setdefault(event, []).append({"type": "command", "command": ir.fields["action"]})
    return {"version": 1, "hooks": hooks}


class CopilotRenderer:
    ide = "copilot"
    supported_kinds = frozenset({"agent", "skill", "hook", "memory"})  # command → gap
    merge_targets = (MergeTarget(IMPORT_BLOCK_REL, "copilot-instructions.md", "block"),)

    def dest_root(self, base: Path) -> Path:
        return base / ".copilot"

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
            staged.append(StagedFile(CORPUS_REL, corpus.encode("utf-8")))
            staged.append(StagedFile(IMPORT_BLOCK_REL, (IMPORT_LINE + "\n").encode("utf-8")))
        if hook_irs:
            fragment = render_hooks_fragment(hook_irs)
            staged.append(
                StagedFile(HOOKS_REL, (json.dumps(fragment, indent=2) + "\n").encode("utf-8"))
            )
        return staged, skipped

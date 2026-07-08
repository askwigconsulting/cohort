"""The Claude renderer — the reference target.

Compiles the IR into byte-stable Claude-native files. The canonical→Claude
mapping is kept here as explicit, tested tables (verified against the installed
Claude Code, 2026-06) so a schema change is a one-place edit:

- agent  → ~/.claude/agents/<name>.md      (frontmatter name/description/tools)
- skill  → ~/.claude/skills/<name>/SKILL.md
- command→ ~/.claude/commands/<name>.md     (description + argument-hint)

`advisory: true` is enforced at render time: the staged tool set is restricted to
read-only tools regardless of what canonical requested (decision 4 / R7).

Aggregating kinds (hook→settings.json, memory→CLAUDE.md) are handled by the
merge layer in Phase 2-T3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..ir import IRArtifact, is_doer
from .base import MergeTarget

# --- tool mapping (verified) -----------------------------------------------

# canonical (normalized lower, no separators) → Claude tool name
_TOOL_MAP = {
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "write": "Write",
    "edit": "Edit",
    "multiedit": "MultiEdit",
    "bash": "Bash",
    "notebookedit": "NotebookEdit",
}
_READONLY = frozenset({"Read", "Grep", "Glob", "WebFetch", "WebSearch"})
_DEFAULT_READONLY = ("Read", "Grep", "Glob", "WebFetch", "WebSearch")
# Stable emit order so output is byte-deterministic.
_TOOL_ORDER = (
    "Read", "Grep", "Glob", "WebFetch", "WebSearch",
    "Write", "Edit", "MultiEdit", "Bash", "NotebookEdit",
)

# canonical hook event → (Claude event, default matcher)
HOOK_EVENT_MAP = {
    "session_start": ("SessionStart", ""),
    "session_end": ("SessionEnd", ""),
    "pre_write": ("PreToolUse", "Write|Edit|MultiEdit"),
    "post_write": ("PostToolUse", "Write|Edit|MultiEdit"),
    "pre_command": ("PreToolUse", "Bash"),
    "post_command": ("PostToolUse", "Bash"),
    "on_stale": ("SessionStart", ""),
}


# Staging layout for aggregating kinds.
MERGE_SUBDIR = ".merge"
CORPUS_REL = "cohort/CLAUDE.cohort.md"  # 1:1 mirror → ~/.claude/cohort/CLAUDE.cohort.md
IMPORT_BLOCK_REL = f"{MERGE_SUBDIR}/CLAUDE.import-block.txt"
HOOKS_FRAGMENT_REL = f"{MERGE_SUBDIR}/settings.hooks.json"
# The @import the managed block writes into ~/.claude/CLAUDE.md (relative to it).
IMPORT_LINE = "@cohort/CLAUDE.cohort.md"
# merge op mapping: staged payload → (dest filename under ~/.claude, strategy)
CLAUDE_MERGE_MAP = [
    (IMPORT_BLOCK_REL, "CLAUDE.md", "block"),
    (HOOKS_FRAGMENT_REL, "settings.json", "json"),
]

_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}

# A generalist's body carries this marker; the renderer replaces it with the
# live specialist directory derived from the roster (P3-T2).
OFFICE_DIRECTORY_MARKER = "<!-- cohort:office-directory -->"


class MarkerError(Exception):
    """Raised when the office-directory marker is missing/misplaced/duplicated."""


def _norm_tool(name: str) -> Optional[str]:
    key = name.lower().replace("-", "").replace("_", "")
    return _TOOL_MAP.get(key)


def claude_tools(ir: IRArtifact) -> list[str]:
    """The Claude tool list for an agent (R7).

    Maps canonical tool names. A ``scope: project`` doer (``advisory: false``) keeps
    its requested write/exec tools; every other agent — advisory, or any synced
    (global) tier — is restricted to read-only, with a sensible read-only default
    when nothing usable is requested. Keyed off ``is_doer`` (never ``advisory``
    alone) so a mis-scoped artifact can't emit write tools at a global compile.
    """
    requested = [m for m in (_norm_tool(str(t)) for t in ir.fields.get("tools", [])) if m]
    if is_doer(ir):
        allowed = set(requested) or set(_DEFAULT_READONLY)
    else:
        allowed = {t for t in requested if t in _READONLY} or set(_DEFAULT_READONLY)
    return [t for t in _TOOL_ORDER if t in allowed]


# --- byte-stable file assembly ---------------------------------------------


def _frontmatter(pairs: list[tuple[str, str]]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in pairs)
    lines.append("---")
    return "\n".join(lines) + "\n"


def _assemble(frontmatter: str, body: str) -> str:
    """frontmatter + blank line + body, normalized to a single trailing newline."""
    return frontmatter + "\n" + body.strip("\n") + "\n"


@dataclass
class StagedFile:
    """A rendered file: its path relative to the staging root, and its bytes."""

    staged_rel: str
    content: bytes


# --- per-kind renderers -----------------------------------------------------


def render_office_directory(specialists: list[IRArtifact]) -> str:
    """Render the specialist directory, ordered by (department, name) codepoint."""
    lines = []
    for ir in sorted(specialists, key=lambda i: (i.fields.get("department", ""), i.name)):
        label = ir.display_name or ir.name
        lines.append(f"- **{label}** ({ir.fields.get('department', '')}) — {ir.description}")
    return "\n".join(lines)


def _resolve_marker(ir: IRArtifact, body: str, directory: Optional[str]) -> str:
    """Resolve / validate the office-directory marker for this agent (P3-T2)."""
    count = body.count(OFFICE_DIRECTORY_MARKER)
    is_generalist = ir.fields.get("topology") == "generalist"
    if is_generalist:
        if count == 0:
            raise MarkerError(f"{ir.name}: generalist is missing the office-directory marker")
        if count > 1:
            raise MarkerError(f"{ir.name}: multiple office-directory markers")
        return body.replace(OFFICE_DIRECTORY_MARKER, directory or "")
    if count:
        raise MarkerError(
            f"{ir.name}: office-directory marker is only allowed in a generalist agent"
        )
    return body


def render_agent(ir: IRArtifact, directory: Optional[str] = None) -> StagedFile:
    tools = ", ".join(claude_tools(ir))
    fm = _frontmatter(
        [("name", ir.name), ("description", ir.description), ("tools", tools)]
    )
    label = ir.display_name or ir.name
    department = ir.fields.get("department", "")
    topology = ir.fields.get("topology", "specialist")
    header = f"> **{label}** — {department} · {topology} (advisory office agent)"
    body = _resolve_marker(ir, ir.body.strip(), directory)
    return StagedFile(f"agents/{ir.name}.md", _assemble(fm, f"{header}\n\n{body}").encode("utf-8"))


def render_skill(ir: IRArtifact) -> StagedFile:
    fm = _frontmatter([("name", ir.name), ("description", ir.description)])
    body = ir.body.strip()
    triggers = ir.fields.get("triggers") or []
    if triggers:
        body = f"{body}\n\n## When to use\nUse when: {', '.join(triggers)}."
    return StagedFile(f"skills/{ir.name}/SKILL.md", _assemble(fm, body).encode("utf-8"))


def render_command(ir: IRArtifact) -> StagedFile:
    pairs = [("description", ir.description)]
    hint = _argument_hint(ir)
    if hint:
        pairs.append(("argument-hint", hint))
    return StagedFile(
        f"commands/{ir.name}.md", _assemble(_frontmatter(pairs), ir.body.strip()).encode("utf-8")
    )


def _argument_hint(ir: IRArtifact) -> str:
    args = ir.fields.get("args") or []
    parts = []
    for arg in args:
        name = arg.get("name", "arg")
        parts.append(f"<{name}>" if arg.get("required") else f"[{name}]")
    return " ".join(parts)


# kinds this renderer produces as 1:1 files (hook/memory handled by the merge layer)
ONE_TO_ONE_RENDERERS = {
    "skill": render_skill,
    "command": render_command,
}


# --- aggregating kinds (hook → settings.json, memory → CLAUDE.md) ----------


def render_hook_entry(ir: IRArtifact) -> tuple[str, dict]:
    """Map a canonical hook IR to (Claude event, settings.json hook entry)."""
    claude_event, default_matcher = HOOK_EVENT_MAP[ir.fields["event"]]
    matcher = ir.fields.get("matcher", default_matcher)
    entry = {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": ir.fields["action"]}],
    }
    return claude_event, entry


def render_hooks_fragment(hook_irs: list[IRArtifact]) -> dict:
    """Build the staged ``settings.json`` hooks fragment Cohort key-merges.

    Multiple canonical hooks may collapse onto one Claude event; they accumulate
    in that event's array (append-on-collision, R8). Sorted by name for byte
    determinism.
    """
    hooks: dict[str, list] = {}
    for ir in sorted(hook_irs, key=lambda i: i.name):
        event, entry = render_hook_entry(ir)
        hooks.setdefault(event, []).append(entry)
    return {"hooks": hooks}


def render_memory_corpus(memory_irs: list[IRArtifact]) -> str:
    """Render the Cohort-owned memory corpus (imported by CLAUDE.md via @import)."""
    items = sorted(
        memory_irs,
        key=lambda ir: (_PRIORITY_ORDER.get(ir.fields.get("priority", "normal"), 1), ir.name),
    )
    parts = [
        "# Cohort office memories",
        "",
        "<!-- Compiled from canonical memories; edit canonical and recompile. -->",
    ]
    for ir in items:
        parts.extend(["", f"## {ir.display_name or ir.name}", "", ir.body.strip()])
    return "\n".join(parts).strip("\n") + "\n"


class ClaudeRenderer:
    """Renders IR → Claude-native staged files (the reference renderer)."""

    ide = "claude"
    dest_subdir = ".claude"
    supported_kinds = frozenset({"agent", "skill", "command", "hook", "memory"})
    merge_targets = (
        MergeTarget(IMPORT_BLOCK_REL, "CLAUDE.md", "block"),
        MergeTarget(HOOKS_FRAGMENT_REL, "settings.json", "json"),
    )

    def dest_root(self, base: Path) -> Path:
        return base / self.dest_subdir

    def matches(self, ir: IRArtifact) -> bool:
        return ir.targets_ide(self.ide)

    def render_one_to_one(
        self, ir: IRArtifact, directory: Optional[str] = None
    ) -> Optional[StagedFile]:
        if ir.kind == "agent":
            return render_agent(ir, directory)
        fn = ONE_TO_ONE_RENDERERS.get(ir.kind)
        return fn(ir) if fn is not None else None

    def compile(
        self, irs: list[IRArtifact], project_tier: bool = False
    ) -> tuple[list[StagedFile], list[str]]:
        """IR → staged Claude files (1:1 + corpus + merge payloads); + skipped names.

        ``project_tier`` is the tier switch. The global office (False) injects the
        specialist directory into its generalist and wires the memory corpus into
        ``CLAUDE.md``. The project tier (True) has neither: no office directory
        (a project generalist is rejected), and no CLAUDE.md merge — that managed
        block is owned by ``cohort init``, so project ``memory`` artifacts are
        skipped rather than allowed to overwrite the init wiring.
        """
        matched = [ir for ir in irs if self.matches(ir)]
        if project_tier:
            generalists = [
                ir for ir in matched if ir.kind == "agent" and ir.fields.get("topology") == "generalist"
            ]
            if generalists:
                raise MarkerError(
                    f"{generalists[0].name}: the project tier cannot declare a generalist "
                    "(no office-directory injection at project scope)"
                )
            directory: Optional[str] = None
        else:
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
            elif ir.kind == "hook":
                hook_irs.append(ir)
            elif ir.kind == "memory":
                memory_irs.append(ir)  # both tiers compile memory into a corpus file
            elif ir.kind == "context":
                # handled by `cohort init` (the project_context scaffold + managed
                # block), deliberately NOT by the compile pipeline
                skipped.append(ir.name)
            else:
                sf = self.render_one_to_one(ir, directory)
                if sf is not None:
                    staged.append(sf)

        if memory_irs:
            corpus = render_memory_corpus(memory_irs)
            staged.append(StagedFile(CORPUS_REL, corpus.encode("utf-8")))
            # The global tier wires the corpus @import into ~/.claude/CLAUDE.md here.
            # The project tier's CLAUDE.md block is init-owned (@import project_context);
            # its corpus @import is added by do_install_project, not this renderer, so
            # this global import line never clobbers the project block.
            if not project_tier:
                staged.append(StagedFile(IMPORT_BLOCK_REL, (IMPORT_LINE + "\n").encode("utf-8")))
        if hook_irs:
            import json as _json

            fragment = render_hooks_fragment(hook_irs)
            staged.append(
                StagedFile(HOOKS_FRAGMENT_REL, (_json.dumps(fragment, indent=2) + "\n").encode("utf-8"))
            )
        return staged, skipped

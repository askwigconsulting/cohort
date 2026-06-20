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
from typing import Optional

from ..ir import IRArtifact

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
    """The read-only-enforced Claude tool list for an agent (R7).

    Maps canonical tool names, restricts to read-only when advisory, and falls
    back to a sensible read-only default when nothing usable is requested.
    """
    advisory = bool(ir.fields.get("advisory", True))
    requested = [m for m in (_norm_tool(str(t)) for t in ir.fields.get("tools", [])) if m]
    if advisory:
        allowed = {t for t in requested if t in _READONLY} or set(_DEFAULT_READONLY)
    else:  # not reachable while Phase 0 enforces advisory: true, but be explicit
        allowed = set(requested) or set(_DEFAULT_READONLY)
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
    """Renders IR → Claude-native staged files."""

    ide = "claude"

    def matches(self, ir: IRArtifact) -> bool:
        return ir.targets_ide(self.ide)

    def render_one_to_one(
        self, ir: IRArtifact, directory: Optional[str] = None
    ) -> Optional[StagedFile]:
        if ir.kind == "agent":
            return render_agent(ir, directory)
        fn = ONE_TO_ONE_RENDERERS.get(ir.kind)
        return fn(ir) if fn is not None else None

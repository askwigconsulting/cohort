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


def render_agent(ir: IRArtifact) -> StagedFile:
    tools = ", ".join(claude_tools(ir))
    fm = _frontmatter(
        [("name", ir.name), ("description", ir.description), ("tools", tools)]
    )
    label = ir.display_name or ir.name
    department = ir.fields.get("department", "")
    topology = ir.fields.get("topology", "specialist")
    header = f"> **{label}** — {department} · {topology} (advisory office agent)"
    body = f"{header}\n\n{ir.body.strip()}"
    return StagedFile(f"agents/{ir.name}.md", _assemble(fm, body).encode("utf-8"))


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
    "agent": render_agent,
    "skill": render_skill,
    "command": render_command,
}


class ClaudeRenderer:
    """Renders IR → Claude-native staged files."""

    ide = "claude"

    def matches(self, ir: IRArtifact) -> bool:
        return ir.targets_ide(self.ide)

    def render_one_to_one(self, ir: IRArtifact) -> Optional[StagedFile]:
        fn = ONE_TO_ONE_RENDERERS.get(ir.kind)
        return fn(ir) if fn is not None else None

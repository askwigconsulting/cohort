"""`cohort distill` — compound recent session/feedback records into a proposed,
provenance-cited addition to ``project_context.md``.

The memory layer does not compound on its own: ``sessions/`` and ``feedback/``
accumulate next to ``project_context.md`` but nothing flows back into it. ``distill``
closes that loop deterministically — no LLM, no network — and it is the one child of
the memory loop that adds a write path, so its shape *is* its safety model:

- **Append-only, dated, outside the managed block.** Each run appends a
  ``## Distilled (YYYY-MM-DD) — review provenance`` section at the **end** of
  ``project_context.md`` — after Cohort's managed block, which
  ``refresh_project_context`` regenerates *in place*. Distilled content therefore
  survives ``cohort context refresh``. Append-only (not an ownership hash) is what
  prevents clobbering: the section is **user-owned the moment it is written**, so a
  later hand-edit never forks or triggers a skip+warn.
- **Extractive, never rewritten.** Every proposed line quotes a source bullet and
  cites its source file + record date. Nothing is paraphrased into an imperative
  instruction — ``sessions/``/``feedback/`` are git-tracked and contributor-writable,
  hence **untrusted input**; the confirm diff is the security gate.
- **Confirm-gated, fail-closed.** A real write requires an explicit affirmative
  confirm decision over a rendered, control-char-escaped diff. No confirm callback
  (an unattended / hooked invocation) never writes — ``distill`` is never wired to a
  hook and cannot run unattended.

Extraction reuses ``reports.collect_sessions`` / ``reports._section_bullets`` — no
third session parser. Stdlib-only.
"""

from __future__ import annotations

import difflib
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .install_model import CohortPaths
from .loader import load_artifact
from .project import is_life_project
from .reports import _utc_now, _to_utc, collect_sessions

DEFAULT_DAYS = 30

# All C0 control characters except newline (\x0a) and tab (\x09), plus DEL (\x7f),
# the C1 range \x80-\x9f (\x9b is a one-byte CSI introducer — ANSI without ESC; \x85
# is NEL), and the Unicode line separators U+2028/U+2029 (which could split a diff
# line). CR (\x0d) and ESC (\x1b) are stripped here on purpose: they are the exact
# bytes an attacker would embed in a session record to visually disguise the lines
# being approved in the diff preview (CR overwrites, ESC opens an ANSI sequence).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")


def _sanitize(text: str) -> str:
    """Escape control characters (C0 except newline/tab, DEL, C1, U+2028/U+2029) to
    a visible ``\\xNN`` form so embedded ANSI/CR cannot disguise approved lines.
    Applied to the extracted content itself, so what is previewed is exactly what is
    written."""
    return _CONTROL.sub(lambda m: repr(m.group())[1:-1], text)


def _inline(text: str) -> str:
    """Sanitize a field that must occupy exactly one physical line (a rating, agent
    name, or cited filename): control chars escaped, embedded newlines made visible.
    Together with ``_first_line`` this keeps the per-line invariant — no record field
    can inject a bare, uncited line into the distilled section."""
    return _sanitize(text).replace("\n", "\\n")


def _first_line(text: str) -> str:
    """Collapse a multi-line note to its first non-empty line, marking clipped
    content with ``[…]``. Keeps the per-line invariant — every physical line in the
    distilled section carries its prefix and citation — so a contributor-writable
    note can never inject a bare markdown line (e.g. a forged ``## Distilled``
    header) between cited lines."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    return lines[0] + (" […]" if len(lines) > 1 else "")


def _quote(bullet: str) -> str:
    """Normalize a ``_section_bullets`` line (``- text``) to its bare quoted text."""
    return _sanitize(bullet[2:].strip() if bullet.startswith("- ") else bullet.strip())


def collect_feedback(paths: CohortPaths, since, until) -> list[dict[str, Any]]:
    """In-window feedback entries as ``{ts, file, rating, agent, note}``.

    Not a session parser (feedback has its own frontmatter shape) — it reuses the same
    ``load_artifact`` loader and UTC-normalization as ``collect_sessions``.
    """
    fb_dir = paths.cohort_home / "feedback"
    if not fb_dir.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in sorted(fb_dir.glob("*.md")):
        loaded = load_artifact(p)
        fm = loaded.frontmatter or {}
        if "timestamp" not in fm:
            continue
        ts = _to_utc(fm["timestamp"])
        if since <= ts <= until:
            entries.append({
                "ts": ts,
                "file": p.name,
                "rating": str(fm.get("rating", "")),
                "agent": str(fm.get("agent", "") or fm.get("command", "")),
                "note": (loaded.body or "").strip(),
            })
    return sorted(entries, key=lambda e: e["ts"], reverse=True)


def _cite(kind: str, filename: str, ts) -> str:
    return f" — _{kind}/{_inline(filename)} · {ts.date()}_"


def render_distilled_section(
    today: str, days: int, sessions: list[dict], feedback: list[dict]
) -> str:
    """The dated, extractive, provenance-cited section (never rewrites records)."""
    lines = [
        f"## Distilled ({today}) — review provenance",
        "",
        f"_Extractive digest of `sessions/` + `feedback/` over the last {days} days. "
        "Each line quotes a source record and cites it; review provenance before "
        "keeping. This section is yours the moment it is written — edit it freely._",
    ]
    decisions = [
        f"- {_quote(b)}{_cite('sessions', s['file'], s['ts'])}"
        for s in sessions for b in s["decisions"]
    ]
    open_items = [
        f"- {_quote(b)}{_cite('sessions', s['file'], s['ts'])}"
        for s in sessions for b in s["open_items"]
    ]
    fb_lines = []
    for f in feedback:
        # Collapse the note to one physical line before sanitizing: a multi-line
        # note must never inject bare (unprefixed, uncited) lines into the section.
        note = _first_line(f["note"])
        if not note:
            continue
        fb_lines.append(
            f"- {_inline(f['rating'])}"
            f"{(' on ' + _inline(f['agent'])) if f['agent'] else ''}: "
            f"{_sanitize(note)}{_cite('feedback', f['file'], f['ts'])}"
        )
    for header, body in (("Decisions", decisions), ("Open items", open_items),
                         ("Feedback", fb_lines)):
        if body:
            lines += ["", f"### {header}", *body]
    return "\n".join(lines) + "\n"


def _append_section(current: str, section: str) -> str:
    """Append the section at the very end of the file, after Cohort's managed block."""
    if current.strip() == "":
        return section
    return current.rstrip("\n") + "\n\n" + section


def _diff(current: str, proposed: str, target: str = "project_context.md") -> str:
    """A control-char-escaped unified diff of the proposed append (the review gate)."""
    diff = difflib.unified_diff(
        current.splitlines(), proposed.splitlines(),
        fromfile=target, tofile=f"{target} (proposed)",
        lineterm="",
    )
    return _sanitize("\n".join(diff))


# --- life-scoped target (RFC 0003 §5) ----------------------------------------
#
# In a `template = "life"` project the distill target is the current week file's
# `## Review` section, and `project_context.md` is REFUSED as a target: the
# project context is `@import`ed into every future session, so distilling
# connector-adjacent session text into it would open a permanent injection
# channel and a privacy leak. Input stays `sessions/` + `feedback/` only — never
# the briefing quarantine.


def _demote_headings(section: str) -> str:
    """Shift the rendered section's headings one level down (## → ###, ### →
    ####) so it nests *inside* the week file's ``## Review`` section."""
    out = []
    for ln in section.splitlines():
        if ln.startswith("## ") or ln.startswith("### "):
            out.append("#" + ln)
        else:
            out.append(ln)
    return "\n".join(out) + "\n"


def _insert_under_review(current: str, label: str, section: str) -> str:
    """Append the section at the end of the week file's ``## Review`` section
    (creating the §1a skeleton, or the heading, when missing)."""
    from .life import WEEK_SKELETON  # life imports project, not distill — no cycle

    if current.strip() == "":
        current = WEEK_SKELETON.format(label=label)
    lines = current.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "## Review"), None)
    if start is None:
        return current.rstrip("\n") + "\n\n## Review\n\n" + section
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
               len(lines))
    head = "\n".join(lines[:end]).rstrip("\n")
    tail = "\n".join(lines[end:]).rstrip("\n")
    return head + "\n\n" + section + (("\n" + tail + "\n") if tail else "")


def do_distill(
    repo: Path,
    days: int = DEFAULT_DAYS,
    dry_run: bool = False,
    confirm: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    """Draft a dated, extractive distilled section and apply it only on explicit
    confirm. Reads ``sessions/`` + ``feedback/`` only (``reports/`` excluded).

    ``confirm`` receives the rendered diff and returns whether to write. It is the
    single write gate: ``dry_run`` never writes, and a missing ``confirm`` (an
    unattended path) **fails closed** — the section is never written without an
    affirmative human decision.
    """
    paths = CohortPaths.for_project(repo)
    project_context = paths.cohort_home / "project_context.md"
    if not project_context.exists():
        return {"action": "distill", "error": "not a Cohort project (run cohort init)"}
    life = is_life_project(paths)
    until = _utc_now()
    since = until - timedelta(days=days)
    sessions = collect_sessions(paths, since, until)
    feedback = collect_feedback(paths, since, until)
    if not sessions and not feedback:
        return {"action": "distill", "empty": True, "days": days}
    today = until.strftime("%Y-%m-%d")
    section = render_distilled_section(today, days, sessions, feedback)
    if life:
        # Life project: `project_context.md` is REFUSED as the distill target
        # (see the section comment above) — the write goes to the current week
        # file's `## Review`, resolved once in the user's local timezone.
        from .life import local_today, week_label

        label = week_label(local_today())
        target_rel = f"weeks/{label}.md"
        target_path = repo / target_rel
        current = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        proposed = _insert_under_review(current, label, _demote_headings(section))
    else:
        target_rel = "project_context.md"
        target_path = project_context
        current = project_context.read_text(encoding="utf-8")
        proposed = _append_section(current, section)
    diff = _diff(current, proposed, target_rel)
    if dry_run:
        return {"action": "distill", "dry_run": True, "empty": False, "target": target_rel,
                "days": days, "diff": diff, "section": section}
    if confirm is None or not confirm(diff):
        return {"action": "distill", "applied": False, "days": days,
                "target": target_rel, "diff": diff}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(proposed, encoding="utf-8")
    return {"action": "distill", "applied": True, "days": days, "target": target_rel,
            "header": f"Distilled ({today})"}

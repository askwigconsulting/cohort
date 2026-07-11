"""Stdlib parser for the RFC 0003 §1a life data model (no new dependencies).

Parses the *pinned* markdown format — headings + GitHub-style checklists + dated
filenames — into JSON-safe dicts for the dashboard's Today / Week / Goals views.

Contract notes (RFC §1a), load-bearing:
  * Unknown sections are **preserved and ignored** (kept in the returned
    ``sections`` map), never an error.
  * A **diagnostic is emitted when a KNOWN heading is missing** — the caller
    surfaces it rather than silently rendering a blank view.
  * "Today"/"this week" resolve from a **timezone-aware datetime passed in by the
    caller** (computed once, never ``datetime.now()`` mid-logic), so the server
    and an interactive session agree across a midnight/UTC boundary.

This module is a pure reader: it never writes life data (that is WS-A's
``cohort life`` verbs). It imports only the standard library.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# --- grammar ----------------------------------------------------------------

# A GitHub-style checklist item at line start, tolerating one optional leading
# indent level (up to four spaces or a tab). ``[x]``/``[X]`` = done; any other
# bracket content = open (RFC §1a).
_CHECKBOX = re.compile(r"^(?:[ ]{1,4}|\t)?- \[(.)\][ \t]?(.*)$")
# An agenda event line: ``- HH:MM title`` (calendar-derived; time + title only).
_AGENDA = re.compile(r"^-[ \t]+(\d{1,2}:\d{2})[ \t]+(.*)$")
# A level-2 heading (``## Name``).
_H2 = re.compile(r"^##[ \t]+(.*)$")

DAY_HEADINGS = ("Agenda", "Top 3", "Log")
WEEK_HEADINGS = ("Plan", "Review")
MAX_TOP3 = 3

# The commands the "Run <command>" buttons may enqueue. Per WS-A's final
# contract, ONLY ``briefing`` and ``triage`` are enqueueable jobs (the two
# commands pinned to the egress-closed briefing profile); today/week/month are
# interactive-only and never enqueued. The dashboard only *names* an allowlisted
# command; WS-A's ``cohort run`` (``_JOBS``) is the sole allowlist authority.
ENQUEUE_COMMANDS = ("briefing", "triage")


# --- date resolution (timezone passed in, computed once) --------------------


def day_stem(now: datetime) -> str:
    """``YYYY-MM-DD`` for the day file, from a caller-computed local datetime."""
    return now.strftime("%Y-%m-%d")


def week_stem(now: datetime) -> str:
    """``YYYY-Wnn`` (ISO-8601 week, zero-padded) from a caller-computed datetime.

    Uses the ISO calendar so the year tracks the ISO week-year, not the calendar
    year, across a year boundary (e.g. 2027-01-01 may be ``2026-W53``)."""
    iso = now.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


# --- low-level parsing ------------------------------------------------------


def _split_sections(text: str) -> tuple[Optional[str], dict[str, list[str]]]:
    """Split markdown into (title, ordered ``{h2 -> body lines}``).

    ``title`` is the first level-1 (``# ``) heading's text. Later ``# `` lines are
    treated as body (the format has exactly one). Content before the first ``##``
    is dropped (the formats put all data under ``##`` sections)."""
    title: Optional[str] = None
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if title is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        h2 = _H2.match(line)
        if h2:
            current = h2.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return title, sections


def _checklist(lines: list[str]) -> list[dict[str, Any]]:
    """Every checklist item in ``lines`` as ``{"text", "done", "line"}``.

    ``line`` is the 1-based position of the item among the checklist items in this
    section — which equals WS-A ``do_toggle_task``'s file-order index for a §1a
    file, where the only checkboxes live in the section they belong to (Top 3 /
    Plan / inbox). The dashboard sends this ``line`` straight to the verb."""
    items: list[dict[str, Any]] = []
    n = 0
    for line in lines:
        m = _CHECKBOX.match(line)
        if m:
            n += 1
            items.append({
                "text": m.group(2).strip(),
                "done": m.group(1).strip().lower() == "x",
                "line": n,
            })
    return items


def _agenda(lines: list[str]) -> list[dict[str, Any]]:
    """Agenda events as ``{"time", "title"}`` — time is ``None`` for a non-timed
    ``- `` bullet (time + title only; never attendees/links, RFC minimization)."""
    events: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        m = _AGENDA.match(stripped)
        if m:
            events.append({"time": m.group(1), "title": m.group(2).strip()})
        else:
            events.append({"time": None, "title": stripped[2:].strip()})
    return events


def _sections_text(sections: dict[str, list[str]]) -> dict[str, str]:
    """The raw section bodies as joined strings (so unknown sections survive)."""
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _missing(known: tuple[str, ...], sections: dict[str, list[str]]) -> list[str]:
    return [f"missing heading: ## {h}" for h in known if h not in sections]


# --- view parsers -----------------------------------------------------------


def parse_day(text: str, *, expected_date: Optional[str] = None) -> dict[str, Any]:
    """A day file → ``{date, agenda, top3, log, diagnostics, sections}``.

    ``expected_date`` (the filename stem) lets the parser flag a title/filename
    mismatch. Missing known headings and an over-long Top 3 are diagnostics."""
    title, sections = _split_sections(text)
    diagnostics = _missing(DAY_HEADINGS, sections)
    if expected_date is not None and title is not None and title != expected_date:
        diagnostics.append(f"date heading '{title}' does not match filename '{expected_date}'")
    top3 = _checklist(sections.get("Top 3", []))
    if len(top3) > MAX_TOP3:
        diagnostics.append(f"Top 3 has {len(top3)} items (expected ≤{MAX_TOP3})")
    return {
        "date": title or expected_date,
        "agenda": _agenda(sections.get("Agenda", [])),
        "top3": top3,
        "log": "\n".join(sections.get("Log", [])).strip(),
        "diagnostics": diagnostics,
        "sections": _sections_text(sections),
    }


def parse_week(text: str, *, expected_week: Optional[str] = None) -> dict[str, Any]:
    """A week file → ``{week, plan, review, diagnostics, sections}``."""
    title, sections = _split_sections(text)
    diagnostics = _missing(WEEK_HEADINGS, sections)
    if expected_week is not None and title is not None and title != expected_week:
        diagnostics.append(f"week heading '{title}' does not match filename '{expected_week}'")
    return {
        "week": title or expected_week,
        "plan": _checklist(sections.get("Plan", [])),
        "review": "\n".join(sections.get("Review", [])).strip(),
        "diagnostics": diagnostics,
        "sections": _sections_text(sections),
    }


def parse_goals(text: str) -> dict[str, Any]:
    """A goals file → ``{title, goals:[{goal, items, text}], diagnostics}``.

    Each ``## <goal>`` section is a goal with its progress checklist. A missing
    ``# ... goals`` title or a file with no goal sections is a diagnostic."""
    title, sections = _split_sections(text)
    diagnostics: list[str] = []
    if title is None:
        diagnostics.append("missing goals title (# <year|quarter> goals)")
    goals = [
        {"goal": name, "items": _checklist(lines), "text": "\n".join(lines).strip()}
        for name, lines in sections.items()
    ]
    if not goals:
        diagnostics.append("no goal sections (## <goal>)")
    return {"title": title, "goals": goals, "diagnostics": diagnostics}


# --- config (fail-safe; consolidates with WS-A's read_project_config) --------


def read_life_config(cohort_home: Path) -> dict[str, Any]:
    """Life-relevant markers from a project's ``cohort.toml``, read **fail-safe**.

    Returns ``{"template": Optional[str], "private": bool}``. An absent ``template``
    means a code project (``None``). ``dashboard.private`` defaults **True for the
    life template** (absent key ⇒ private, RFC §4) and False otherwise; an explicit
    boolean always wins. A missing/corrupt TOML degrades to a code project.

    WS-A introduces a shared ``read_project_config`` — this is the WS-B-local
    reader to consolidate with at merge.
    """
    toml_path = Path(cohort_home) / "cohort.toml"
    data: dict[str, Any] = {}
    if toml_path.exists():
        try:
            import tomllib

            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
        except Exception:  # noqa: BLE001 - a bad config falls back to a code project
            data = {}
    template = data.get("template")
    template = template if isinstance(template, str) else None
    dashboard = data.get("dashboard")
    private = dashboard.get("private") if isinstance(dashboard, dict) else None
    if not isinstance(private, bool):
        private = template == "life"  # fail-safe: the life template is private by default
    return {"template": template, "private": private}


def is_life(cohort_home: Path) -> bool:
    return read_life_config(cohort_home)["template"] == "life"


def is_private(cohort_home: Path) -> bool:
    return bool(read_life_config(cohort_home)["private"])


# --- assembly (files → the dashboard life block) ----------------------------

_MAX_QUARANTINE_BYTES = 40_000  # cap untrusted briefing/job output read into state
_MAX_QUARANTINE_FILES = 12
_MAX_JOBS = 12


def _read_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_capped(path: Path, limit: int) -> str:
    """Read at most ``limit`` bytes of an untrusted quarantine file as text."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return ""
    text = raw[:limit].decode("utf-8", errors="replace")
    if len(raw) > limit:
        text += "\n… (truncated)"
    return text


def load_life_views(repo: Path, now: datetime) -> dict[str, Any]:
    """Parse the day/week/goals files for the focused life project.

    ``now`` is the caller's once-computed, timezone-aware datetime — it fixes
    which day/week file counts as "today"/"this week". Missing files yield an
    empty view carrying a diagnostic, never a crash."""
    repo = Path(repo)
    today_name = day_stem(now)
    week_name = week_stem(now)

    day_file = repo / "days" / f"{today_name}.md"
    day_text = _read_file(day_file)
    if day_text is None:
        today = {"date": today_name, "agenda": [], "top3": [], "log": "",
                 "diagnostics": [f"no day file yet (days/{today_name}.md)"], "sections": {}}
    else:
        today = parse_day(day_text, expected_date=today_name)

    week_file = repo / "weeks" / f"{week_name}.md"
    week_text = _read_file(week_file)
    if week_text is None:
        week = {"week": week_name, "plan": [], "review": "",
                "diagnostics": [f"no week file yet (weeks/{week_name}.md)"], "sections": {}}
    else:
        week = parse_week(week_text, expected_week=week_name)

    goals_dir = repo / "goals"
    goals: list[dict[str, Any]] = []
    if goals_dir.is_dir():
        for gf in sorted(goals_dir.glob("*.md")):
            text = _read_file(gf)
            if text is not None:
                parsed = parse_goals(text)
                parsed["file"] = gf.name
                goals.append(parsed)

    return {"today": today, "week": week, "goals": goals}


def read_quarantine(cohort_home: Path) -> list[dict[str, Any]]:
    """The briefing/job-output quarantine (``reports/briefings/``), newest-first.

    Every entry is **untrusted, connector/job-derived** content: the caller must
    render it ``textContent``-only under the untrusted banner. Content is size-
    capped; nothing here is ever ``@import``ed or auto-loaded."""
    qdir = Path(cohort_home) / "reports" / "briefings"
    if not qdir.is_dir():
        return []
    files = [p for p in qdir.iterdir() if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:_MAX_QUARANTINE_FILES]:
        out.append({
            "name": p.name,
            "text": _read_capped(p, _MAX_QUARANTINE_BYTES),
            "untrusted": True,
        })
    return out


def read_jobs(cohort_home: Path) -> list[dict[str, Any]]:
    """Recent job records from ``.cohort/jobs/`` (WS-A's bounded schema).

    Surfaces ``command``/``status``/``requested_at`` plus the runner-written
    ``output`` (a quarantine-relative path), ``error`` and ``exit_code`` so the
    live panel can show progress (queued→running→done|failed|rejected). Every
    field is bounded — a request is an allowlisted-command descriptor, never a
    free-text prompt (RFC §4). Malformed JSON is skipped."""
    import json

    jdir = Path(cohort_home) / "jobs"
    if not jdir.is_dir():
        return []
    files = sorted(jdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:_MAX_JOBS]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append({
            "file": p.name,
            "command": str(data.get("command", ""))[:64],
            "status": str(data.get("status", ""))[:32],
            "requested_at": str(data.get("requested_at", ""))[:32],
            "output": str(data.get("output", ""))[:256],  # quarantine-relative path
            "error": str(data.get("error", ""))[:256],
            "exit_code": data.get("exit_code"),
        })
    return out


def collect_life(repo: Path, cohort_home: Path, now: datetime) -> dict[str, Any]:
    """The full ``state["life"]`` block for a focused life project."""
    views = load_life_views(repo, now)
    quarantine = read_quarantine(cohort_home)
    return {
        "date": day_stem(now),
        "week_id": week_stem(now),
        "today": views["today"],
        "week": views["week"],
        "goals": views["goals"],
        "briefing": quarantine[0] if quarantine else None,
        "quarantine": quarantine,
        "jobs": read_jobs(cohort_home),
        "commands": list(ENQUEUE_COMMANDS),
    }

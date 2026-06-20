"""`cohort weekly-report` / `monthly-report` — deterministic dated reports.

A report body is a pure function of (window, in-window sessions, stable git
fields). Only **commit subjects, counts, and contributor names** are rendered —
never SHAs or commit dates (both volatile), so the goldens are stable (R2).
Session timestamps are normalized to UTC whether stored as strings or YAML-
parsed datetimes (R7).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .install_model import CohortPaths
from .loader import load_artifact

WINDOW_DAYS = {"weekly": 7, "monthly": 30}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: Any) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_window(period: str, since: Optional[str], until: Optional[str]) -> tuple[datetime, datetime]:
    until_dt = _to_utc(until) if until else _utc_now()
    if since:
        since_dt = _to_utc(since)
    else:
        since_dt = until_dt - timedelta(days=WINDOW_DAYS[period])
    return since_dt, until_dt


def _section_bullets(body: str, header: str) -> list[str]:
    """Bullet lines under a '## <header>' section (deterministic, placeholder-free)."""
    out: list[str] = []
    in_section = False
    for line in body.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == f"## {header}"
            continue
        if in_section and line.lstrip().startswith("- "):
            out.append(line.strip())
    return out


def collect_sessions(paths: CohortPaths, since: datetime, until: datetime) -> list[dict[str, Any]]:
    sessions_dir = paths.cohort_home / "sessions"
    if not sessions_dir.exists():
        return []
    entries = []
    for p in sorted(sessions_dir.glob("*.md")):
        loaded = load_artifact(p)
        fm = loaded.frontmatter or {}
        if "timestamp" not in fm:
            continue
        ts = _to_utc(fm["timestamp"])
        if since <= ts <= until:
            entries.append({
                "ts": ts,
                "decisions": _section_bullets(loaded.body, "Decisions"),
                "open_items": _section_bullets(loaded.body, "Open items"),
            })
    return sorted(entries, key=lambda e: e["ts"], reverse=True)


def collect_commits(repo: Path, since: datetime, until: datetime) -> list[tuple[str, str]]:
    """In-window commits as (author_name, subject) — no SHAs/dates in output (R2).

    Windowing is done in Python on the committer date (``%cI``) rather than via
    git's ``--since/--until`` (which is unreliable for absolute dates / clock
    skew); the date drives the filter but is never rendered.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--pretty=format:%cI%x1f%an%x1f%s"],
            cwd=repo, capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001 - git is best-effort
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        cdate, author, subject = parts
        ts = _to_utc(cdate)
        if since <= ts <= until:
            commits.append((author, subject))
    return commits


def render_report(period: str, since: datetime, until: datetime,
                  sessions: list[dict], commits: list[tuple[str, str]]) -> str:
    contributors = sorted({a for a, _ in commits})
    decisions = [d for s in sessions for d in s["decisions"]]
    open_items = [o for s in sessions for o in s["open_items"]]

    lines = [
        f"# {period.capitalize()} report — {since.date()} to {until.date()}",
        "",
        "## Summary",
        f"- Snapshots: {len(sessions)}",
        f"- Commits: {len(commits)}",
        f"- Contributors: {len(contributors)}",
        "",
        "## Sessions",
        "### Decisions",
    ]
    lines.extend(decisions or ["- _none_"])
    lines += ["", "## Commits"]
    if commits:
        by_author: dict[str, list[str]] = {}
        for author, subject in commits:
            by_author.setdefault(author, []).append(subject)
        for author in sorted(by_author):
            lines.append(f"### {author}")
            lines.extend(f"- {s}" for s in by_author[author])
    else:
        lines.append("- _none_")
    lines += ["", "## Open items"]
    lines.extend(open_items or ["- _none_"])
    return "\n".join(lines) + "\n"


def do_report(repo: Path, period: str, since: Optional[str], until: Optional[str],
              dry_run: bool) -> dict[str, Any]:
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists():
        return {"action": "report", "error": "not a Cohort project (run cohort init)"}
    since_dt, until_dt = resolve_window(period, since, until)
    sessions = collect_sessions(paths, since_dt, until_dt)
    commits = collect_commits(repo, since_dt, until_dt)
    body = render_report(period, since_dt, until_dt, sessions, commits)
    filename = f"{period}-{until_dt.strftime('%Y-%m-%d')}.md"
    if dry_run:
        return {"action": "report", "dry_run": True, "file": filename, "body": body}
    reports_dir = paths.cohort_home / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)  # lazy create (R6)
    (reports_dir / filename).write_text(body, encoding="utf-8")
    return {"action": "report", "dry_run": False, "file": filename}

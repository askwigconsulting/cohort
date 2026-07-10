"""Unit tests for the improve.py aggregation view-model (#145): the shared,
dated extraction (load_feedback_entries / load_session_entries) that
aggregate_signals composes into per-project counts, and agent_scorecards, the
per-agent up/down benchmarking view the dashboard builds cross-project on top
of. Pure functions — no CLI subprocess, no git repo required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cohort.frontmatter import dump_frontmatter
from cohort.improve import (
    agent_scorecards,
    aggregate_signals,
    load_feedback_entries,
    load_session_entries,
)
from cohort.install_model import CohortPaths


def _write_feedback(repo: Path, name: str, rating: str, agent: str | None = None,
                     command: str | None = None, timestamp: str | None = None) -> None:
    fb_dir = repo / ".cohort" / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    pairs = [("rating", rating)]
    if agent:
        pairs.append(("agent", agent))
    if command:
        pairs.append(("command", command))
    if timestamp:
        pairs.append(("timestamp", timestamp))
    (fb_dir / name).write_text(dump_frontmatter(pairs), encoding="utf-8")


def _write_session(repo: Path, name: str, timestamp: str, author: str = "dev",
                    branch: str = "main") -> None:
    sessions_dir = repo / ".cohort" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    pairs = [("timestamp", timestamp), ("author", author), ("branch", branch)]
    (sessions_dir / name).write_text(dump_frontmatter(pairs), encoding="utf-8")


# === load_feedback_entries / load_session_entries (shared extraction) =======


def test_load_feedback_entries_empty_dir_returns_empty_list(tmp_path):
    paths = CohortPaths.for_project(tmp_path)
    assert load_feedback_entries(paths) == []


def test_load_feedback_entries_keeps_timestamps(tmp_path):
    _write_feedback(tmp_path, "a.md", "up", agent="counsel", timestamp="2026-07-01T10:00:00+00:00")
    paths = CohortPaths.for_project(tmp_path)
    entries = load_feedback_entries(paths)
    assert entries == [{
        "rating": "up", "agent": "counsel", "command": None,
        "timestamp": "2026-07-01T10:00:00+00:00",
    }]


def test_load_session_entries_empty_dir_returns_empty_list(tmp_path):
    paths = CohortPaths.for_project(tmp_path)
    assert load_session_entries(paths) == []


def test_load_session_entries_reads_frontmatter(tmp_path):
    _write_session(tmp_path, "s.md", "2026-07-05T09:00:00+00:00", author="jonathan", branch="feat/x")
    paths = CohortPaths.for_project(tmp_path)
    entries = load_session_entries(paths)
    assert entries == [{
        "timestamp": "2026-07-05T09:00:00+00:00", "author": "jonathan", "branch": "feat/x",
    }]


# === aggregate_signals (backward-compatible, now built on the shared extraction) ==


def test_aggregate_signals_empty_project_is_zeroed(tmp_path):
    paths = CohortPaths.for_project(tmp_path)
    ev = aggregate_signals(paths)
    assert ev == {
        "feedback_total": 0, "sessions": 0, "agent_usage": {},
        "low_rated_agents": [], "friction_commands": [],
    }


def test_aggregate_signals_counts_ratings_and_sessions(tmp_path):
    _write_feedback(tmp_path, "a.md", "up", agent="counsel")
    _write_feedback(tmp_path, "b.md", "down", agent="counsel")
    _write_feedback(tmp_path, "c.md", "down", agent="counsel")
    _write_feedback(tmp_path, "d.md", "down", command="ship")
    _write_session(tmp_path, "s1.md", "2026-07-01T00:00:00+00:00")
    paths = CohortPaths.for_project(tmp_path)
    ev = aggregate_signals(paths)
    assert ev["feedback_total"] == 4
    assert ev["sessions"] == 1
    assert ev["agent_usage"] == {"counsel": 3}
    assert ev["low_rated_agents"] == ["counsel"]  # 2 down > 1 up
    assert ev["friction_commands"] == ["ship"]


# === agent_scorecards ========================================================


def test_agent_scorecards_empty_entries_is_empty_list():
    assert agent_scorecards([]) == []


def test_agent_scorecards_ignores_agentless_and_unrated_entries():
    entries = [
        {"rating": "up", "agent": None, "command": "ship", "timestamp": None},
        {"rating": "sideways", "agent": "counsel", "command": None, "timestamp": None},
    ]
    assert agent_scorecards(entries) == []


def test_agent_scorecards_computes_counts_net_and_ratio():
    entries = [
        {"rating": "up", "agent": "counsel", "command": None, "timestamp": None},
        {"rating": "up", "agent": "counsel", "command": None, "timestamp": None},
        {"rating": "down", "agent": "counsel", "command": None, "timestamp": None},
    ]
    cards = agent_scorecards(entries)
    assert len(cards) == 1
    card = cards[0]
    assert card["agent"] == "counsel"
    assert card["up"] == 2
    assert card["down"] == 1
    assert card["net"] == 1
    assert card["up_ratio"] == round(2 / 3, 3)
    assert card["trend"] == []  # no timestamps → no trend-window data


def test_agent_scorecards_orders_by_volume_then_name():
    entries = [
        {"rating": "up", "agent": "zeta", "command": None, "timestamp": None},
        {"rating": "up", "agent": "alpha", "command": None, "timestamp": None},
        {"rating": "up", "agent": "alpha", "command": None, "timestamp": None},
        {"rating": "down", "agent": "alpha", "command": None, "timestamp": None},
    ]
    cards = agent_scorecards(entries)
    assert [c["agent"] for c in cards] == ["alpha", "zeta"]  # alpha has 3, zeta has 1


def test_agent_scorecards_trend_buckets_by_day_within_last_30(tmp_path):
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    entries = [
        # inside the 30-day window
        {"rating": "up", "agent": "counsel", "command": None, "timestamp": "2026-07-01T09:00:00+00:00"},
        {"rating": "up", "agent": "counsel", "command": None, "timestamp": "2026-07-01T15:00:00+00:00"},
        {"rating": "down", "agent": "counsel", "command": None, "timestamp": "2026-07-05T09:00:00+00:00"},
        # outside the window (older than 30 days from `now`)
        {"rating": "down", "agent": "counsel", "command": None, "timestamp": "2026-05-01T09:00:00+00:00"},
    ]
    cards = agent_scorecards(entries, now=now)
    card = cards[0]
    # totals include the out-of-window entry...
    assert card["up"] == 2
    assert card["down"] == 2
    # ...but the trend only covers the last-30-day window, bucketed by day.
    assert card["trend"] == [
        {"date": "2026-07-01", "up": 2, "down": 0},
        {"date": "2026-07-05", "up": 0, "down": 1},
    ]


def test_agent_scorecards_ignores_malformed_timestamp_for_trend():
    entries = [
        {"rating": "up", "agent": "counsel", "command": None, "timestamp": "not-a-date"},
    ]
    cards = agent_scorecards(entries, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
    assert cards[0]["up"] == 1  # still counted in totals
    assert cards[0]["trend"] == []  # but excluded from the dated trend

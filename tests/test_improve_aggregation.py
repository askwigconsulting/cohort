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


# === limit-before-parse (#295 item 3) ======================================


def _count_parses(monkeypatch) -> dict[str, int]:
    """Spy on ``improve.load_artifact`` so a test can assert how many record files
    a loader actually opened (not just how many entries it returned)."""
    from cohort import improve

    seen = {"n": 0}
    real = improve.load_artifact

    def counting(path):
        seen["n"] += 1
        return real(path)

    monkeypatch.setattr(improve, "load_artifact", counting)
    return seen


def test_load_feedback_entries_limit_parses_only_the_newest_records(tmp_path, monkeypatch):
    for i in range(5):
        _write_feedback(
            tmp_path, f"2026070{i + 1}T100000Z-{i}.md", "up", agent=f"a{i}",
            timestamp=f"2026-07-0{i + 1}T10:00:00+00:00",
        )
    paths = CohortPaths.for_project(tmp_path)
    parses = _count_parses(monkeypatch)
    entries = load_feedback_entries(paths, limit=2)
    assert parses["n"] == 2  # three older files were never opened
    assert [e["agent"] for e in entries] == ["a3", "a4"]  # newest, still oldest-first


def test_load_session_entries_limit_parses_only_the_newest_records(tmp_path, monkeypatch):
    for i in range(5):
        _write_session(tmp_path, f"2026070{i + 1}T100000Z-{i}.md",
                       f"2026-07-0{i + 1}T10:00:00+00:00", branch=f"b{i}")
    paths = CohortPaths.for_project(tmp_path)
    parses = _count_parses(monkeypatch)
    entries = load_session_entries(paths, limit=2)
    assert parses["n"] == 2
    assert [e["branch"] for e in entries] == ["b3", "b4"]


def test_loaders_without_a_limit_still_read_every_record(tmp_path):
    for i in range(3):
        _write_feedback(tmp_path, f"2026070{i + 1}T100000Z-{i}.md", "up", agent="counsel")
        _write_session(tmp_path, f"2026070{i + 1}T100000Z-{i}.md",
                       f"2026-07-0{i + 1}T10:00:00+00:00")
    paths = CohortPaths.for_project(tmp_path)
    assert len(load_feedback_entries(paths)) == 3
    assert len(load_session_entries(paths)) == 3


def test_loader_limit_of_zero_selects_nothing(tmp_path, monkeypatch):
    _write_feedback(tmp_path, "20260701T100000Z-a.md", "up", agent="counsel")
    paths = CohortPaths.for_project(tmp_path)
    parses = _count_parses(monkeypatch)
    assert load_feedback_entries(paths, limit=0) == []
    assert parses["n"] == 0


# === unquoted (YAML-native) timestamps (#299 item 1) ========================


def test_agent_scorecards_trend_accepts_an_unquoted_yaml_datetime():
    """A hand-edited record writes ``timestamp: 2026-07-01T09:00:00Z`` unquoted, which
    PyYAML hands back as a ``datetime``. It must count in the trend, not only totals."""
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    entries = [
        {"rating": "up", "agent": "counsel", "command": None,
         "timestamp": datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)},
        {"rating": "up", "agent": "counsel", "command": None,
         "timestamp": "2026-07-01T15:00:00+00:00"},
    ]
    card = agent_scorecards(entries, now=now)[0]
    assert card["up"] == 2
    assert card["trend"] == [{"date": "2026-07-01", "up": 2, "down": 0}]


def test_agent_scorecards_trend_accepts_a_naive_datetime_as_utc():
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    entries = [{"rating": "down", "agent": "counsel", "command": None,
                "timestamp": datetime(2026, 7, 2, 9, 0)}]
    card = agent_scorecards(entries, now=now)[0]
    assert card["trend"] == [{"date": "2026-07-02", "up": 0, "down": 1}]


def test_normalized_timestamp_makes_every_stored_form_one_sortable_string():
    from cohort.improve import normalized_timestamp

    unquoted = normalized_timestamp(datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc))
    quoted = normalized_timestamp("2026-07-01T10:00:00Z")
    assert unquoted == "2026-07-01T09:00:00+00:00"
    assert quoted == "2026-07-01T10:00:00+00:00"
    assert unquoted < quoted  # plain string comparison is chronological
    assert normalized_timestamp("not-a-date") is None
    assert normalized_timestamp(None) is None


def test_loaders_normalize_an_unquoted_yaml_timestamp(tmp_path):
    """PyYAML returns a ``datetime`` for an unquoted record. The loaders hand back one
    comparable, JSON-safe shape so no consumer sorts str against datetime (#299)."""
    import json

    sessions = tmp_path / ".cohort" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "20260701T090000Z-a.md").write_text(
        "---\ntimestamp: 2026-07-01T09:00:00Z\nauthor: dev\nbranch: hand\n---\nbody\n",
        encoding="utf-8")
    (sessions / "20260701T100000Z-b.md").write_text(
        "---\ntimestamp: '2026-07-01T10:00:00+00:00'\nauthor: dev\nbranch: tool\n---\nbody\n",
        encoding="utf-8")
    entries = load_session_entries(CohortPaths.for_project(tmp_path))
    assert [e["timestamp"] for e in entries] == [
        "2026-07-01T09:00:00+00:00", "2026-07-01T10:00:00+00:00",
    ]
    sorted(entries, key=lambda e: e["timestamp"] or "")  # would raise TypeError before
    json.dumps(entries)  # a datetime would raise here

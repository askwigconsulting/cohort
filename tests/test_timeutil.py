"""The `Z` suffix must parse on every Python this project supports.

`datetime.fromisoformat` only accepted the `Z` UTC designator from 3.11, and the floor is
3.10. Cohort writes its own timestamps with a trailing `Z`, so on the floor `weekly-report`
crashed and the feedback trend silently dropped every record. These tests fail on 3.10 if
the shim is removed, and pass everywhere with it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cohort.timeutil import parse_iso8601, parse_iso8601_utc


def test_trailing_z_parses_as_utc() -> None:
    """The exact shape Cohort writes, and the one that broke the 3.10 floor."""
    assert parse_iso8601("2026-06-01T12:00:00Z") == datetime(
        2026, 6, 1, 12, 0, tzinfo=timezone.utc
    )


def test_lowercase_z_parses_too() -> None:
    assert parse_iso8601("2026-06-01T12:00:00z").utcoffset() == timezone.utc.utcoffset(None)


def test_an_explicit_offset_is_left_alone() -> None:
    """Only a *trailing* Z is rewritten — a real offset must survive untouched."""
    parsed = parse_iso8601("2026-06-01T12:00:00+02:00")
    assert parsed.utcoffset().total_seconds() == 7200


def test_a_naive_timestamp_stays_naive() -> None:
    """`parse_iso8601` reports what it was given; deciding what a missing offset means is
    the caller's policy, and the two callers differ."""
    assert parse_iso8601("2026-06-01T12:00:00").tzinfo is None


def test_utc_helper_reads_a_naive_timestamp_as_utc_not_local_time() -> None:
    """A record that lost its offset must not silently shift by the reader's zone."""
    assert parse_iso8601_utc("2026-06-01T12:00:00") == datetime(
        2026, 6, 1, 12, 0, tzinfo=timezone.utc
    )


def test_utc_helper_normalises_an_offset_to_utc() -> None:
    assert parse_iso8601_utc("2026-06-01T14:00:00+02:00") == datetime(
        2026, 6, 1, 12, 0, tzinfo=timezone.utc
    )


def test_malformed_input_still_raises() -> None:
    """Callers that treat a hand-edited record as recoverable catch this themselves — so
    the shim must not swallow a genuinely bad value."""
    with pytest.raises(ValueError):
        parse_iso8601("not a timestamp")


def test_reports_window_accepts_a_z_suffixed_bound() -> None:
    """End-to-end at the call site that crashed: `cohort weekly-report --until <Z stamp>`."""
    from cohort.reports import resolve_window

    start, end = resolve_window("weekly", None, "2026-06-01T12:00:00Z")
    assert end == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert (end - start).days == 7


def test_feedback_timestamps_are_not_silently_dropped() -> None:
    """The quieter half of the bug: improve.py caught ValueError and returned None, so on
    3.10 every feedback record vanished from the trend instead of failing loudly."""
    from cohort.improve import _parse_timestamp

    assert _parse_timestamp("2026-06-01T12:00:00Z") == datetime(
        2026, 6, 1, 12, 0, tzinfo=timezone.utc
    )

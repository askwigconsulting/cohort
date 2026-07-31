"""Audit r3 follow-ups: M3 (response size cap), M6 (transcript race), M8 (rollback ledger).

Each is a case where something unbounded or unreported could bite only under conditions the
happy path never reaches — a hostile response body, two concurrent reviews, a read-only
state dir.
"""

from __future__ import annotations

import json

import pytest

from cohort import update
from cohort.cli import _next_transcript_path
from cohort.engines.xai import ResponseTooLargeError, _read_bounded


class _Body:
    """A response double whose `read(size)` mirrors http.client.HTTPResponse."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int | None = None) -> bytes:
        return self._payload if size is None else self._payload[:size]


# --------------------------------------------------------------------------- #
# M3 — a client-side ceiling on the buffered response body
# --------------------------------------------------------------------------- #


def test_a_response_within_the_cap_is_returned_whole() -> None:
    assert _read_bounded(_Body(b"x" * 100), limit=1000) == b"x" * 100


def test_a_response_exactly_at_the_cap_is_allowed() -> None:
    """The boundary must be inclusive, or the cap silently rejects a legal maximum."""
    assert _read_bounded(_Body(b"x" * 1000), limit=1000) == b"x" * 1000


def test_an_oversized_response_is_refused_rather_than_buffered() -> None:
    """`max_tokens` is a request hint the server may ignore, so it is not a resource
    bound. A hostile or misconfigured endpoint must not be able to make the client
    buffer without limit on a path the user is waiting on."""
    with pytest.raises(ResponseTooLargeError):
        _read_bounded(_Body(b"x" * 1001), limit=1000)


# --------------------------------------------------------------------------- #
# M6 — transcripts are the record of what was egressed; they must not collide
# --------------------------------------------------------------------------- #


def test_transcript_paths_are_unique_across_concurrent_reservations(tmp_path) -> None:
    """Observed for real during audit r3: five parallel reviews produced four transcripts.

    Scanning for the highest index and returning `highest + 1` is check-then-act — every
    caller that has not yet written sees the same highest. Reserving the slot makes the
    name unique even when nobody has written anything yet.
    """
    paths = [_next_transcript_path(tmp_path, None) for _ in range(5)]
    assert len(set(paths)) == 5, paths
    assert all(p.exists() for p in paths)  # reserved, not merely computed


def test_transcript_numbering_continues_past_existing_files(tmp_path) -> None:
    tdir = tmp_path / ".cohort" / "engine-transcripts"
    tdir.mkdir(parents=True)
    (tdir / "0001.jsonl").write_text("{}", encoding="utf-8")
    (tdir / "0002.jsonl").write_text("{}", encoding="utf-8")
    assert _next_transcript_path(tmp_path, None).name == "0003.jsonl"


def test_an_explicit_override_is_honoured_and_not_reserved(tmp_path) -> None:
    target = tmp_path / "somewhere" / "mine.jsonl"
    assert _next_transcript_path(tmp_path, target) == target
    assert not target.exists()  # the caller owns an explicit path entirely


# --------------------------------------------------------------------------- #
# M8 — a rollback point that failed to record must not fail silently
# --------------------------------------------------------------------------- #


def test_recording_a_rollback_point_reports_success(tmp_path) -> None:
    assert update._record_update(tmp_path, "aaa", "bbb", "update", at="2026-07-31") is True
    entries = json.loads(update._history_path(tmp_path).read_text(encoding="utf-8"))
    assert entries["entries"][-1]["from"] == "aaa"


def test_an_unwritable_ledger_reports_failure_instead_of_passing_silently(
    tmp_path, monkeypatch
) -> None:
    """The update itself must still succeed — the clone has already moved — but the
    caller has to know the advertised one-shot undo is gone, rather than discovering it
    later when `cohort rollback` says there is nothing to roll back to."""
    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    assert update._record_update(tmp_path, "aaa", "bbb", "update", at="2026-07-31") is False


def test_a_recorded_update_is_what_a_bare_rollback_returns_to(tmp_path) -> None:
    update._record_update(tmp_path, "aaa", "bbb", "update", at="2026-07-31")
    assert update._last_rollback_point(tmp_path) == "aaa"


def test_no_recorded_update_means_no_rollback_point(tmp_path) -> None:
    assert update._last_rollback_point(tmp_path) is None

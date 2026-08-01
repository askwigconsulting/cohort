"""ISO-8601 parsing that works on every Python this project supports.

``datetime.fromisoformat`` only learned to accept the ``Z`` UTC designator in **3.11**,
and this project's floor is 3.10 (``requires-python``). Cohort writes its own timestamps
with a trailing ``Z``, so every one of them was unparseable on the floor — which failed
two different ways depending on the caller:

* ``reports.py`` did not guard the call, so ``cohort weekly-report`` and
  ``monthly-report`` crashed outright with ``Invalid isoformat string``.
* ``improve.py`` caught ``ValueError`` and returned ``None``, so the feedback trend
  silently dropped *every* record rather than failing — the quieter and worse of the two.

Both now go through :func:`parse_iso8601`, so the floor is honoured in one place instead
of being rediscovered at each call site.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting a trailing ``Z`` on any supported Python.

    ``Z`` is rewritten to the equivalent ``+00:00`` offset that 3.10's parser understands.
    Only a *trailing* ``Z`` is touched, so an offset that is already explicit is passed
    through untouched.

    Args:
        value: The timestamp text, e.g. ``"2026-06-01T12:00:00Z"``.

    Returns:
        The parsed datetime, naive or aware exactly as the input was — callers decide what
        a missing offset means, since that policy differs between them.

    Raises:
        ValueError: if the text is not a valid ISO-8601 timestamp. Callers that treat a
            malformed record as recoverable catch this themselves.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def parse_iso8601_utc(value: str) -> datetime:
    """:func:`parse_iso8601`, with a naive result read as UTC and the result normalised.

    The common case: Cohort's own timestamps are UTC, and a record that lost its offset
    should not silently shift by the reader's local zone.
    """
    parsed = parse_iso8601(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

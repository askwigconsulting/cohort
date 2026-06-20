"""Structured key=value logging.

Every log line carries the Cohort-standard field set in a stable order
(spec §1.5)::

    component=<...> action=<...> scope=<...> ide=<...> artifact=<...> status=<...> duration_ms=<...>

Logs are written to stderr so stdout stays clean for ``--json`` payloads.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

# Stable field order for every structured log line.
LOG_FIELDS = ("component", "action", "scope", "ide", "artifact", "status", "duration_ms")


def format_log_line(
    *,
    component: str,
    action: str,
    scope: str = "-",
    ide: str = "-",
    artifact: str = "-",
    status: str = "-",
    duration_ms: int = 0,
) -> str:
    """Render one structured log line as space-separated ``key=value`` pairs."""
    values: dict[str, Any] = {
        "component": component,
        "action": action,
        "scope": scope,
        "ide": ide,
        "artifact": artifact,
        "status": status,
        "duration_ms": duration_ms,
    }
    return " ".join(f"{field}={values[field]}" for field in LOG_FIELDS)


def emit_log(*, stream: TextIO | None = None, **fields: Any) -> None:
    """Write one structured log line to ``stream`` (default stderr)."""
    out = stream if stream is not None else sys.stderr
    print(format_log_line(**fields), file=out)

"""The supervision dial — how often Cohort stops to ask, on a fixed safety floor.

This is a *supervision* control, not a safeguard control: it tunes discretionary
friction (per-step confirmations, plan-approval pauses, signoff granularity, whether to
run a cross-examination consult) over **cheaply-reversible** actions. It does NOT — and
structurally cannot — disable the fixed floor: the human PR-merge gate, the code-enforced
egress/secret/footprint gates, the advisory tool-strip, worktree isolation for external
diffs, coordinator verification of every foreign diff, or the operational hard-limits.
"Full autopilot, no checks" is therefore incoherent — merge is human by construction — so
the honest ceiling is *Autopilot up-to-PR*: the machine does everything up to opening the
PR, and a human still merges.

Safety properties of the level itself:
- **Machine-local, never synced.** The level lives under the global ``state/`` dir, which
  ``my-office sync`` never touches — so a pulled/committed config can never *raise* a
  machine's autonomy (a synced autopilot flag would be a privilege-escalation vector).
- **Fail-closed.** An unreadable or unrecognized value resolves to the *safest* level
  (``paired``), never to autopilot.
- **The confirm-for-irreversible/outward/destructive prompt is not on the dial** — that
  stays stop-and-ask at every level (see the ``autonomy-levels`` memory and
  ``operational-hard-limits``).
"""

from __future__ import annotations

from pathlib import Path

from .install_model import CohortPaths

# Ascending autonomy. Index order matters: a repo/flag may only ever *lower* autonomy
# relative to the machine level (min-rule), never raise it.
LEVELS: tuple[str, ...] = ("paired", "guided", "supervised", "autopilot")
DEFAULT_LEVEL = "guided"   # the recommended everyday level when none is set
SAFEST_LEVEL = "paired"    # what a malformed/unreadable value fails closed to

_DESCRIPTIONS = {
    "paired": "confirm every step; plan and per-task signoff shown and awaited (max friction)",
    "guided": "no prompts for routine reversible edits, but pause for the plan and every "
    "commit/branch/PR and hard-limit-adjacent action (recommended default)",
    "supervised": "run reversible in-repo work without prompts; batch signoff at the end; "
    "the plan proceeds unless you object in-turn",
    "autopilot": "run end-to-end and stop at the PR — no discretionary confirmations; "
    "coordinator verification and the whole safety floor still hold (never auto-merges)",
}


def _level_file(home: Path) -> Path:
    return CohortPaths.for_global(home).state / "autonomy"


def read_autonomy_level(home: Path) -> str:
    """The current machine-local autonomy level. Fail-closed: an absent file is the
    recommended default (``guided``); an unreadable or unrecognized value is the safest
    level (``paired``), never autopilot."""
    path = _level_file(home)
    try:
        raw = path.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return DEFAULT_LEVEL
    except OSError:
        return SAFEST_LEVEL
    return raw if raw in LEVELS else SAFEST_LEVEL


def set_autonomy_level(home: Path, level: str) -> str:
    """Set the machine-local autonomy level. Returns the normalized level.

    Raises:
        ValueError: ``level`` is not one of ``LEVELS``.
    """
    normalized = level.strip().lower()
    if normalized not in LEVELS:
        raise ValueError(
            f"unknown autonomy level {level!r}; choose one of {', '.join(LEVELS)}"
        )
    path = _level_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized + "\n", encoding="utf-8")
    return normalized


def describe(level: str) -> str:
    """A one-line description of a level (empty string if unknown)."""
    return _DESCRIPTIONS.get(level, "")


def clamp(requested: str, ceiling: str) -> str:
    """Lower ``requested`` to ``ceiling`` if it asks for more autonomy — a request may
    only ever reduce autonomy relative to the machine level, never raise it."""
    if requested not in LEVELS:
        return SAFEST_LEVEL
    if ceiling not in LEVELS:
        return requested
    return requested if LEVELS.index(requested) <= LEVELS.index(ceiling) else ceiling

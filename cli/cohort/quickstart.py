"""The canonical clone-and-go journey as a single source of truth (P9 [R2]).

``QUICKSTART_STEPS`` is the one definition the README renders from and the
full-system e2e executes (each step wrapped with test scaffolding). A test
asserts the README's fenced command block equals this list, so docs and the
tool can't drift.
"""

from __future__ import annotations

# Bare commands a new team runs, in order. (`git clone` precedes these; the e2e
# starts at install since the checkout is the fixture.)
QUICKSTART_STEPS = [
    "cohort install --ide claude,codex,cursor",
    "cohort init",
    "cohort add-specialist --name data-modeler --display-name DataModeler --department Data --description 'Schema and data modeling.'",
    "cohort snapshot",
    "cohort weekly-report",
    "cohort feedback --rating up --agent data-modeler",
    "cohort propose-improvement",
    "cohort submit-proposals",
]


def quickstart_verbs() -> list[str]:
    """The subcommand of each step (the binding the e2e checks its sequence against)."""
    return [step.split()[1] for step in QUICKSTART_STEPS]

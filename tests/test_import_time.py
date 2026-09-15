"""Pin the ``cohort.cli`` import tree (#295).

Every hook process pays this module's import cost before it runs a line of code,
and ``~/.claude/settings.json`` wires six of them — five on ``SessionStart`` and
``working-capture`` on every ``Stop``. So the modules that only a handful of
commands ever touch must be imported inside those command bodies, not at module
level.

This is a **structural** pin, not a timing one: it asserts which modules are in
the tree, never how many milliseconds they take (timings are noisy on CI). The
companion measurement below prints the current shape for the record and asserts
nothing about it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import cohort

# `import time: self | cumulative |   <indent><module>` on stderr under -X importtime.
_IMPORTTIME_LINE = re.compile(r"^import time:\s+(\d+) \|\s+(\d+) \|(\s*)(\S+)$")

# The modules deferred into their command bodies. Each is used by at most a
# handful of commands; none is reachable from a hook target.
DEFERRED_MODULES = frozenset({
    "cohort.compile",
    "cohort.dashboard",
    "cohort.install",
    "cohort.improve",
    "cohort.update",
    "cohort.adopt",
    "cohort.reference",
    "cohort.gc",
    "cohort.trial",
    "cohort.engines.cli_doer",
    "cohort.engines.ratchet",
    "cohort.engines.patch_proposal",
    "cohort.engines.codex_cli",
    # Proxies: each of these imports `compile`/`install`/`update` at *its* module
    # level, so deferring the heavy three in cli.py only works if these are
    # deferred too.
    "cohort.office_setup",
    "cohort.roster",
    "cohort.specialists",
})

# Names that must stay bound at `cohort.cli` import time, whatever it costs:
# the two module-level attributes the suite patches, and the one constant that a
# Typer option default reads when the decorator runs.
REQUIRED_MODULE_LEVEL = ("find_repo_root", "engine_xai", "DEFAULT_DAYS")


def _import_tree() -> list[tuple[str, int, int]]:
    """Import ``cohort.cli`` in a fresh interpreter under ``-X importtime`` and
    return ``(module, cumulative microseconds, depth)`` for everything it pulled in."""
    env = dict(os.environ)
    cli_dir = Path(cohort.__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(cli_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import cohort.cli"],
        capture_output=True, text=True, env=env, check=True,
    )
    tree: list[tuple[str, int, int]] = []
    for line in proc.stderr.splitlines():
        match = _IMPORTTIME_LINE.match(line)
        if match is not None:
            tree.append((match.group(4), int(match.group(2)), len(match.group(3)) // 2))
    assert any(name == "cohort.cli" for name, _cost, _depth in tree), proc.stderr[-2000:]
    return tree


def test_deferred_modules_are_absent_from_the_cli_import_tree() -> None:
    eager = sorted(DEFERRED_MODULES.intersection(name for name, _c, _d in _import_tree()))
    assert eager == [], (
        "these modules are imported when `cohort.cli` is imported, so every hook "
        f"process pays for them: {eager}. Move the import into the command body "
        "that uses it."
    )


def test_patched_and_decoration_time_names_stay_module_level() -> None:
    import cohort.cli as cli

    missing = [name for name in REQUIRED_MODULE_LEVEL if not hasattr(cli, name)]
    assert missing == [], (
        f"{missing} must stay importable at `cohort.cli` module level: the suite "
        "patches them there, or a decorator reads them when the module is imported."
    )


def test_import_cost_composition_is_recorded() -> None:
    """Print where `import cohort.cli` spends its time. Recorded, never asserted —
    wall-clock numbers vary by machine and by CI runner."""
    tree = _import_tree()
    total = next(cost for name, cost, depth in tree if name == "cohort.cli" and depth == 0)
    typer_cost = max(
        (cost for name, cost, _d in tree if name in ("typer", "click", "rich")), default=0
    )
    # Only the direct children of cohort.cli: one row per subtree it pays for.
    biggest = sorted(
        ((cost, name) for name, cost, depth in tree if depth == 1), reverse=True
    )[:8]
    print(f"\nimport cohort.cli: {total / 1000:.1f} ms total, "
          f"{typer_cost / 1000:.1f} ms of it typer "
          f"({(total - typer_cost) / 1000:.1f} ms excluding typer)")
    for cost, name in biggest:
        print(f"  {cost / 1000:7.1f} ms  {name}")
    assert total > 0

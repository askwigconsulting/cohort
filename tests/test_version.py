"""The package version is single-sourced and exposed via `cohort --version`."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import cohort

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    """The `[project] version` from pyproject, read without `tomllib`.

    `tomllib` is 3.11+, and this project's floor is 3.10 (`requires-python`), so importing
    it at module scope made this whole module fail to import on 3.10 — taking the version
    check down on the exact interpreter it most needed to run on. Only one scalar is
    needed here, so a scoped scan beats adding a parser dependency.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.match(r'^version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    raise AssertionError("no [project] version found in pyproject.toml")


def test_version_is_single_sourced_with_pyproject():
    assert cohort.__version__ == _pyproject_version()


def test_version_flag_prints_version_and_exits_0():
    proc = subprocess.run(
        [sys.executable, "-m", "cohort", "--version"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == cohort.__version__


def test_changelog_documents_the_current_version():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{cohort.__version__}]" in changelog  # current release has a section

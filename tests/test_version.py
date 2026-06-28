"""The package version is single-sourced and exposed via `cohort --version`."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import cohort

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_is_single_sourced_with_pyproject():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert cohort.__version__ == pyproject["project"]["version"]


def test_version_flag_prints_version_and_exits_0():
    proc = subprocess.run(
        [sys.executable, "-m", "cohort", "--version"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == cohort.__version__


def test_changelog_documents_the_current_version():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{cohort.__version__}]" in changelog  # current release has a section

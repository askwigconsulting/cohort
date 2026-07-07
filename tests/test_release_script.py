"""#127: the release helper (scripts/release.py) bumps the version in lockstep and
rolls the CHANGELOG, producing a state that satisfies the same invariants
tests/test_version.py enforces — and fails closed on bad input."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Load scripts/release.py (not an installed module) by path.
_spec = importlib.util.spec_from_file_location("release_tool", REPO_ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def _fixture_repo(root: Path, *, version: str = "0.4.0", unreleased: str = "### Added\n- A thing.\n") -> Path:
    (root / "cli" / "cohort").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "cohort"\nversion = "{version}"\n', encoding="utf-8")
    (root / "cli" / "cohort" / "__init__.py").write_text(
        f'"""cohort"""\n\n__version__ = "{version}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        f"{unreleased}\n"
        f"## [{version}] — 2026-01-01 · Prior\n\n### Added\n- Older thing.\n\n"
        f"[Unreleased]: https://github.com/askwigconsulting/cohort/compare/v{version}...HEAD\n"
        f"[{version}]: https://github.com/askwigconsulting/cohort/compare/v0.3.0...v{version}\n",
        encoding="utf-8",
    )
    return root


def test_cut_release_bumps_both_version_files_in_lockstep(tmp_path):
    repo = _fixture_repo(tmp_path)
    release.cut_release(repo, "0.5.0", "Org profiles", "2026-08-01")
    assert release.read_pyproject_version(repo) == "0.5.0"
    assert release.read_init_version(repo) == "0.5.0"


def test_cut_release_dates_the_section_empties_unreleased_and_adds_link(tmp_path):
    repo = _fixture_repo(tmp_path)
    release.cut_release(repo, "0.5.0", "Org profiles", "2026-08-01")
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.5.0] — 2026-08-01 · Org profiles" in text
    assert "- A thing." in text  # the unreleased entry moved under the dated heading
    # [Unreleased] is now empty (its heading is immediately followed by the new one)
    assert release._unreleased_body(text).strip() == ""
    assert "[0.5.0]: https://github.com/askwigconsulting/cohort/compare/v0.4.0...v0.5.0" in text
    assert "[Unreleased]: https://github.com/askwigconsulting/cohort/compare/v0.5.0...HEAD" in text


def test_cut_release_output_satisfies_the_version_check(tmp_path):
    """The produced state passes the same invariants test_version.py asserts."""
    repo = _fixture_repo(tmp_path)
    release.cut_release(repo, "0.5.0", "Org profiles", "2026-08-01")
    release.check_consistency(repo)  # raises if version files disagree or no CHANGELOG section


def test_cut_release_refuses_empty_unreleased(tmp_path):
    repo = _fixture_repo(tmp_path, unreleased="")  # nothing accumulated
    with pytest.raises(release.ReleaseError, match="empty"):
        release.cut_release(repo, "0.5.0", "X", "2026-08-01")
    assert release.read_pyproject_version(repo) == "0.4.0"  # unchanged — failed closed


def test_cut_release_refuses_non_increasing_version(tmp_path):
    repo = _fixture_repo(tmp_path, version="0.4.0")
    with pytest.raises(release.ReleaseError, match="greater"):
        release.cut_release(repo, "0.4.0", "X", "2026-08-01")


def test_cut_release_refuses_bad_semver(tmp_path):
    repo = _fixture_repo(tmp_path)
    with pytest.raises(release.ReleaseError, match="X.Y.Z"):
        release.cut_release(repo, "0.5", "X", "2026-08-01")


def test_check_consistency_passes_on_the_real_repo():
    """Ties the helper to reality: the shipped repo is version-consistent."""
    release.check_consistency(REPO_ROOT)

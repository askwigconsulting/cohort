#!/usr/bin/env python3
"""Cut a Cohort release: bump the version and roll the CHANGELOG in one step.

Releases stalled once (0.3.0 → ten merged PRs) because the version lives in two
files and the CHANGELOG follows Keep-a-Changelog: features accumulate under
`[Unreleased]` and a *release* is what dates them and bumps the number. Doing
that by hand is easy to forget and easy to get subtly wrong (mismatched version
files, missing compare link). This script does the mechanical parts atomically:

  * bump `version` in pyproject.toml AND `__version__` in cli/cohort/__init__.py,
    kept in lockstep (tests/test_version.py enforces they match);
  * move the `[Unreleased]` backlog into a dated `## [X.Y.Z] — <date> · <title>`
    section, leaving a fresh empty `[Unreleased]`;
  * update the compare links at the foot of the CHANGELOG.

It does NOT commit, push, or tag — it edits files and prints the exact git
commands, so a human stays in the loop (matching Cohort's human-gated posture).

Usage:
    python scripts/release.py 0.5.0 --title "Org profiles"
    python scripts/release.py 0.5.0 --title "Org profiles" --date 2026-08-01
    python scripts/release.py --check          # verify version files + CHANGELOG agree
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(Exception):
    """A release precondition failed; the message is shown to the maintainer."""


def _pyproject_version_path(root: Path) -> Path:
    return root / "pyproject.toml"


def _init_path(root: Path) -> Path:
    return root / "cli" / "cohort" / "__init__.py"


def _changelog_path(root: Path) -> Path:
    return root / "CHANGELOG.md"


def read_pyproject_version(root: Path) -> str:
    """Return the `[project].version` string from pyproject.toml (the source of truth)."""
    text = _pyproject_version_path(root).read_text(encoding="utf-8")
    m = re.search(r'(?m)^version = "(\d+\.\d+\.\d+)"', text)
    if not m:
        raise ReleaseError("could not find `version = \"X.Y.Z\"` in pyproject.toml")
    return m.group(1)


def read_init_version(root: Path) -> str:
    text = _init_path(root).read_text(encoding="utf-8")
    m = re.search(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"', text)
    if not m:
        raise ReleaseError("could not find `__version__ = \"X.Y.Z\"` in cli/cohort/__init__.py")
    return m.group(1)


def _version_tuple(v: str) -> tuple[int, int, int]:
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


def bump_version_files(root: Path, new: str) -> None:
    """Rewrite the version string in pyproject.toml and __init__.py to ``new``."""
    py = _pyproject_version_path(root)
    py.write_text(
        re.sub(r'(?m)^version = "\d+\.\d+\.\d+"', f'version = "{new}"',
               py.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
    )
    init = _init_path(root)
    init.write_text(
        re.sub(r'(?m)^__version__ = "\d+\.\d+\.\d+"', f'__version__ = "{new}"',
               init.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
    )


def _unreleased_body(changelog: str) -> str:
    """The text between `## [Unreleased]` and the next `## [` heading."""
    m = re.search(r"(?ms)^## \[Unreleased\]\n(.*?)(?=^## \[)", changelog)
    if not m:
        raise ReleaseError("CHANGELOG.md has no `## [Unreleased]` section")
    return m.group(1)


def update_changelog(root: Path, new: str, title: str, date: str, old: str) -> None:
    """Move the `[Unreleased]` backlog into a dated section and fix the compare links."""
    path = _changelog_path(root)
    text = path.read_text(encoding="utf-8")

    if not _unreleased_body(text).strip():
        raise ReleaseError(
            "`## [Unreleased]` is empty — nothing to release. Add CHANGELOG entries first."
        )
    if f"## [{new}]" in text:
        raise ReleaseError(f"CHANGELOG already has a `## [{new}]` section")

    # Insert the dated heading right after `## [Unreleased]`, leaving it empty above.
    text, n = re.subn(
        r"(?m)^## \[Unreleased\]\n\n",
        f"## [Unreleased]\n\n## [{new}] — {date} · {title}\n\n",
        text, count=1,
    )
    if n != 1:
        raise ReleaseError("could not locate the `## [Unreleased]` heading to split")

    # Compare links: point Unreleased at the new tag and add the new tag's own link.
    base_m = re.search(r"\[Unreleased\]: (\S+)/compare/v[\d.]+\.\.\.HEAD", text)
    if not base_m:
        raise ReleaseError("could not find the `[Unreleased]:` compare link to update")
    base = base_m.group(1)  # e.g. https://github.com/askwigconsulting/cohort
    text, n = re.subn(
        r"(?m)^\[Unreleased\]: \S+/compare/v[\d.]+\.\.\.HEAD$",
        f"[Unreleased]: {base}/compare/v{new}...HEAD\n"
        f"[{new}]: {base}/compare/v{old}...v{new}",
        text, count=1,
    )
    if n != 1:
        raise ReleaseError("could not rewrite the `[Unreleased]:` compare link")

    path.write_text(text, encoding="utf-8")


def cut_release(root: Path, new: str, title: str, date: str) -> str:
    """Validate, then bump the version files and roll the CHANGELOG. Returns the old version."""
    if not _SEMVER.match(new):
        raise ReleaseError(f"version must be X.Y.Z, got {new!r}")
    old = read_pyproject_version(root)
    if _version_tuple(new) <= _version_tuple(old):
        raise ReleaseError(f"new version {new} must be greater than current {old}")
    if not title.strip():
        raise ReleaseError("a release --title is required (it names the CHANGELOG section)")

    update_changelog(root, new, title, date, old)  # fails closed before touching version files
    bump_version_files(root, new)
    return old


def check_consistency(root: Path) -> None:
    """Assert the two version files agree and the current version has a CHANGELOG section."""
    py, init = read_pyproject_version(root), read_init_version(root)
    if py != init:
        raise ReleaseError(f"version mismatch: pyproject={py} __init__={init}")
    changelog = _changelog_path(root).read_text(encoding="utf-8")
    if f"[{py}]" not in changelog:
        raise ReleaseError(f"CHANGELOG.md has no section for the current version {py}")
    print(f"OK — version {py} is single-sourced and documented in the CHANGELOG.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut a Cohort release (version bump + CHANGELOG).")
    parser.add_argument("version", nargs="?", help="new version, e.g. 0.5.0")
    parser.add_argument("--title", default="", help="release theme for the CHANGELOG heading")
    parser.add_argument("--date", default=None, help="release date YYYY-MM-DD (default: today)")
    parser.add_argument("--check", action="store_true",
                        help="only verify version files + CHANGELOG agree; make no changes")
    args = parser.parse_args(argv)

    try:
        if args.check:
            check_consistency(REPO_ROOT)
            return 0
        if not args.version:
            parser.error("a version is required (or use --check)")
        date = args.date or datetime.date.today().isoformat()
        old = cut_release(REPO_ROOT, args.version, args.title, date)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1

    v = args.version
    print(f"Cut {old} → {v}. Review the diff, then:\n"
          f"  git checkout -b release/{v}\n"
          f'  git add -A && git commit -m "Release {v} — {args.title}"\n'
          f"  # open a PR; after it merges to master, tag the release commit:\n"
          f'  git tag -a v{v} -m "Release {v} — {args.title}" && git push origin v{v}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cohort.loader import load_artifact_text
from cohort.schema import FileResult, validate_load_result

FIXTURES = Path(__file__).parent / "fixtures"
VALID = FIXTURES / "valid"
INVALID = FIXTURES / "invalid"


def _symlinks_creatable() -> bool:
    """True if this host can actually create a symlink (Windows needs Developer
    Mode/admin; POSIX always can)."""
    try:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "t"
            target.write_text("x", encoding="utf-8")
            (Path(d) / "l").symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False


# For tests that directly create symlinks or assert POSIX symlink mechanics.
# Cohort never emits LINK ops on Windows (copy-mode is the default there), and the
# symlink semantics these assert (readlink normalization, reverse removal) differ
# on Windows even when a symlink *can* be created — so skip on nt outright, and on
# any POSIX host that can't create one.
requires_symlinks = pytest.mark.skipif(
    os.name == "nt" or not _symlinks_creatable(),
    reason="symlink mechanics are POSIX-only (Cohort uses copy-mode on Windows)",
)


@pytest.fixture(autouse=True)
def _never_write_the_real_home(tmp_path_factory, monkeypatch) -> None:
    """Keep the CLI-health marker out of the developer's (and the runner's) real home.

    ``note_cli_broken`` writes to ``~/.cohort/state/`` in production, which is correct —
    but tests that exercise a vendor failure called it for real, creating ``~/.cohort``
    on whatever machine ran the suite. On Windows that was not merely untidy: pytest's
    ``tmp_path`` lives *under* ``$HOME`` there, so the stray directory became a ``.cohort``
    ancestor of every subsequent test's working directory and silently satisfied the
    engine egress provenance guard, turning two fail-closed tests green-then-red.
    """
    from cohort.engines import cli_doer

    marker = tmp_path_factory.mktemp("cohort-state") / cli_doer._CLI_BROKEN_MARKER
    monkeypatch.setattr(cli_doer, "_cli_broken_marker_path", lambda: marker)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def valid_dir() -> Path:
    return VALID


@pytest.fixture
def invalid_dir() -> Path:
    return INVALID


def validate_text(content: str, stem: str = "artifact") -> FileResult:
    """Validate raw artifact text as if its filename stem were ``stem``."""
    return validate_load_result(load_artifact_text(content, name_stem=stem))


def codes(content: str, stem: str = "artifact") -> list[str]:
    """Return the list of error codes produced for raw artifact text."""
    return [e.code for e in validate_text(content, stem).errors]


def code_set(content: str, stem: str = "artifact") -> set[str]:
    return set(codes(content, stem))

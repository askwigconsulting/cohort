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


# For tests that directly create symlinks or assert symlink mechanics. Skips on a
# host where symlinks aren't creatable (Windows without Developer Mode) rather
# than failing — copy-mode is Cohort's default there and is covered elsewhere.
requires_symlinks = pytest.mark.skipif(
    not _symlinks_creatable(),
    reason="symlinks not creatable on this host (Windows without Developer Mode)",
)


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

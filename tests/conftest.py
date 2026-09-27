"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from cohort.engines import patch_proposal
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


# Must match the prefix `cohort.engines.patch_proposal._create_worktree` passes to
# `tempfile.mkdtemp` (and, in production, `cohort.gc._PROPOSAL_PREFIX`).
_PROPOSAL_PREFIX = "cohort-proposal-"


def _reclaim_stray_proposal_worktree(parent: Path) -> None:
    """Best-effort cleanup of one leaked `cohort-proposal-*` directory.

    The repo it was checked out from isn't known to the caller, so it is recovered from
    the worktree's own git linkage (every worktree — detached or not — can resolve its
    shared ``.git`` directory via ``rev-parse --git-common-dir``) and handed to
    :func:`cohort.engines.patch_proposal.cleanup_worktree`, the same function production
    code and most tests already use, so the git worktree registration is removed and not
    just the directory. Falls back to a bare ``rmtree`` when git cannot resolve it (a run
    that failed before checkout ever completed) so a partial leak never survives either.
    Never raises: this runs in fixture teardown, after the test's own outcome is decided.
    """
    worktree = parent / "worktree"
    if worktree.is_dir():
        try:
            proc = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                common_dir = Path(proc.stdout.strip())
                if not common_dir.is_absolute():
                    common_dir = (worktree / common_dir).resolve()
                patch_proposal.cleanup_worktree(common_dir.parent, worktree)
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture(autouse=True)
def _redirect_system_tempdir_into_pytest_space(
    tmp_path_factory, monkeypatch
) -> Iterator[None]:
    """Land anything a test drops into "the system temp dir" inside pytest's own space,
    and reclaim it there when the test ends.

    ``patch_proposal._create_worktree`` (the single creation point every engine path
    uses) runs ``tempfile.mkdtemp(prefix="cohort-proposal-")`` against
    ``tempfile.gettempdir()`` and, on success, deliberately leave the worktree in place
    for a human to review — the same "propose, don't apply" contract `cohort gc` exists
    to reclaim in production. A test that exercises that success path inherits the same
    "leave it" contract but has nowhere of its own to leave it; every one that forgot (or
    intentionally doesn't) call ``cleanup_worktree`` stranded a `cohort-proposal-*`
    directory under the *real* system temp dir, where nothing is pytest-managed and
    nothing ever reclaims it — 369 accumulated in one earlier run and exhausted a tmpfs
    quota mid-suite.

    Redirecting ``tempfile.tempdir`` (which ``tempfile.gettempdir()`` returns verbatim
    once set, on every platform — bypassing ``TMPDIR``/``TMP``/``TEMP`` entirely) to a
    fresh directory under this test's own pytest-managed temp root, and sweeping that same
    directory for any `cohort-proposal-*` leftovers on teardown, means a test that forgets
    to clean up after itself no longer needs fixing one at a time — this generalizes the
    per-file reclaim fixture the ratchet tests used to carry (see git history of
    tests/test_ratchet.py) to the whole suite, so newly written tests are covered too.
    Nothing pre-exists in the freshly minted directory, so the sweep needs no snapshot:
    everything found there was created during this test.

    The `TMPDIR`/`TMP`/`TEMP` env vars are set alongside ``tempfile.tempdir`` only as a
    narrowing belt-and-suspenders for a subprocess that reads them directly instead of
    going through Python's ``tempfile`` module — this only ever shrinks where such a
    subprocess would write, so it does not fight the separate Windows-shape harness that
    points the real ``TMPDIR`` under an empty ``HOME`` for that CI leg.
    """
    redirected = tmp_path_factory.mktemp("systemp")
    monkeypatch.setattr(tempfile, "tempdir", str(redirected))
    for env_var in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(env_var, str(redirected))
    yield
    for parent in redirected.glob(f"{_PROPOSAL_PREFIX}*"):
        _reclaim_stray_proposal_worktree(parent)


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

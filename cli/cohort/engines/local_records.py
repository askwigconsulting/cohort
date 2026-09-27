"""Cohort's own local records — the tracked files no external engine is ever shown.

``.cohort/sessions``, ``feedback``, ``proposals`` and ``state`` are tracked by design but
are never part of the work: session entries carry the git author's real name and email
(``project.py``'s ``author`` field), feedback entries are free text that routinely quotes
code and frustration, proposals are earlier engine payloads, and ``state`` is local
bookkeeping. The user consented to that content living in their own git remote, not to
shipping it to a model vendor, and nothing an engine is asked to do needs it (#275).

Two boundaries consult this module, so one predicate decides for both:

* **Worktree creation** — :func:`cohort.engines.patch_proposal._create_worktree` calls
  :func:`exclude_local_records` on every worktree it makes, so no engine path (one-shot
  or agentic propose, ratchet, CLI doer) can be handed a checkout that still holds them.
* **Agentic reads** — :class:`cohort.engines.xai_agentic.ReadOnlyToolbox` refuses any
  path :func:`is_local_record` names, because its root is often the user's live repo,
  not a worktree.

A standalone, stdlib-only module because both callers need it and they import each other
in one direction only (``patch_proposal`` imports ``xai_agentic``): the predicate cannot
live in either without a cycle.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Repo-relative directory prefixes, lowercase, each ending in ``/``.
EXCLUDED_PREFIXES: tuple[str, ...] = (
    ".cohort/sessions/",
    ".cohort/feedback/",
    ".cohort/proposals/",
    ".cohort/state/",
)

# How many paths to hand one ``git update-index`` invocation. A repo with thousands of
# session entries would otherwise build an argv past the OS limit; git applies each batch
# independently, so splitting changes nothing but the argv length.
_GIT_PATH_BATCH = 100
_GIT_TIMEOUT_SECONDS: float = 30.0


def is_local_record(rel: str) -> bool:
    """True if repo-relative POSIX path ``rel`` is a local record or one of their
    directories (``.cohort/sessions`` itself, not just what is under it — so a listing
    can hide the directory, not only its files).

    **Case is folded.** On a case-insensitive filesystem (macOS and Windows defaults)
    ``.Cohort/Sessions/x.md`` opens the very same file, and ``Path.resolve`` does not
    canonicalise case on macOS, so matching the spelling would let a re-cased path
    straight past the gate. On a case-sensitive filesystem the folded match can over-
    refuse a genuinely distinct ``.Cohort/Sessions`` directory; nothing legitimate lives
    there, and refusing a read is the fail-closed direction.

    Callers must pass a normalised path (no ``./``, no doubled ``/``): this is a prefix
    match, not a path resolver.
    """
    folded = rel.casefold()
    return any(
        folded == prefix.rstrip("/") or folded.startswith(prefix)
        for prefix in EXCLUDED_PREFIXES
    )


def _git(worktree: Path, *args: str) -> None:
    """Run a bounded git command in ``worktree`` (raises on failure). Output is kept as
    bytes and discarded, so nothing here decodes a raw path."""
    subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def tracked_paths(worktree: Path) -> list[str]:
    """Every path in the worktree's index, unfiltered. NUL-delimited so a path with an
    embedded newline is not split.

    Read as **bytes** and decoded with :func:`os.fsdecode`: ``-z`` emits names verbatim,
    and a tracked name that is not UTF-8 (legal on Linux) would otherwise crash the
    decode — and with it every engine path, since all of them run the exclusion. The
    surrogate-escaped str round-trips: ``Path`` operations and subprocess argv re-encode
    it with ``os.fsencode``, so the unlink and ``update-index`` reach the exact bytes git
    listed."""
    raw = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "-z"],
        capture_output=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout
    return [os.fsdecode(rel) for rel in raw.split(b"\0") if rel]


def exclude_local_records(worktree: Path) -> None:
    """Delete Cohort's own local records from a freshly created engine worktree.

    Chosen over ``git sparse-checkout`` deliberately: deleting the checked-out files and
    marking them ``--skip-worktree`` needs no git-version-dependent sparse machinery, and
    it leaves ``git status`` clean and ``git add -A`` free of phantom deletions — so the
    diff the coordinator reviews shows the engine's work and nothing else, and a ratchet
    keep's commit carries the records unchanged rather than deleting them. Without the
    skip-worktree bit, every excluded file would read as deleted by the engine.

    **Residual, stated plainly.** This removes the files from the *checkout*, which is
    what the vendor CLI walks. It does not remove them from the object store the worktree
    is attached to. grok cannot reach that store — the bubblewrap jail binds only the
    worktree, and its ``.git`` file points outside — but codex reads the whole host
    filesystem by design (see :mod:`cohort.engines.cli_doer`), so a codex run that
    deliberately went looking could still restore them. The threat this closes is the
    default one: a routine dispatch shipping the author's name, email and friction notes
    to a vendor without anyone choosing to.

    Args:
        worktree: A detached worktree just created off ``HEAD``.

    Raises:
        subprocess.SubprocessError / OSError: git failed or a file could not be removed;
            the caller must discard the worktree rather than hand it to an engine.
    """
    excluded = [rel for rel in tracked_paths(worktree) if is_local_record(rel)]
    if not excluded:
        return
    for rel in excluded:
        try:
            (worktree / rel).unlink()
        except FileNotFoundError:
            pass
    for index in range(0, len(excluded), _GIT_PATH_BATCH):
        _git(
            worktree,
            "update-index",
            "--skip-worktree",
            "--",
            *excluded[index : index + _GIT_PATH_BATCH],
        )
    _prune_empty_record_dirs(worktree)


def _prune_empty_record_dirs(worktree: Path) -> None:
    """Remove the directories the excluded records left behind, deepest first.

    Cosmetic but worth it: an empty ``.cohort/sessions/`` invites the engine to treat it
    as a place to write. ``rmdir`` refuses a non-empty directory, so an untracked file
    someone left in one is never destroyed."""
    for prefix in EXCLUDED_PREFIXES:
        root = worktree / prefix
        if not root.is_dir():
            continue
        for parent, dirs, _files in os.walk(root, topdown=False):
            for name in dirs:
                try:
                    os.rmdir(os.path.join(parent, name))
                except OSError:
                    pass
        try:
            root.rmdir()
        except OSError:
            pass

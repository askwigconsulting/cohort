"""Portable, stdlib-only advisory file lock for serializing read-modify-write
cycles on Cohort's JSON state across concurrent processes.

Cohort has no long-running daemon: several ``cohort`` invocations (a terminal
command, the dashboard's polling, a session hook) can run at the same time and
each does a load→modify→write on the same small JSON file — the install
manifest, the project registry, or the quarantine stores. Without mutual
exclusion two of them interleave and one update is silently lost. For the
quarantine stores a lost write is not merely lost data: it is a security-gate
bypass (an unreviewed auto-activating artifact escapes quarantine), so the RMW
cycle must be serialized, not merely made crash-atomic.

Mechanism — an ``O_CREAT | O_EXCL`` lock-file:
    Creating a file with ``O_CREAT | O_EXCL`` is atomic and fails if the file
    already exists, on POSIX *and* on Windows (there the flag pair maps to the
    ``CREATE_NEW`` disposition). Exactly one waiter wins the create and owns the
    lock; the rest retry. This is deliberately **not** ``fcntl.flock`` (POSIX
    only) nor ``msvcrt.locking`` (Windows only): the exclusive-create file is the
    single primitive whose semantics are identical on both platforms, which this
    project (Windows + POSIX, Python 3.10+, stdlib-only) requires.

Two hazards the design handles:
  * **A crashed holder** leaves its lock-file behind forever, deadlocking every
    later waiter. A *stale* lock (its mtime older than ``stale`` seconds) is
    therefore stolen — removed, then re-created by the next waiter. ``stale`` is
    set far longer than any real critical section (rewriting a few-KB JSON file
    is milliseconds), so a live holder is never stolen from.
  * **A steal racing the original holder** (it was merely slow, not dead). Each
    holder writes a unique token into its lock-file and, on release, removes the
    file *only if it still carries that token*. So a holder whose stale lock was
    stolen and re-created by someone else never deletes the new owner's lock.

Residual risk (documented, not eliminated): if a holder stalls for longer than
``stale`` (e.g. a process suspended by the OS), another waiter steals the lock
and both may run their critical sections concurrently for a window. The token
check prevents cross-deletion of lock-files but not the concurrent execution
itself. ``stale`` is chosen to make this effectively impossible for the tiny
JSON rewrites guarded here; a workload that could legitimately hold the lock for
tens of seconds must raise ``stale`` accordingly.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

# A lock older than this (by mtime) is presumed abandoned and may be stolen. Set
# far above any real critical section here (small-JSON rewrites are ms).
_DEFAULT_STALE = 15.0
# Max time to wait for the lock before giving up with LockTimeout.
_DEFAULT_TIMEOUT = 30.0
# Retry cadence while waiting on a held lock.
_DEFAULT_POLL = 0.05


class LockTimeout(RuntimeError):
    """The lock could not be acquired within ``timeout`` seconds. Raised from the
    ``with`` entry, so the guarded body never runs when it is raised."""


def _lock_path(target: Union[str, Path]) -> Path:
    """The sibling lock-file for ``target`` (``<target>.lock``)."""
    return Path(str(target) + ".lock")


def _is_stale(lock_path: Path, stale: float) -> bool:
    """True if ``lock_path`` exists and its mtime is older than ``stale`` seconds.
    A vanished lock is not stale (the create will just be retried)."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age > stale


def _steal_if_stale(lock_path: Path, stale: float) -> None:
    """Best-effort removal of a lock presumed abandoned. Re-checks staleness
    immediately before unlinking to narrow the window in which a lock that was
    just refreshed gets stolen. Errors are swallowed — the exclusive create that
    follows is the real arbiter of ownership."""
    if not _is_stale(lock_path, stale):
        return
    try:
        os.unlink(lock_path)
    except OSError:
        pass  # already gone / another waiter stole it first → fine


def _release(lock_path: Path, token: bytes) -> None:
    """Remove our lock-file, but only if it still carries our token — so a holder
    whose stale lock was stolen and re-created by another process never deletes
    that new owner's lock. Best-effort; a release failure never propagates."""
    try:
        current = lock_path.read_bytes()
    except OSError:
        return  # already gone
    if current != token:
        return  # not ours anymore (stolen and re-created) → leave it
    try:
        os.unlink(lock_path)
    except OSError:
        pass


@contextmanager
def file_lock(
    target: Union[str, Path],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    stale: float = _DEFAULT_STALE,
    poll: float = _DEFAULT_POLL,
) -> Iterator[None]:
    """Hold an exclusive advisory lock for ``target`` across the ``with`` body.

    ``target`` names the file whose read-modify-write is being guarded; the lock
    itself is a sibling ``<target>.lock`` created in the same directory, so that
    directory must already exist. Raises :class:`LockTimeout` if the lock cannot
    be acquired within ``timeout`` seconds — the guarded body then never runs, so
    callers for whom losing the write is worse than blocking should let it
    propagate, and callers for whom the state is advisory may catch it and
    proceed unlocked.
    """
    lock_path = _lock_path(target)
    token = uuid.uuid4().hex.encode("ascii")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _steal_if_stale(lock_path, stale)
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not acquire lock {lock_path} within {timeout}s")
            time.sleep(poll)
            continue
        # Won the exclusive create — we own the lock. Stamp our token so a later
        # stale-steal can never make us delete a successor's lock.
        try:
            os.write(fd, token)
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        _release(lock_path, token)

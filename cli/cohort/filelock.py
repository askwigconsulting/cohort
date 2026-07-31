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
  * **A steal or release racing the original holder** (it was merely slow, not
    dead). Both the stale-steal and the release must remove a lock-file, and a
    naive "check that it's mine/stale, then unlink it" is two syscalls with a gap:
    between the check and the unlink the file can be stolen and re-created by
    someone else, so the unlink deletes a *fresh* lock — leaving two holders. To
    close that gap every removal goes through an **atomic capture**: the lock-file
    is renamed (``os.replace``) into a process-unique staging name. Rename is
    atomic, so among racing callers exactly one captures a given file; the losers'
    renames fail and they simply retry. The winner then inspects the captured file
    — now *private*, so the check is race-free — and unlinks it only if it is
    genuinely the file it meant to remove (still stale for a steal; still carrying
    our token for a release). If it captured a fresh lock that raced into the slot
    it *reinstates* it (re-creating it only if the slot is still free) rather than
    deleting it. Each holder's token, written once at create, is what a release
    matches on.

Residual risk (documented, not eliminated): if a holder stalls for longer than
``stale`` (e.g. a process suspended by the OS), another waiter steals the lock
and both may run their critical sections concurrently for a window. The atomic
capture prevents cross-*deletion* of lock-files (no one ever removes a fresh lock
they did not verify), but not the concurrent execution itself. ``stale`` is
chosen to make this effectively impossible for the tiny JSON rewrites guarded
here; a workload that could legitimately hold the lock for tens of seconds must
raise ``stale`` accordingly.
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


def _unlink(path: Path) -> None:
    """Best-effort unlink; a vanished file counts as success."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _capture(lock_path: Path) -> Union[Path, None]:
    """Atomically move the current lock-file aside into a process-unique staging
    file and return that path, or ``None`` if there was nothing to capture.

    ``os.replace`` is atomic, so among concurrent callers racing to remove the
    same lock-file exactly one wins the capture; the losers get ``FileNotFoundError``
    (the source is already gone) and are told, via ``None``, to retry rather than
    delete anything. The returned staging file has a name no other process knows,
    so the winner can inspect it — re-check staleness, compare its token — without
    any further race before deciding to unlink it. The staging name reuses the
    lock-file's directory so the rename stays on one filesystem."""
    staging = lock_path.with_name(f"{lock_path.name}.stealing.{uuid.uuid4().hex}")
    try:
        os.replace(lock_path, staging)
    except OSError:
        return None  # nothing there, or a competitor captured it first
    return staging


def _reinstate(lock_path: Path, staging: Path) -> None:
    """Put a captured lock-file back that turned out not to be ours to remove — a
    fresh lock that raced into the slot, or a successor's lock — then drop our
    staging copy. Restore it only via an exclusive create, so a lock that has
    already reappeared in the slot is never clobbered. Best-effort; on any error
    the staging copy is dropped last so it is not leaked."""
    try:
        content = staging.read_bytes()
    except OSError:
        return  # cannot read it back → leave staging rather than risk a bad delete
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        pass  # slot re-taken (or vanished) → leave the current occupant alone
    else:
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
    _unlink(staging)


def _steal_if_stale(lock_path: Path, stale: float) -> None:
    """Reclaim a lock presumed abandoned — atomically, never deleting a fresh lock.

    A plain ``_is_stale`` then ``os.unlink`` is a check-then-act with a gap: two
    waiters both pass the check and both unlink, the second deleting a *fresh* lock
    a third party created in between (#230). Instead, once the lock *looks* stale
    we atomically capture it (rename into a private staging file): exactly one
    waiter wins, the losers retry. We then re-check staleness on the captured file
    — now ours alone, so the check is race-free — and unlink it only if it is
    still stale. If a live holder had re-created the lock in the gap we captured
    that fresh file instead and reinstate it rather than delete it. The
    ``O_EXCL`` create that follows remains the real arbiter of ownership."""
    if not _is_stale(lock_path, stale):
        return  # looks live → do not even attempt a steal (never churn a live lock)
    staging = _capture(lock_path)
    if staging is None:
        return  # someone else captured/removed it first → retry the acquire loop
    if _is_stale(staging, stale):
        _unlink(staging)  # confirmed abandoned → gone
    else:
        _reinstate(lock_path, staging)  # a fresh lock raced in → put it back


def _release(lock_path: Path, token: bytes) -> None:
    """Remove our lock-file, but only ever the file that still carries our token.

    Reading the token and then unlinking by path is a check-then-act with a gap
    (#230): between them our stale lock can be stolen and re-created by a
    successor, whose fresh lock the unlink would then delete. Instead we atomically
    capture the lock-file into a private staging file and inspect *that*: if it
    still carries our token we drop it; if a successor had re-created it we
    reinstate it untouched. Best-effort; a release failure never propagates."""
    staging = _capture(lock_path)
    if staging is None:
        return  # already gone
    try:
        current: Union[bytes, None] = staging.read_bytes()
    except OSError:
        current = None
    if current == token:
        _unlink(staging)  # still ours → remove
    else:
        _reinstate(lock_path, staging)  # not ours (stolen and re-created) → restore


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

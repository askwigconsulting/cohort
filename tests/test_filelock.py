"""Portable advisory file lock (#4): exclusivity, stale-steal, token-safe release.

The lock underpins every JSON-state read-modify-write cycle (manifest, registry,
quarantine). These tests pin the primitive's contract directly; the concurrent
data-loss regressions that motivate it live in test_state_locking.py.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from cohort.filelock import LockTimeout, file_lock


def test_lock_blocks_a_second_holder_until_release(tmp_path):
    target = tmp_path / "state.json"
    order: list[str] = []

    def worker():
        # Tiny timeout: this must NOT acquire while the main thread holds the lock.
        with pytest.raises(LockTimeout):
            with file_lock(target, timeout=0.2, poll=0.02):
                order.append("worker-inside")  # never reached

    with file_lock(target):
        order.append("main-inside")
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert order == ["main-inside"]  # the second holder never got in


def test_lock_is_reacquirable_after_release(tmp_path):
    target = tmp_path / "state.json"
    with file_lock(target, timeout=1):
        pass
    # Released cleanly, so a subsequent acquisition succeeds immediately.
    with file_lock(target, timeout=1):
        assert (tmp_path / "state.json.lock").exists()
    assert not (tmp_path / "state.json.lock").exists()  # removed on release


def test_stale_lock_is_stolen(tmp_path):
    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    lock.write_bytes(b"deadbeef")  # a lock left behind by a crashed holder
    old = time.time() - 3600
    os.utime(lock, (old, old))  # make it look abandoned
    acquired = False
    with file_lock(target, timeout=1, stale=1.0):
        acquired = True  # stale lock stolen → we get in without waiting the timeout
    assert acquired


def test_fresh_lock_is_not_stolen(tmp_path):
    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    lock.write_bytes(b"live-holder")  # a *live* holder's lock (just written)
    # A generous stale window means this fresh lock must not be stolen → timeout.
    with pytest.raises(LockTimeout):
        with file_lock(target, timeout=0.2, stale=60.0, poll=0.02):
            pass
    assert lock.read_bytes() == b"live-holder"  # untouched


def test_release_leaves_a_stolen_lock_alone(tmp_path):
    """If our lock was stolen and re-created by another holder while we ran, our
    release must not delete that successor's lock (token mismatch)."""
    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    with file_lock(target, timeout=1):
        # Simulate a steal + re-create by a different holder mid-critical-section.
        lock.write_bytes(b"successor-token")
    assert lock.exists()  # not ours anymore → left in place
    assert lock.read_bytes() == b"successor-token"


def test_steal_does_not_delete_a_fresh_lock_that_raced_in(tmp_path, monkeypatch):
    """#230 steal TOCTOU: two waiters both pass the stale check on one abandoned
    lock; between a waiter's check and its removal the stale lock is reclaimed and
    a third party wins a *fresh* ``O_EXCL`` create in the freed slot. The removal
    must not delete that fresh lock.

    The race is injected deterministically by hooking ``_is_stale``: the first time
    it confirms the lock is stale it swaps the abandoned lock for a fresh one — the
    exact interleaving the atomic capture must survive. Against the old bare-unlink
    logic the waiter deletes the fresh lock and then wrongly acquires (no
    LockTimeout); against the fix it captures the fresh lock, sees it is not stale,
    reinstates it, and times out."""
    import cohort.filelock as fl

    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    lock.write_bytes(b"stale-holder")  # a crashed holder's lock
    old = time.time() - 3600
    os.utime(lock, (old, old))  # abandoned

    real_is_stale = fl._is_stale
    state = {"injected": False}

    def is_stale_injecting(path, stale):
        result = real_is_stale(path, stale)
        if not state["injected"] and Path(path) == lock and result:
            state["injected"] = True
            # A competing waiter reclaims the stale lock and a third party wins a
            # fresh create in the gap between our stale-check and our removal.
            os.unlink(lock)
            lock.write_bytes(b"fresh-third-party")  # fresh mtime = now
        return result

    monkeypatch.setattr(fl, "_is_stale", is_stale_injecting)

    with pytest.raises(LockTimeout):
        with file_lock(target, timeout=0.3, stale=1.0, poll=0.05):
            pass  # must not acquire: a fresh lock is held

    assert lock.exists(), "steal deleted the fresh third-party lock"
    assert lock.read_bytes() == b"fresh-third-party"


def test_release_does_not_delete_a_successor_that_raced_in(tmp_path, monkeypatch):
    """#230 release TOCTOU: a successor steals + re-creates our lock in the window
    between our token-check and our unlink. The prior holder must not delete that
    fresh lock.

    The successor is injected deterministically at the moment of removal by hooking
    ``os.unlink``. Against the old read-then-unlink logic the holder reads its own
    token, then the successor re-creates the lock, then the unlink deletes the
    successor's fresh lock. Against the fix the lock-file is captured (renamed into
    a private staging file) *before* the successor acts, so the unlink only ever
    targets that private copy and the successor's re-created lock survives."""
    import cohort.filelock as fl

    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"

    real_unlink = os.unlink
    state = {"injected": False}

    def unlink_injecting_successor(path, *args, **kwargs):
        # Model a successor stealing + re-creating the lock in the window between
        # deciding to remove and actually removing.
        if not state["injected"]:
            state["injected"] = True
            lock.write_bytes(b"successor-token")  # a fresh lock from a new holder
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(fl.os, "unlink", unlink_injecting_successor)

    with file_lock(target, timeout=1):
        pass  # release runs on exit, into the injected successor race

    assert lock.exists(), "prior holder deleted the successor's fresh lock"
    assert lock.read_bytes() == b"successor-token"

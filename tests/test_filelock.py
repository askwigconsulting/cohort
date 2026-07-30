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

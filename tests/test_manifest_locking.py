"""#215 (lost-update on the main manifest) concurrency regressions.

PR #214 added ``manifest_lock`` but wired it only to the ``refresh_*`` callers.
The MAIN manifest writers (install / project-recompile / adopt) did a load →
mutate → persist WITHOUT the lock, so two concurrent ``cohort`` processes could
interleave: both read the same manifest, each appends its own op, and the last
writer's ``persist`` overwrites the first's — a placed file with no reversal
entry, which breaks the reversibility invariant.

Each test injects a delay into ``Manifest.persist`` to force the load→persist
window open. Without the lock around the whole cycle a second writer reads the
pre-write state and clobbers the first writer's op record; with it, the second
writer blocks until the first fully commits, so both op records survive — which
is what these assert. Mirrors the interleaving style of ``test_state_locking``.

Bite check (run manually during development, per the task): temporarily make
``cohort.manifest.manifest_lock`` a no-op ``yield`` and both concurrency tests
FAIL (a dropped op record); restore it and they pass.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cohort import install as install_mod
from cohort.install import do_install
from cohort.install_model import CohortPaths, Op, OpType
from cohort.manifest import Manifest, load_manifest, manifest_lock


def _run_both(fn_a, fn_b) -> list:
    """Run two callables on their own threads; collect any exception each raised."""
    errors: list = []

    def guarded(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - surface a worker crash to the test
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(f,)) for f in (fn_a, fn_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def _slow_persist(monkeypatch, delay: float = 0.25) -> None:
    """Widen the load→persist window every guarded cycle must cover atomically."""
    real = Manifest.persist

    def slow(self: Manifest, path: Path) -> None:
        time.sleep(delay)
        real(self, path)

    monkeypatch.setattr(Manifest, "persist", slow)


# --- direct primitive: load-inside-lock → mutate → persist --------------------


def test_manifest_lock_serializes_load_mutate_persist(tmp_path, monkeypatch):
    """The exact pattern every guarded writer uses: re-read under the lock, append
    a distinct op, persist. The lock must keep BOTH ops — neither writer's record
    may be lost to the other's stale-read overwrite."""
    state = tmp_path / "state"
    state.mkdir()
    mpath = state / "manifest.json"
    Manifest(install_id="i", created_at="t", mode="link", ops=[]).persist(mpath)
    _slow_persist(monkeypatch)

    def writer(name: str) -> None:
        with manifest_lock(mpath):
            manifest = load_manifest(mpath)
            assert manifest is not None
            manifest.ops.append(Op(OpType.MKDIR.value, name, str(tmp_path / name)))
            manifest.persist(mpath)

    errors = _run_both(lambda: writer("alpha"), lambda: writer("beta"))
    assert not errors
    dests = {o.dest for o in load_manifest(mpath).ops}
    assert {str(tmp_path / "alpha"), str(tmp_path / "beta")} <= dests


# --- real path: two concurrent do_install runs keep both op records -----------


def _bootstrap_install(home: Path, source: Path) -> CohortPaths:
    """One real install to create ``state/`` + the manifest, so subsequent installs
    take the racy (b) path (lock-guarded), not the fresh-init bootstrap (a) path."""
    (source / "canonical").mkdir(parents=True)
    do_install(
        home=home, selection=[], mode="link", force=False, source=source, dry_run=False
    )
    paths = CohortPaths(home)
    assert paths.manifest.exists()  # state/ now present → (b) path from here on
    return paths


def test_concurrent_do_install_keeps_both_op_records(tmp_path, monkeypatch):
    """Two ``cohort install`` processes adding different artifacts must not lose
    either's recorded op. Each writer contributes one distinct MKDIR op (via a
    stubbed ``adapter_ops``); without the lock one op record is dropped, leaving a
    created directory with no reversal entry."""
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source"
    paths = _bootstrap_install(home, source)

    def fake_adapter_ops(ides, paths_, source_, mode):
        # One unique, not-yet-existing dest per selected IDE → each classifies APPLY
        # and appends exactly one recorded op the concurrent writer must not clobber.
        return [
            Op(OpType.MKDIR.value, ide, str(paths_.cohort_home / f"extra-{ide}"))
            for ide in ides
        ]

    monkeypatch.setattr(install_mod, "adapter_ops", fake_adapter_ops)
    _slow_persist(monkeypatch)

    errors = _run_both(
        lambda: do_install(
            home=home, selection=["claude"], mode="link", force=False,
            source=source, dry_run=False,
        ),
        lambda: do_install(
            home=home, selection=["cursor"], mode="link", force=False,
            source=source, dry_run=False,
        ),
    )
    assert not errors
    manifest = load_manifest(paths.manifest)
    assert manifest is not None
    mkdir_dests = {o.dest for o in manifest.ops if o.op == OpType.MKDIR.value}
    # Both writers' recorded ops survive: last-writer-wins (no lock) would keep only
    # one of these, orphaning the other created directory from any reverse.
    assert str(paths.cohort_home / "extra-claude") in mkdir_dests
    assert str(paths.cohort_home / "extra-cursor") in mkdir_dests

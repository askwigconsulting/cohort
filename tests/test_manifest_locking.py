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

import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cohort import install as install_mod
from cohort import office_setup as office_mod
from cohort import project as project_mod
from cohort import roster as roster_mod
from cohort import specialists as specialists_mod
from cohort.install import do_install
from cohort.install_model import CohortPaths, Op, OpType
from cohort.manifest import Manifest, load_manifest, manifest_lock
from cohort.office_setup import persist_roster
from cohort.project import do_init
from cohort.specialists import do_add_specialist, do_remove_specialist

COHORT_SRC = Path(__file__).resolve().parents[1]


def _record_lock(monkeypatch, module: object, attr: str = "manifest_lock") -> list[Path]:
    """Replace ``module.<attr>`` (its bound ``manifest_lock``) with a recorder that
    logs each entry and still serializes via the real lock. Returns the entry log
    so a caller can assert the guarded site actually acquired the lock — the
    lock-acquisition proof for the #4 sites too heavy to drive concurrently.

    Bite check: delete the ``with manifest_lock(...)`` from the site under test and
    the log stays empty, so the asserting test FAILS."""
    entered: list[Path] = []

    @contextmanager
    def recorder(path: Path) -> Iterator[None]:
        entered.append(path)
        with manifest_lock(path):
            yield

    monkeypatch.setattr(module, attr, recorder)
    return entered


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


# --- #4 site 1: office_setup.persist_roster — real concurrency, bites ---------


def test_persist_roster_serializes_against_a_concurrent_op_writer(tmp_path, monkeypatch):
    """``persist_roster`` (office_setup) must re-read under the lock so a concurrent
    manifest writer's op record survives its roster write, and vice-versa. Without
    the lock the roster write's stale-read overwrite drops the concurrent op — a
    placed dir with no reversal entry. Mirrors the interleaving of the primitive
    test above."""
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source"
    paths = _bootstrap_install(home, source)
    _slow_persist(monkeypatch)

    def set_roster() -> None:
        persist_roster(home, ["chief-of-staff"])

    def append_op() -> None:
        with manifest_lock(paths.manifest):
            m = load_manifest(paths.manifest)
            assert m is not None
            m.ops.append(Op(OpType.MKDIR.value, "x", str(tmp_path / "concurrent-dir")))
            m.persist(paths.manifest)

    errors = _run_both(set_roster, append_op)
    assert not errors
    final = load_manifest(paths.manifest)
    assert final is not None
    assert final.roster == ["chief-of-staff"]  # the roster write survived
    assert str(tmp_path / "concurrent-dir") in {o.dest for o in final.ops}  # the op survived


# --- #4 site 4: project.do_init — the conditional (bootstrap-(a)) guard --------


def test_do_init_fresh_repo_survives_absent_state_dir(tmp_path):
    """A first ``cohort init`` creates ``state/`` mid-apply, so ``<manifest>.lock``
    has no parent dir yet. The conditional guard must fall back to ``nullcontext``
    — an unconditional lock would raise ``FileNotFoundError`` from ``file_lock``'s
    ``os.open`` and abort the install."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    result = do_init(repo, source=COHORT_SRC, dry_run=False, home=home)
    assert result["action"] == "init"
    assert CohortPaths.for_project(repo).manifest.exists()


def test_do_init_locks_only_once_state_exists(tmp_path, monkeypatch):
    """One recorder proves BOTH branches of the conditional guard: the fresh init
    (``state/`` absent at entry) must NOT lock — ``nullcontext`` — while the re-init
    (``state/`` now present, the racy (b) path) MUST take the real lock."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    entered = _record_lock(monkeypatch, project_mod)
    do_init(repo, source=COHORT_SRC, dry_run=False, home=home)
    assert entered == []  # fresh init: state/ absent → nullcontext, no lock taken
    do_init(repo, source=COHORT_SRC, dry_run=False, home=home)
    assert len(entered) == 1  # re-init: state/ present → lock acquired


# --- #4 site 3: specialists.do_remove_specialist — lock-acquisition ------------


def test_remove_specialist_persists_removal_under_the_lock(tmp_path, monkeypatch):
    """The op-removal RMW in ``do_remove_specialist`` must run under the manifest
    lock (racy (b): the project ``state/`` already exists). The recorder on the
    ``specialists`` module records only that site — ``refresh_project_context``
    below re-acquires via its own module binding, and ``do_add_specialist``'s
    install ran before the recorder was installed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    do_init(repo, source=COHORT_SRC, dry_run=False, home=home)
    do_add_specialist(repo, home, "devrel", "DevRel", "Marketing", "desc", dry_run=False)
    entered = _record_lock(monkeypatch, specialists_mod)
    result = do_remove_specialist(repo, home, "devrel", dry_run=False)
    assert result["action"] == "remove-specialist"
    assert len(entered) == 1  # the op-removal read-modify-persist ran under the lock


# --- #4 site 2: roster.do_add_agent office roster-extend — lock-acquisition ----


def test_add_office_agent_roster_extend_acquires_the_lock(tmp_path, monkeypatch):
    """The ``to == "office"`` roster-extend after ``do_install`` (racy (b): the
    install just created ``state/`` and released its own lock) must take the lock.
    A pinned roster subset that excludes the new agent triggers the extend branch.
    The recorder on the ``roster`` module isolates that site from ``do_install``'s
    own lock (a different module binding)."""
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", source / "canonical")
    do_install(
        home=home, selection=["claude"], mode="link", force=False, source=source, dry_run=False
    )
    persist_roster(home, ["chief-of-staff"])  # pin a subset so the extend branch fires
    entered = _record_lock(monkeypatch, roster_mod)
    roster_mod.do_add_agent(
        source, home, "devrel", "DevRel", "Marketing", "specialist", "desc",
        dry_run=False, to="office",
    )
    assert len(entered) == 1  # the roster-extend read-modify-persist ran under the lock

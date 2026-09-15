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

import ast
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cohort import adopt as adopt_mod
from cohort import cli as cli_mod
from cohort import compile as compile_mod
from cohort import dashboard as dashboard_mod
from cohort import install as install_mod
from cohort import office_setup as office_mod
from cohort import project as project_mod
from cohort import roster as roster_mod
from cohort import specialists as specialists_mod
from cohort import update as update_mod
from cohort.adapters.claude import StagedFile
from cohort.compile import CompileResult, write_staging
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


# --- #291: write_staging swaps the IDE tree in UNDER the manifest lock ---------
#
# ``do_install`` reads ``compiled/<ide>/`` under the manifest lock, but every
# caller compiled + wrote staging BEFORE taking it, so a concurrent recompile
# could rmtree the tree an install was reading from. ``write_staging`` now builds
# into a temp sibling with no lock held and swaps it in under a fresh, non-nested
# acquisition of the manifest lock — so a lock holder never sees a half-built or
# vanished tree, and a writer blocks until the holder is done.

def _result(ide: str, files: dict[str, str]) -> CompileResult:
    return CompileResult(
        ide=ide,
        staged=[StagedFile(rel, body.encode("utf-8")) for rel, body in files.items()],
    )


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_write_staging_blocks_while_another_process_holds_the_manifest_lock(tmp_path):
    """A holder of the manifest lock (a ``do_install`` mid-read) must keep seeing the
    tree it started with: the writer's swap waits for the lock, and the old tree
    stays intact and complete until then. After release the new tree is live."""
    home = tmp_path / "home"
    home.mkdir()
    paths = _bootstrap_install(home, tmp_path / "source")
    write_staging(paths, _result("claude", {"agents/a.md": "old-a\n", "agents/b.md": "old-b\n"}))
    live = paths.compiled_ide("claude")
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def holder() -> None:
        with manifest_lock(paths.manifest):
            entered.set()
            release.wait(10)

    def writer() -> None:
        write_staging(paths, _result("claude", {"agents/a.md": "new-a\n"}))
        done.set()

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert entered.wait(5)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert not done.wait(0.5)  # blocked: the holder still owns the lock
    assert _tree(live) == {"agents/a.md": "old-a\n", "agents/b.md": "old-b\n"}
    release.set()
    holder_thread.join(5)
    writer_thread.join(5)
    assert done.is_set()  # a LockTimeout here would mean a nested acquisition
    assert _tree(live) == {"agents/a.md": "new-a\n"}


def test_write_staging_leaves_no_temp_siblings_and_sweeps_an_orphaned_old_tree(tmp_path):
    """The swap is rename-old → rename-new → rmtree-old. Afterwards ``compiled/``
    holds exactly the live tree: no ``<ide>.new-*`` build dir, no ``<ide>.old-*``
    leftover — including one orphaned by a crash between the two renames."""
    home = tmp_path / "home"
    home.mkdir()
    paths = _bootstrap_install(home, tmp_path / "source")
    write_staging(paths, _result("claude", {"agents/a.md": "v1\n", "agents/gone.md": "x\n"}))
    orphan = paths.compiled / "claude.old-deadbeef"
    (orphan / "agents").mkdir(parents=True)
    (orphan / "agents" / "stale.md").write_text("crashed\n", encoding="utf-8")
    write_staging(paths, _result("claude", {"agents/a.md": "v2\n"}))
    assert sorted(p.name for p in paths.compiled.iterdir()) == ["claude"]
    assert _tree(paths.compiled_ide("claude")) == {"agents/a.md": "v2\n"}  # wholesale rebuild


def test_write_staging_failed_swap_keeps_the_old_tree_live_and_leaks_nothing(tmp_path, monkeypatch):
    """If the swap cannot complete (Windows refusing a rename over an open handle,
    say) the previously live tree is untouched and the build sibling is removed —
    a caller sees the error, never a half-swapped ``compiled/``."""
    home = tmp_path / "home"
    home.mkdir()
    paths = _bootstrap_install(home, tmp_path / "source")
    write_staging(paths, _result("claude", {"agents/a.md": "live\n"}))

    def refuse(staging_root: Path, build_root: Path) -> None:
        raise PermissionError("simulated: handle open in the old tree")

    monkeypatch.setattr(compile_mod, "_swap_staging", refuse)
    try:
        write_staging(paths, _result("claude", {"agents/a.md": "never\n"}))
    except PermissionError:
        pass
    else:
        raise AssertionError("the swap failure must propagate")
    assert sorted(p.name for p in paths.compiled.iterdir()) == ["claude"]
    assert _tree(paths.compiled_ide("claude")) == {"agents/a.md": "live\n"}


def test_write_staging_bootstraps_without_a_state_dir(tmp_path):
    """A first ``cohort init`` writes staging before ``state/`` exists, so the lock
    file has no parent yet: the acquisition is conditional (bootstrap-(a), like
    ``do_install``/``do_init``) and must neither raise nor create ``state/``."""
    paths = CohortPaths(tmp_path / "home")
    assert not paths.manifest.parent.exists()
    write_staging(paths, _result("claude", {"agents/a.md": "a\n"}))
    assert _tree(paths.compiled_ide("claude")) == {"agents/a.md": "a\n"}
    assert not paths.manifest.parent.exists()


def test_write_staging_still_refuses_a_symlinked_staging_root(tmp_path):
    """The repo-escape guard (``_assert_staging_contained``) survives the rewrite:
    a symlinked ``compiled/`` is refused before anything is built or swapped."""
    if os.name == "nt":  # pragma: no cover - symlink creation needs privileges there
        return
    paths = CohortPaths(tmp_path / "home")
    paths.cohort_home.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    paths.compiled.symlink_to(elsewhere)
    try:
        write_staging(paths, _result("claude", {"agents/a.md": "a\n"}))
    except compile_mod.CompileError:
        pass
    else:
        raise AssertionError("a symlinked compiled/ must be refused")
    assert list(elsewhere.iterdir()) == []


def test_concurrent_recompile_never_disturbs_an_install_reading_staging(tmp_path, monkeypatch):
    """The r5 probe, as a regression: a racer rewrites staging in a loop while a
    copy-mode ``do_install`` reads it under the lock. The install must complete
    and place the bytes of ONE consistent tree (never a torn or vanished one)."""
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source"
    paths = _bootstrap_install(home, source)
    result = _result("claude", {"agents/a.md": "a\n", "agents/b.md": "b\n"})
    write_staging(paths, result)
    _slow_persist(monkeypatch, delay=0.05)  # keep the lock held long enough to be raced
    stop = threading.Event()
    racer_errors: list[Exception] = []

    def racer() -> None:
        while not stop.is_set():
            try:
                write_staging(paths, result)
            except Exception as exc:  # noqa: BLE001 - surface to the test
                racer_errors.append(exc)
                return

    thread = threading.Thread(target=racer)
    thread.start()
    try:
        report = do_install(
            home=home, selection=["claude"], mode="copy", force=False, source=source,
            dry_run=False,
        )
    finally:
        stop.set()
        thread.join(10)
    assert not racer_errors
    assert report.summary["applied"] >= 2
    placed = home / ".claude" / "agents"
    assert (placed / "a.md").read_text(encoding="utf-8") == "a\n"
    assert (placed / "b.md").read_text(encoding="utf-8") == "b\n"


def _with_bodies_under_lock(tree: ast.AST) -> list[ast.With]:
    """Every ``with`` whose context manager is a ``manifest_lock``/``file_lock`` call."""
    found: list[ast.With] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                fn = call.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in {"manifest_lock", "file_lock"}:
                    found.append(node)
    return found


def test_no_caller_invokes_write_staging_inside_the_manifest_lock():
    """The lock is not reentrant, so ``write_staging`` (which now takes it) must
    never be called from inside a ``with manifest_lock(...)``/``file_lock(...)``
    body. Static proof over every module that calls it: a nested call would be a
    deadlock-then-``LockTimeout`` at runtime, not flakiness."""
    modules = [
        adopt_mod, cli_mod, dashboard_mod, install_mod, office_mod, roster_mod,
        specialists_mod, update_mod, compile_mod,
    ]
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for with_node in _with_bodies_under_lock(tree):
            for node in ast.walk(with_node):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    assert name != "write_staging", (
                        f"{module.__name__}:{node.lineno} calls write_staging under the lock"
                    )


# --- #292: the office roster is EXTENDED under the lock, never overwritten -----


def test_concurrent_add_agent_keeps_both_roster_entries(tmp_path, monkeypatch):
    """Two ``add-agent --to office`` runs that both read the roster before either
    wrote it. The second runs to completion inside the first's read→persist window
    (injected at the first's compile step). With a blind ``fresh.roster = subset``
    the first's persist would drop the second's entry; extending ``fresh.roster``
    under the lock keeps both."""
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", source / "canonical")
    do_install(
        home=home, selection=["claude"], mode="link", force=False, source=source, dry_run=False
    )
    persist_roster(home, ["chief-of-staff"])
    paths = CohortPaths(home)
    real_compile = roster_mod.compile_ide
    interleaved: list[str] = []

    def compile_after_a_concurrent_add(*args, **kwargs):
        if not interleaved:  # only the OUTER add-agent's compile step is intercepted
            interleaved.append("second")
            roster_mod.do_add_agent(
                source, home, "second", "Second", "Ops", "specialist", "desc",
                dry_run=False, to="office",
            )
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(roster_mod, "compile_ide", compile_after_a_concurrent_add)
    roster_mod.do_add_agent(
        source, home, "first", "First", "Ops", "specialist", "desc", dry_run=False, to="office",
    )
    final = load_manifest(paths.manifest)
    assert final is not None
    assert set(final.roster) == {"chief-of-staff", "first", "second"}
    assert final.roster.count("first") == 1  # extended once, no duplicate


# --- #291 (project variant): context staging is unique and made under the lock --


def test_refresh_project_context_stages_a_unique_file_under_the_lock(tmp_path, monkeypatch):
    """``refresh_project_context`` used a FIXED staging path written outside the lock
    and read inside it, so two refreshes could hand one another's bytes to ``apply``.
    It now stages to a unique name while holding the lock and removes it after."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    do_init(repo, source=COHORT_SRC, dry_run=False, home=home)
    paths = CohortPaths.for_project(repo)
    held: list[bool] = [False]
    staged_names: list[str] = []

    @contextmanager
    def recording_lock(path: Path) -> Iterator[None]:
        with manifest_lock(path):
            held[0] = True
            try:
                yield
            finally:
                held[0] = False

    real_stage = project_mod._stage

    def recording_stage(stage_dir: Path, name: str, content: str) -> str:
        assert held[0], "context staging must happen while the manifest lock is held"
        staged_names.append(name)
        return real_stage(stage_dir, name, content)

    monkeypatch.setattr(project_mod, "manifest_lock", recording_lock)
    monkeypatch.setattr(project_mod, "_stage", recording_stage)
    project_mod.refresh_project_context(paths)
    project_mod.refresh_project_context(paths)
    assert len(staged_names) == 2
    assert len(set(staged_names)) == 2  # unique per call, never the fixed name
    assert "context-block.txt" not in staged_names
    leftovers = [p.name for p in (paths.compiled / "project").glob("context-block*")]
    assert leftovers == ["context-block.txt"]  # only init's own file remains

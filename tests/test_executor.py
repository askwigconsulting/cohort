"""P1-T1: InstallPlan model & executor — preflight, apply, reverse.

Behavioral (REVIEW GATE) + integration + unit tests, run against a temp ``$HOME``.
Per decision G, the executor returns structured results; process exit codes are
asserted in the CLI tests, not here.
"""

from __future__ import annotations

import pytest

from cohort.executor import (
    ClobberRefused,
    apply,
    path_hash,
    preflight,
    reverse_full,
    reverse_slice,
)
from cohort.install_model import GLOBAL_IDE, CohortPaths, Op, OpStatus, OpType
from cohort.manifest import Manifest, load_manifest


def make_manifest(mode: str = "link") -> Manifest:
    return Manifest(install_id="testid000001", created_at="2026-01-01T00:00:00+00:00", mode=mode)


@pytest.fixture
def src(tmp_path):
    """A source tree with a file and a subdir to link/copy from."""
    d = tmp_path / "src"
    (d / "sub").mkdir(parents=True)
    (d / "file.txt").write_text("hello\n", encoding="utf-8")
    (d / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
    return d


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def paths_for(home):
    return CohortPaths(home=home)


# --- behavioral: apply on clean --------------------------------------------


def test_clean_apply_creates_dirs_and_links_in_order(home, src):
    paths = paths_for(home)
    plan = [
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(home / "a")),
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(home / "a" / "b")),
        Op(OpType.LINK.value, GLOBAL_IDE, str(home / "a" / "b" / "link"), src=str(src / "file.txt")),
    ]
    m = make_manifest()
    outcomes = apply(plan, paths, m, force=False)
    assert [o.status for o in outcomes] == ["applied", "applied", "applied"]
    assert (home / "a").is_dir()
    assert (home / "a" / "b" / "link").is_symlink()
    # manifest records order, ide tags, created flags
    assert [o.op for o in m.ops] == ["mkdir", "mkdir", "link"]
    assert all(o.ide == GLOBAL_IDE for o in m.ops)
    assert m.ops[0].created is True and m.ops[1].created is True


def test_reapply_is_all_skipped(home, src):
    paths = paths_for(home)
    plan = [
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(home / "a")),
        Op(OpType.LINK.value, GLOBAL_IDE, str(home / "a" / "link"), src=str(src / "file.txt")),
    ]
    apply(plan, paths, make_manifest(), force=False)
    m2 = make_manifest()
    outcomes = apply(plan, paths, m2, force=False)
    assert [o.status for o in outcomes] == ["skipped", "skipped"]
    assert m2.ops == []  # nothing re-recorded


def test_correct_symlink_is_noop_not_backed_up(home, src):
    paths = paths_for(home)
    dest = home / "link"
    import os

    os.symlink(str(src / "file.txt"), dest)
    op = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    pf = preflight([op], None, force=False)
    assert pf.classified[0].status == OpStatus.SATISFIED
    assert pf.clobbers == []


# --- behavioral: clobber / force / backup ----------------------------------


def test_foreign_file_refused_without_force(home, src):
    dest = home / "link"
    dest.write_text("MINE\n", encoding="utf-8")
    op = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    pf = preflight([op], None, force=False)
    assert len(pf.clobbers) == 1
    # apply without force raises and leaves the foreign file intact
    with pytest.raises(ClobberRefused):
        apply([op], paths_for(home), make_manifest(), force=False)
    assert dest.read_text(encoding="utf-8") == "MINE\n"


def test_force_backs_up_then_links_and_maps_backup(home, src):
    paths = paths_for(home)
    paths.state.mkdir(parents=True)  # so backups/ + manifest can be written
    dest = home / "link"
    dest.write_text("MINE\n", encoding="utf-8")
    op = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    m = make_manifest()
    outcomes = apply([op], paths, m, force=True)
    assert [o.status for o in outcomes] == ["backup", "applied"]
    assert dest.is_symlink()
    backup_op = next(o for o in m.ops if o.op == OpType.BACKUP.value)
    from pathlib import Path

    assert Path(backup_op.backup).read_text(encoding="utf-8") == "MINE\n"


# --- behavioral: atomic refusal (foreign not first) ------------------------


def test_foreign_not_first_refused_with_zero_writes(home, src):
    paths = paths_for(home)
    # op 0 clean, op 1 foreign
    clean_dest = home / "ok"
    foreign_dest = home / "foreign"
    foreign_dest.write_text("MINE\n", encoding="utf-8")
    plan = [
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(clean_dest)),
        Op(OpType.LINK.value, GLOBAL_IDE, str(foreign_dest), src=str(src / "file.txt")),
    ]
    before = path_hash(home)
    pf = preflight(plan, None, force=False)
    assert len(pf.clobbers) == 1
    # orchestration would refuse before apply; nothing on disk changed
    assert path_hash(home) == before
    assert not clean_dest.exists()


# --- behavioral: TOCTOU re-check -------------------------------------------


def test_apply_refuses_dest_that_turned_foreign_after_preflight(home, src):
    paths = paths_for(home)
    dest = home / "link"
    op = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    pf = preflight([op], None, force=False)
    assert pf.clobbers == []  # clean at preflight
    dest.write_text("APPEARED\n", encoding="utf-8")  # race: foreign appears
    with pytest.raises(ClobberRefused):
        apply([op], paths, make_manifest(), force=False)
    assert dest.read_text(encoding="utf-8") == "APPEARED\n"


# --- behavioral: copy idempotency ------------------------------------------


def test_copy_byte_identical_is_skipped(home, src):
    paths = paths_for(home)
    dest = home / "copy.txt"
    op = Op(OpType.COPY.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    apply([op], paths, make_manifest(), force=False)
    m2 = make_manifest()
    outcomes = apply([op], paths, m2, force=False)
    assert outcomes[0].status == "skipped"


def test_copy_divergent_ours_overwrites_without_backup(home, src):
    paths = paths_for(home)
    dest = home / "copy.txt"
    op = Op(OpType.COPY.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    m = make_manifest()
    apply([op], paths, m, force=False)
    # source changes; dest still matches the recorded hash → ours, stale
    (src / "file.txt").write_text("CHANGED\n", encoding="utf-8")
    outcomes = apply([op], paths, m, force=False)
    assert [o.status for o in outcomes] == ["applied"]
    assert dest.read_text(encoding="utf-8") == "CHANGED\n"
    assert all(o.status != "backup" for o in outcomes)  # overwrite-ours: no backup


def test_copy_divergent_foreign_is_clobber(home, src):
    paths = paths_for(home)
    dest = home / "copy.txt"
    dest.write_text("FOREIGN\n", encoding="utf-8")
    op = Op(OpType.COPY.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    pf = preflight([op], None, force=False)
    assert len(pf.clobbers) == 1


# --- behavioral: mkdir onto a non-dir file ---------------------------------


def test_mkdir_onto_file_is_clobber(home):
    dest = home / "x"
    dest.write_text("not a dir\n", encoding="utf-8")
    op = Op(OpType.MKDIR.value, GLOBAL_IDE, str(dest))
    pf = preflight([op], None, force=False)
    assert pf.classified[0].status == OpStatus.CLOBBER


# --- behavioral: reverse LIFO + rmdir-if-empty -----------------------------


def test_reverse_lifo_removes_link_before_created_dir(home, src):
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    plan = [
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(home / "d")),
        Op(OpType.LINK.value, GLOBAL_IDE, str(home / "d" / "lnk"), src=str(src / "file.txt")),
    ]
    m = make_manifest()
    apply(plan, paths, m, force=False)
    result = reverse_full(m, paths)
    assert not (home / "d").exists()  # link removed, then empty dir rmdir'd
    assert result.removed == 1
    assert result.dirs_removed >= 1


def test_reverse_keeps_created_dir_left_nonempty_by_user(home, src):
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    plan = [Op(OpType.MKDIR.value, GLOBAL_IDE, str(home / "d"))]
    m = make_manifest()
    apply(plan, paths, m, force=False)
    (home / "d" / "user.txt").write_text("user\n", encoding="utf-8")  # user drops a file in
    reverse_full(m, paths)
    assert (home / "d").exists()  # not removed — non-empty
    assert (home / "d" / "user.txt").exists()


def test_reverse_skips_diverged_symlink(home, src):
    """Ownership check (B): a dest replaced by the user is not deleted."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "lnk"
    op = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    m = make_manifest()
    apply([op], paths, m, force=False)
    dest.unlink()
    dest.write_text("USER REPLACED\n", encoding="utf-8")  # diverged
    result = reverse_full(m, paths)
    assert dest.read_text(encoding="utf-8") == "USER REPLACED\n"  # preserved
    assert result.removed == 0
    assert result.skipped >= 1


# --- behavioral: ide-filtered reverse --------------------------------------


def test_slice_reverse_touches_only_that_ide(home, src):
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    plan = [
        Op(OpType.LINK.value, GLOBAL_IDE, str(home / "g"), src=str(src / "file.txt")),
        Op(OpType.LINK.value, "claude", str(home / "c"), src=str(src / "file.txt")),
        Op(OpType.LINK.value, "cursor", str(home / "u"), src=str(src / "file.txt")),
    ]
    m = make_manifest()
    m.ides = ["claude", "cursor"]
    apply(plan, paths, m, force=False)
    reverse_slice(m, paths, "claude")
    assert not (home / "c").exists()  # claude removed
    assert (home / "g").exists()  # global untouched
    assert (home / "u").exists()  # cursor untouched
    assert m.ides == ["cursor"]
    assert all(o.ide != "claude" for o in m.ops)


# --- integration: full round-trip ------------------------------------------


def test_full_round_trip_is_byte_identical(home, src):
    paths = paths_for(home)
    before = path_hash(home)
    plan = [
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(paths.cohort_home)),
        Op(OpType.MKDIR.value, GLOBAL_IDE, str(paths.state)),
        Op(OpType.LINK.value, GLOBAL_IDE, str(paths.canonical), src=str(src)),
    ]
    m = make_manifest()
    apply(plan, paths, m, force=False)
    m.persist(paths.manifest)
    assert paths.manifest.exists()
    reverse_full(m, paths)
    assert not paths.manifest.exists()
    assert not paths.cohort_home.exists()
    assert path_hash(home) == before


# --- unit -------------------------------------------------------------------


def test_manifest_round_trips_with_ide_and_created(tmp_path):
    m = Manifest(install_id="id", created_at="t", mode="link", ides=["claude"])
    m.ops.append(Op(OpType.MKDIR.value, GLOBAL_IDE, "/x", created=True))
    m.ops.append(Op(OpType.LINK.value, "claude", "/y", src="/s"))
    path = tmp_path / "manifest.json"
    m.persist(path)
    loaded = load_manifest(path)
    assert loaded.ides == ["claude"]
    assert loaded.ops[0].created is True
    assert loaded.ops[1].ide == "claude"


def test_tree_hash_does_not_follow_symlinks(home, src):
    import os

    link = home / "lnk"
    os.symlink(str(src / "file.txt"), link)
    # changing the link *target's* content must not change the link's hash
    h1 = path_hash(link)
    (src / "file.txt").write_text("totally different\n", encoding="utf-8")
    assert path_hash(link) == h1


def test_symlink_target_comparison(home, src):
    import os

    dest = home / "lnk"
    os.symlink(str(src / "file.txt"), dest)
    same = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    other = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "sub"))
    assert preflight([same], None, False).classified[0].status == OpStatus.SATISFIED
    assert preflight([other], None, False).classified[0].status == OpStatus.CLOBBER

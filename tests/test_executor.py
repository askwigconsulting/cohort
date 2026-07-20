"""P1-T1: InstallPlan model & executor — preflight, apply, reverse.

Behavioral (REVIEW GATE) + integration + unit tests, run against a temp ``$HOME``.
Per decision G, the executor returns structured results; process exit codes are
asserted in the CLI tests, not here.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from cohort.executor import (
    ClobberRefused,
    InvalidJSONError,
    apply,
    path_hash,
    preflight,
    reverse_full,
    reverse_slice,
)
from cohort.install_model import GLOBAL_IDE, CohortPaths, Op, OpStatus, OpType
from cohort.manifest import Manifest, load_manifest
from conftest import requires_symlinks


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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
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


@requires_symlinks
def test_tree_hash_does_not_follow_symlinks(home, src):
    import os

    link = home / "lnk"
    os.symlink(str(src / "file.txt"), link)
    # changing the link *target's* content must not change the link's hash
    h1 = path_hash(link)
    (src / "file.txt").write_text("totally different\n", encoding="utf-8")
    assert path_hash(link) == h1


@requires_symlinks
def test_symlink_target_comparison(home, src):
    import os

    dest = home / "lnk"
    os.symlink(str(src / "file.txt"), dest)
    same = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))
    other = Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "sub"))
    assert preflight([same], None, False).classified[0].status == OpStatus.SATISFIED
    assert preflight([other], None, False).classified[0].status == OpStatus.CLOBBER


# --- moved/renamed source: Cohort-owned links self-heal (issue #34) ---------


@requires_symlinks
def test_moved_source_link_self_heals_without_force(home, src, tmp_path):
    """A Cohort-owned link whose source moved (now dangling) re-points on the next
    install from the new path — no --force, no clobber."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "canonical"
    m = make_manifest()
    apply([Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))], paths, m, force=False)
    assert dest.is_symlink() and dest.resolve() == (src / "file.txt")

    new_src = tmp_path / "src2"
    new_src.mkdir()
    (new_src / "file.txt").write_text("hello\n", encoding="utf-8")
    shutil.rmtree(src)  # the original clone moved/was deleted → link now dangles
    assert dest.is_symlink() and not dest.exists()

    plan = [Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(new_src / "file.txt"))]
    assert preflight(plan, m, force=False).clobbers == []  # re-point, not a clobber
    apply(plan, paths, m, force=False)
    assert dest.is_symlink() and dest.exists() and dest.resolve() == (new_src / "file.txt")


@requires_symlinks
def test_user_repointed_link_is_still_a_clobber(home, src, tmp_path):
    """A link the *user* re-pointed to their own live target is foreign — still a
    clobber (backup/--force), never silently overwritten."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "canonical"
    m = make_manifest()
    apply([Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))], paths, m, force=False)

    other = tmp_path / "mine.txt"
    other.write_text("mine\n", encoding="utf-8")
    dest.unlink()
    os.symlink(other, dest)  # user re-points it to a live, non-Cohort target

    plan = [Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))]
    assert len(preflight(plan, m, force=False).clobbers) == 1


@requires_symlinks
def test_reverse_removes_dangling_owned_link(home, src):
    """Uninstall removes a Cohort-owned link even after its source vanished — a
    dangling link we recorded is ours to clean up, never leaked."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "canonical"
    m = make_manifest()
    apply([Op(OpType.LINK.value, GLOBAL_IDE, str(dest), src=str(src / "file.txt"))], paths, m, force=False)
    shutil.rmtree(src)  # link dangles
    assert dest.is_symlink() and not dest.exists()

    result = reverse_full(m, paths)
    assert not dest.is_symlink() and result.removed == 1  # removed, not skipped/leaked


# --- O1: an interrupted copy must never masquerade as a foreign clobber -----


def test_interrupted_copy_never_leaves_a_clobber(home, src, monkeypatch):
    """A crash mid-``shutil.copytree`` (Ctrl-C, disk-full) must never leave a
    partial ``dest`` — previously that partial dest had no manifest op, so the
    next preflight's ``classify`` saw it as foreign and refused the whole
    install with CLOBBER, even though nothing was ever actually clobbered."""
    paths = paths_for(home)
    dest = home / "copy_dir"
    op = Op(OpType.COPY.value, GLOBAL_IDE, str(dest), src=str(src))

    def boom(*_args, **_kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(shutil, "copytree", boom)
    m = make_manifest()
    with pytest.raises(OSError):
        apply([op], paths, m, force=False)

    assert not dest.exists()  # never partially materialized at the real path
    # no stray temp sibling left behind either
    assert list(home.iterdir()) == []
    assert m.ops == []  # nothing recorded — apply never got that far

    # next run: classify must resolve this as a normal fresh install, not CLOBBER
    pf = preflight([op], m, force=False)
    assert pf.clobbers == []
    assert pf.classified[0].status == OpStatus.APPLY


def test_interrupted_copy_over_existing_dest_leaves_old_content_intact(home, src, monkeypatch):
    """If a prior good copy already exists at ``dest`` and a re-copy (e.g. after
    the source changed) is interrupted while building the new tree, the old
    ``dest`` must be left exactly as it was — never partially replaced."""
    paths = paths_for(home)
    dest = home / "copy_dir"
    op = Op(OpType.COPY.value, GLOBAL_IDE, str(dest), src=str(src))
    m = make_manifest()
    apply([op], paths, m, force=False)
    before = path_hash(dest)

    def boom(*_args, **_kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(shutil, "copytree", boom)
    (src / "new_file.txt").write_text("new\n", encoding="utf-8")  # force a re-copy
    with pytest.raises(OSError):
        apply([op], paths, m, force=False)

    assert path_hash(dest) == before  # untouched by the failed re-copy
    assert not any(p.name.startswith(".") for p in home.iterdir())  # no stray temp


# --- O4: an unparseable existing JSON file must refuse cleanly, not crash ---


def test_merge_preflight_refuses_cleanly_on_invalid_existing_json(home, src):
    """A JSONC comment / trailing comma in the user's existing settings file
    must raise a clean, file-naming refusal, not an uncaught JSONDecodeError."""
    dest = home / "settings.json"
    dest.write_text('{\n  "a": 1,\n}\n', encoding="utf-8")  # trailing comma
    fragment_src = src / "fragment.json"
    fragment_src.write_text(json.dumps({"hooks": {"PostToolUse": [{"command": "x"}]}}), encoding="utf-8")
    op = Op(OpType.MERGE.value, GLOBAL_IDE, str(dest), src=str(fragment_src), strategy="json")

    with pytest.raises(InvalidJSONError) as exc_info:
        preflight([op], None, force=False)
    assert str(dest) in str(exc_info.value)
    with pytest.raises(json.JSONDecodeError):
        raise exc_info.value.__cause__  # the original decode error is chained, not swallowed


def test_merge_reverse_refuses_cleanly_on_invalid_existing_json(home, src):
    """Same refusal on the reverse path (~439): a settings file corrupted after
    install must not crash uninstall with a raw JSONDecodeError."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "settings.json"
    fragment_src = src / "fragment.json"
    fragment_src.write_text(json.dumps({"hooks": {"PostToolUse": [{"command": "x"}]}}), encoding="utf-8")
    op = Op(OpType.MERGE.value, GLOBAL_IDE, str(dest), src=str(fragment_src), strategy="json")
    m = make_manifest()
    apply([op], paths, m, force=False)

    dest.write_text('{\n  "hooks": {},\n}\n', encoding="utf-8")  # user corrupts it (trailing comma)
    with pytest.raises(InvalidJSONError) as exc_info:
        reverse_full(m, paths)
    assert str(dest) in str(exc_info.value)


# --- O2-backup: a merge reverse must snapshot the file before mutating it ---


@requires_symlinks
def test_block_merge_reverse_backs_up_before_removal(home, src):
    """A block-merge reverse rewrites/deletes the file it merged into — it must
    snapshot the pre-write content to a sibling ``.bak`` first (O2-backup),
    otherwise a wrong block removal is unrecoverable. Previously only the
    CLOBBER path got a backup."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "CLAUDE.md"
    dest.write_text("# my notes\n", encoding="utf-8")  # user content the block gets merged into
    block_src = src / "block.md"
    block_src.write_text("cohort managed content\n", encoding="utf-8")
    op = Op(OpType.MERGE.value, GLOBAL_IDE, str(dest), src=str(block_src), strategy="block")
    m = make_manifest()
    apply([op], paths, m, force=False)
    pre_reverse_content = dest.read_text(encoding="utf-8")
    assert "cohort managed content" in pre_reverse_content

    result = reverse_full(m, paths)

    backup = dest.with_name(dest.name + ".bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == pre_reverse_content
    assert result.removed == 1


def test_json_merge_reverse_backs_up_before_removal(home, src):
    """Same guarantee for the json (hook key-merge) strategy: when the user's own
    content survives the reverse (the file is rewritten, not deleted), snapshot it
    to ``.bak`` first."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "settings.json"
    # Pre-existing USER content — so reverse rewrites (keeps this), never deletes.
    dest.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "user-own"}]}}), encoding="utf-8"
    )
    fragment_src = src / "fragment.json"
    fragment_src.write_text(json.dumps({"hooks": {"PostToolUse": [{"command": "x"}]}}), encoding="utf-8")
    op = Op(OpType.MERGE.value, GLOBAL_IDE, str(dest), src=str(fragment_src), strategy="json")
    m = make_manifest()
    apply([op], paths, m, force=False)
    pre_reverse_content = dest.read_text(encoding="utf-8")

    reverse_full(m, paths)

    backup = dest.with_name(dest.name + ".bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == pre_reverse_content
    assert "user-own" in dest.read_text(encoding="utf-8")  # user's entry survived


def test_delete_if_only_ours_reverse_leaves_no_backup(home, src):
    """A file Cohort CREATED that has nothing but Cohort's content is unlinked on
    reverse with NO ``.bak`` sidecar. A backup there would have nothing of the
    user's to protect and would survive ``deinit --purge``, littering the dir
    (regression guard for the O2-backup / purge interaction)."""
    paths = paths_for(home)
    paths.state.mkdir(parents=True)
    dest = home / "settings.json"  # does NOT pre-exist → Cohort creates it
    fragment_src = src / "fragment.json"
    fragment_src.write_text(json.dumps({"hooks": {"PostToolUse": [{"command": "x"}]}}), encoding="utf-8")
    op = Op(OpType.MERGE.value, GLOBAL_IDE, str(dest), src=str(fragment_src), strategy="json")
    m = make_manifest()
    apply([op], paths, m, force=False)

    reverse_full(m, paths)

    assert not dest.exists()  # delete-if-only-ours
    assert not dest.with_name(dest.name + ".bak").exists()  # no litter

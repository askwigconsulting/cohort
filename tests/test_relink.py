"""#40: `cohort relink` + the dangling-source advisory in `cohort status`."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cohort.install import do_install
from cohort.install_model import CohortPaths
from cohort.status import RELINK_HINT, _source_health
from cohort.update import do_relink
from conftest import requires_symlinks

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_health_ok_when_not_linked(tmp_path):
    gpaths = CohortPaths.for_global(tmp_path / "home")
    assert _source_health(gpaths) == {"ok": True, "linked": False}


@requires_symlinks
def test_source_health_flags_a_dangling_link(tmp_path):
    home = tmp_path / "home"
    gpaths = CohortPaths.for_global(home)
    gpaths.cohort_home.mkdir(parents=True)
    os.symlink(tmp_path / "gone", gpaths.canonical)  # points nowhere
    health = _source_health(gpaths)
    assert health["ok"] is False and health["linked"] is True
    assert health["restore"] == RELINK_HINT


@requires_symlinks
def test_relink_repoints_a_moved_install(tmp_path):
    src1 = tmp_path / "src1"
    src1.mkdir()
    shutil.copytree(REPO_ROOT / "canonical", src1 / "canonical")
    home = tmp_path / "home"
    home.mkdir()
    do_install(home=home, selection=["claude"], mode="link", force=False, source=src1, dry_run=False)

    canonical = CohortPaths.for_global(home).canonical
    assert canonical.is_symlink() and canonical.exists()

    src2 = tmp_path / "src2"
    src1.rename(src2)  # the clone "moves" — link now dangles
    assert canonical.is_symlink() and not canonical.exists()

    result = do_relink(src2, home)
    assert result["refused"] is None and result["recompiled_ides"] == ["claude"]
    assert canonical.exists() and canonical.resolve() == (src2 / "canonical").resolve()


def test_relink_is_a_noop_without_an_install(tmp_path):
    result = do_relink(REPO_ROOT, tmp_path / "home")
    assert result == {"recompiled_ides": [], "refused": None}

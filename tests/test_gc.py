"""`cohort gc` reclaims what Cohort leaves behind — and nothing else.

The motivating failure was real: doer and ratchet runs deliberately keep their worktree so
a human can review the diff, nothing ever collected them, and 1,592 accumulated over nine
days until the tmpfs quota gave out mid-test-run. These tests pin the two properties that
make an automatic deleter safe to ship — it removes only what Cohort created, and it does
not remove anything until asked.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from cohort import gc


def _aged(path: Path, days: float) -> Path:
    """Backdate a path so age thresholds can be tested without waiting."""
    old = time.time() - days * 86400
    import os

    os.utime(path, (old, old))
    return path


def _proposal(tmp: Path, name: str, *, days: float, with_worktree: bool = True) -> Path:
    parent = tmp / f"cohort-proposal-{name}"
    parent.mkdir()
    (parent / "ratchet-results.tsv").write_text("x\n", encoding="utf-8")
    if with_worktree:
        (parent / "worktree").mkdir()
    return _aged(parent, days)


def test_scan_reports_without_removing_anything(tmp_path) -> None:
    """Dry-run is the default because reclaiming is deletion. Scanning must be inert."""
    stale = _proposal(tmp_path, "stale", days=30)

    report = gc.scan(min_age_days=7, temp_root=tmp_path)

    assert [i.path for i in report.items] == [stale]
    assert report.removed == []
    assert stale.exists()  # nothing touched


def test_recent_artifacts_are_left_alone(tmp_path) -> None:
    """Something created an hour ago is usually the thing someone is looking at."""
    _proposal(tmp_path, "fresh", days=0.04)
    assert gc.scan(min_age_days=7, temp_root=tmp_path).items == []


def test_only_cohorts_own_directories_are_candidates(tmp_path) -> None:
    """The prefix is the whole safety argument: never a path a user named."""
    _proposal(tmp_path, "mine", days=30)
    someone_elses = tmp_path / "important-user-data"
    someone_elses.mkdir()
    _aged(someone_elses, 400)
    (tmp_path / "tmpXYZ").mkdir()

    paths = [i.path for i in gc.scan(min_age_days=7, temp_root=tmp_path).items]

    assert someone_elses not in paths
    assert len(paths) == 1


def test_a_directory_with_no_worktree_is_reclaimable(tmp_path) -> None:
    """A run that failed before checkout, or one already half-reaped."""
    empty = _proposal(tmp_path, "empty", days=30, with_worktree=False)
    item = next(i for i in gc.scan(min_age_days=7, temp_root=tmp_path).items if i.path == empty)
    assert item.state == "empty" and item.safe_by_default


def test_a_worktree_git_no_longer_resolves_is_dead(tmp_path) -> None:
    """The unambiguous case: nothing can use it and no repo still lists it."""
    dead = _proposal(tmp_path, "dead", days=30)
    item = next(i for i in gc.scan(min_age_days=7, temp_root=tmp_path).items if i.path == dead)
    assert item.state == "dead" and item.safe_by_default


def test_a_live_worktree_is_reported_but_not_safe_by_default(tmp_path) -> None:
    """"Left for review" means someone may still want that diff — age alone is not
    evidence it is garbage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=repo, check=True, capture_output=True,
    )
    parent = tmp_path / "cohort-proposal-live"
    parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(parent / "worktree"), "HEAD"],
        cwd=repo, check=True, capture_output=True,
    )
    _aged(parent, 30)

    item = next(i for i in gc.scan(min_age_days=7, temp_root=tmp_path).items
                if i.path == parent)
    assert item.state == "live"
    assert not item.safe_by_default

    # And reclaim leaves it alone unless explicitly told otherwise.
    gc.reclaim(gc.scan(min_age_days=7, temp_root=tmp_path))
    assert parent.exists()


def test_reclaim_removes_the_safe_ones_and_reports_what_it_removed(tmp_path) -> None:
    dead = _proposal(tmp_path, "dead", days=30)
    empty = _proposal(tmp_path, "empty", days=30, with_worktree=False)

    report = gc.reclaim(gc.scan(min_age_days=7, temp_root=tmp_path))

    assert set(report.removed) == {dead, empty}
    assert not dead.exists() and not empty.exists()


def test_transcripts_keep_a_tail_however_old_they_are(tmp_path) -> None:
    """Transcripts are the record of what left the machine, so the newest are retained
    regardless of age — only the excess beyond the tail is a candidate."""
    tdir = tmp_path / "repo" / ".cohort" / "engine-transcripts"
    tdir.mkdir(parents=True)
    for i in range(1, 8):
        transcript = tdir / f"{i:04d}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        _aged(transcript, 30)

    report = gc.scan(
        repo_root=tmp_path / "repo", min_age_days=7, keep_transcripts=5,
        temp_root=tmp_path,
    )
    names = sorted(p.path.name for p in report.items if p.kind == "transcript")

    assert names == ["0001.jsonl", "0002.jsonl"]  # the two oldest only


def test_nothing_to_do_is_not_an_error(tmp_path) -> None:
    report = gc.scan(min_age_days=7, temp_root=tmp_path)
    assert report.items == [] and report.reclaimable_bytes == 0

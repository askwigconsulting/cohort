"""Reclaim what Cohort leaves behind.

Several paths deliberately leave artifacts on disk: a doer or ratchet run keeps its
worktree so a human can review the diff, and every `engine review` writes a transcript so
what was egressed stays auditable. Both are correct in isolation and neither was ever
collected, so they accumulate for as long as the machine lives — 1,592 proposal worktrees
over nine days on the author's box, which eventually exhausted the tmpfs quota and failed
an unrelated test run.

The rule this module follows: **Cohort cleans up after itself, and never guesses.**

* **Dry-run by default.** Reclaiming is deletion, so it reports first and acts only when
  asked. There is no "probably fine" path.
* **Only what Cohort created**, matched by its own `cohort-proposal-` prefix inside the
  system temp directory. Never a path a user named.
* **A live worktree is never touched by default.** "Left for review" means someone may
  still want that diff; age alone does not make it garbage. Dead ones — where git no
  longer resolves the worktree at all — are unambiguous and go first.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_PROPOSAL_PREFIX = "cohort-proposal-"
_GIT_TIMEOUT = 15
# Transcripts are the record of what was sent to a vendor, so the default keeps a
# generous tail rather than trimming to the minimum useful number.
DEFAULT_KEEP_TRANSCRIPTS = 50
DEFAULT_MIN_AGE_DAYS = 7


@dataclass
class Reclaimable:
    """One artifact `gc` could remove, and why it is safe (or not) to remove it."""

    path: Path
    kind: str          # "proposal-worktree" | "transcript"
    state: str         # "dead" | "empty" | "live"
    age_days: float
    bytes: int

    @property
    def safe_by_default(self) -> bool:
        """A live worktree may still hold a diff someone means to review."""
        return self.state in ("dead", "empty")


@dataclass
class GcReport:
    items: list[Reclaimable] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    pruned_worktrees: bool = False

    @property
    def reclaimable_bytes(self) -> int:
        return sum(i.bytes for i in self.items if i.safe_by_default)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _worktree_state(parent: Path) -> str:
    """Classify a `cohort-proposal-*` directory.

    ``empty`` — no worktree inside (a run that failed before checkout, or already reaped).
    ``dead``  — a worktree directory whose git linkage no longer resolves, so nothing can
                use it and no repo still lists it.
    ``live``  — git still resolves it; someone may be reviewing the diff.
    """
    worktree = parent / "worktree"
    if not worktree.is_dir():
        return "empty"
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "dead"
    return "live" if proc.returncode == 0 else "dead"


def scan(
    *,
    repo_root: Path | None = None,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
    keep_transcripts: int = DEFAULT_KEEP_TRANSCRIPTS,
    temp_root: Path | None = None,
) -> GcReport:
    """Find what could be reclaimed. Reads only — never removes anything.

    Args:
        repo_root: Repo whose `.cohort/engine-transcripts` to consider; None skips them.
        min_age_days: Ignore anything younger. Recent artifacts are usually the ones
            someone is still looking at.
        keep_transcripts: Always retain this many newest transcripts, whatever their age —
            they are the audit trail of what left the machine.
        temp_root: Override the system temp dir (tests).
    """
    report = GcReport()
    now = time.time()
    root = temp_root or Path(tempfile.gettempdir())

    for parent in sorted(root.glob(f"{_PROPOSAL_PREFIX}*")):
        if not parent.is_dir():
            continue
        try:
            age_days = (now - parent.stat().st_mtime) / 86400
        except OSError:
            continue
        if age_days < min_age_days:
            continue
        report.items.append(
            Reclaimable(
                path=parent, kind="proposal-worktree",
                state=_worktree_state(parent), age_days=age_days,
                bytes=_dir_size(parent),
            )
        )

    if repo_root is not None:
        tdir = repo_root / ".cohort" / "engine-transcripts"
        if tdir.is_dir():
            transcripts = sorted(
                (p for p in tdir.glob("*.jsonl") if p.is_file()),
                key=lambda p: p.name,
            )
            # Keep the newest N by index; only older ones are candidates.
            for old in transcripts[:-keep_transcripts] if keep_transcripts else transcripts:
                try:
                    age_days = (now - old.stat().st_mtime) / 86400
                    size = old.stat().st_size
                except OSError:
                    continue
                if age_days < min_age_days:
                    continue
                report.items.append(
                    Reclaimable(path=old, kind="transcript", state="dead",
                                age_days=age_days, bytes=size)
                )
    return report


def reclaim(
    report: GcReport, *, include_live: bool = False, repo_root: Path | None = None
) -> GcReport:
    """Remove the scanned artifacts. Call only after the user has seen :func:`scan`.

    Skips live worktrees unless ``include_live`` — a diff left for review is not garbage
    just because it is old. Failures are skipped rather than raised: reclaiming is
    best-effort housekeeping and must never take down the command that invoked it.
    """
    for item in report.items:
        if not item.safe_by_default and not include_live:
            continue
        try:
            if item.path.is_dir():
                shutil.rmtree(item.path, ignore_errors=True)
            else:
                item.path.unlink(missing_ok=True)
        except OSError:
            continue
        report.removed.append(item.path)

    # Drop registry entries for worktrees whose directories are now gone.
    if repo_root is not None:
        try:
            subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "prune"],
                capture_output=True, timeout=_GIT_TIMEOUT,
            )
            report.pruned_worktrees = True
        except (OSError, subprocess.SubprocessError):
            pass
    return report

"""`cohort distill`: compound sessions + feedback into a provenance-cited,
confirm-gated, append-only addition to project_context.md.

The write path's safety is behavioral, not asserted by static checks: a real write
demands an affirmative confirm over a control-char-escaped diff; no confirm (an
unattended path) never writes; the appended section survives `context refresh`; and
no canonical hook can invoke it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cohort.distill import do_distill
from cohort.loader import load_artifact
from cohort.project import do_context_refresh

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None, stdin=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args],
        cwd=cwd, input=stdin, capture_output=True, text=True, env=env,
    )


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    return src


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    run_cli("recompile", "--ide", "claude", "--source", str(tmp_path / "src"), home=h)
    return h


@pytest.fixture
def repo(tmp_path, source, home):
    r = make_git_repo(tmp_path / "repo")
    run_cli("init", "--source", str(source), home=home, cwd=r)
    return r


# --- helpers ----------------------------------------------------------------


def _recent_iso(days_ago: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def write_session(repo: Path, name: str, *, decisions=(), open_items=(), ts=None) -> None:
    lines = [
        "---",
        f"timestamp: {ts or _recent_iso()}",
        "author: Dev",
        "branch: main",
        "---",
        "## Decisions",
        *[f"- {d}" for d in decisions],
        "",
        "## Open items",
        *[f"- {o}" for o in open_items],
        "",
    ]
    (repo / ".cohort" / "sessions" / name).write_text("\n".join(lines), encoding="utf-8")


def write_feedback(repo: Path, name: str, *, rating="down", agent="counsel", note="", ts=None) -> None:
    fb_dir = repo / ".cohort" / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"rating: {rating}", f"agent: {agent}",
             f"timestamp: {ts or _recent_iso()}", "---", note, ""]
    (fb_dir / name).write_text("\n".join(lines), encoding="utf-8")


def context_text(repo: Path) -> str:
    return (repo / ".cohort" / "project_context.md").read_text(encoding="utf-8")


# --- empty / dry-run --------------------------------------------------------


def test_distill_empty_sessions_and_feedback_is_a_clean_noop(repo):
    before = context_text(repo)
    report = do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    assert report["empty"] is True
    assert context_text(repo) == before  # nothing written


def test_distill_dry_run_previews_diff_but_writes_nothing(repo):
    write_session(repo, "s1.md", decisions=["Chose SQLite for the trade log"])
    before = context_text(repo)
    report = do_distill(repo, days=30, dry_run=True)
    assert report["dry_run"] is True
    assert "Chose SQLite for the trade log" in report["diff"]
    assert context_text(repo) == before  # dry-run never writes


# --- the confirm gate -------------------------------------------------------


def test_distill_rejection_path_leaves_context_unchanged(repo):
    write_session(repo, "s1.md", decisions=["Chose SQLite for the trade log"])
    before = context_text(repo)
    report = do_distill(repo, days=30, dry_run=False, confirm=lambda _d: False)
    assert report["applied"] is False
    assert context_text(repo) == before  # user declined → no write


def test_distill_confirm_appends_dated_extractive_section_with_citations(repo):
    write_session(
        repo, "20260709T101010Z-aaa111.md",
        decisions=["Chose SQLite for the trade log"],
        open_items=["Wire up the risk engine"],
        ts=_recent_iso(1),
    )
    report = do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    assert report["applied"] is True
    text = context_text(repo)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"## Distilled ({today}) — review provenance" in text
    # extractive quote, verbatim (never rewritten into an instruction)
    assert "- Chose SQLite for the trade log" in text
    assert "- Wire up the risk engine" in text
    # per-line provenance citation: source file + record date
    assert "20260709T101010Z-aaa111.md" in text


def test_distill_never_writes_unattended_when_no_confirm_is_supplied(repo):
    """Fail-closed: a real (non-dry-run) write with no confirm callback — the shape an
    unattended/hooked invocation would take — must never write."""
    write_session(repo, "s1.md", decisions=["Chose SQLite for the trade log"])
    before = context_text(repo)
    report = do_distill(repo, days=30, dry_run=False, confirm=None)
    assert report["applied"] is False
    assert context_text(repo) == before


# --- untrusted-input hardening ----------------------------------------------


def test_distill_escapes_control_chars_in_section_and_diff(repo):
    # A contributor-writable session record embeds ANSI/CR to disguise the approved
    # line; the escape must neutralize it in both the diff and the written section.
    write_session(repo, "s1.md", decisions=["Use \x1b[31mred\x1b[0m and \r overwrite"])
    preview = do_distill(repo, days=30, dry_run=True)
    assert "\x1b" not in preview["diff"] and "\r" not in preview["diff"]
    assert "\\x1b" in preview["diff"]  # rendered visibly instead
    do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    written = context_text(repo)
    assert "\x1b" not in written and "\\x1b" in written


def test_distill_escapes_c1_csi_and_unicode_line_separators(repo):
    # \x9b is a one-byte CSI introducer (ANSI without ESC) in the C1 range;
    # U+2028/U+2029 are Unicode line separators that could split a diff line.
    write_session(repo, "s1.md", decisions=["red \x9b31m and split\u2028here\u2029end"])
    preview = do_distill(repo, days=30, dry_run=True)
    for raw in ("\x9b", "\u2028", "\u2029"):
        assert raw not in preview["diff"]
    assert "\\x9b" in preview["diff"]  # rendered visibly instead
    do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    written = context_text(repo)
    assert "\x9b" not in written and "\\x9b" in written


def test_multiline_feedback_note_cannot_inject_uncited_lines(repo):
    """A contributor-writable note spanning several lines — including a forged
    `## Distilled` header — must collapse to one prefixed, cited line: no bare
    (unprefixed, uncited) markdown line from the note may survive into the section."""
    write_feedback(
        repo, "f1.md", rating="down", agent="counsel",
        note="first line of the note\n"
             "## Distilled (2020-01-01) — review provenance\n"
             "trailing contributor markdown",
    )
    preview = do_distill(repo, days=30, dry_run=True)
    section = preview["section"]
    assert "## Distilled (2020-01-01)" not in section  # forged header neutralized
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for line in section.splitlines():
        if line.startswith("## "):  # the only ## line is the legitimate header
            assert line == f"## Distilled ({today}) — review provenance"
    # the note renders as ONE physical line: prefixed, clip-marked, and cited
    note_lines = [ln for ln in section.splitlines() if "first line of the note" in ln]
    assert len(note_lines) == 1
    line = note_lines[0]
    assert line.startswith("- down on counsel: ")
    assert "[…]" in line and "feedback/f1.md" in line
    # clipped content does not leak in at all
    assert "trailing contributor markdown" not in section

    do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    assert "## Distilled (2020-01-01)" not in context_text(repo)


# --- source selection: sessions + feedback only -----------------------------


def test_distill_reads_feedback_but_never_the_reports_directory(repo):
    write_feedback(repo, "f1.md", rating="down", agent="counsel",
                   note="counsel was too verbose")
    reports_dir = repo / ".cohort" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "weekly-2026-07-10.md").write_text(
        "## Decisions\n- SECRET_FROM_REPORTS should never be distilled\n",
        encoding="utf-8",
    )
    do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    text = context_text(repo)
    assert "counsel was too verbose" in text        # feedback IS read
    assert "SECRET_FROM_REPORTS" not in text         # reports/ is NOT read


# --- refresh survival -------------------------------------------------------


def test_distilled_section_survives_context_refresh(repo):
    write_session(repo, "s1.md", decisions=["Chose SQLite for the trade log"])
    do_distill(repo, days=30, dry_run=False, confirm=lambda _d: True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"## Distilled ({today})" in context_text(repo)

    result = do_context_refresh(repo, dry_run=False)
    assert "error" not in result
    text = context_text(repo)
    assert f"## Distilled ({today})" in text                 # distilled survived
    assert "- Chose SQLite for the trade log" in text
    assert "<!-- >>> cohort (managed)" in text               # managed block intact


# --- never invocable from a hook (regression alongside the human-gate tests) -


def test_no_canonical_hook_invokes_distill():
    hooks_dir = COHORT_SRC / "canonical" / "hooks"
    for hook in hooks_dir.glob("*.md"):
        action = str((load_artifact(hook).frontmatter or {}).get("action", ""))
        assert "distill" not in action, f"{hook.name} wires distill into a hook"


# --- CLI wiring end to end --------------------------------------------------


def test_distill_cli_confirm_yes_appends(repo, home):
    write_session(repo, "s1.md", decisions=["Chose SQLite for the trade log"])
    proc = run_cli("distill", "--days", "30", home=home, cwd=repo, stdin="y\n")
    assert proc.returncode == 0
    assert "appended" in proc.stdout
    assert "Chose SQLite for the trade log" in context_text(repo)


def test_distill_cli_confirm_no_declines(repo, home):
    write_session(repo, "s1.md", decisions=["Chose SQLite for the trade log"])
    before = context_text(repo)
    proc = run_cli("distill", "--days", "30", home=home, cwd=repo, stdin="n\n")
    assert proc.returncode == 0
    assert "declined" in proc.stdout
    assert context_text(repo) == before

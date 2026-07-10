"""Wording-lock for the compiled /goal command (#141).

/goal drives a GitHub issue to a *draft* PR through an independent judge. Two
guards in its body are safety-critical and must never silently regress, so they
are locked here in the style of the other compiled-wording tests
(test_roster_compile, test_golden_lock):

- the **push prohibition** — push only via an explicit `git push -u origin HEAD`,
  and never merge the PR; and
- the **default-branch check** performed before any push.

The instruction-level prohibition's only backstop is the IDE permission system,
not Cohort — hence the draft PR and this lock. If the wording is reworded,
update these assertions deliberately.
"""

from __future__ import annotations

from pathlib import Path

from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]


def _compiled_goal_body() -> str:
    """Return the compiled Claude /goal command body (fresh from the renderer)."""
    staged = {sf.staged_rel: sf.content for sf in compile_ide(REPO, "claude").staged}
    rel = "commands/goal.md"
    assert rel in staged, f"/goal did not compile for claude; got {sorted(staged)}"
    return staged[rel].decode("utf-8")


def test_goal_locks_push_prohibition():
    body = _compiled_goal_body()
    # Push only via the explicit, current-branch push — never a bare push, never merge.
    assert "git push -u origin HEAD" in body
    assert "Never merge the PR" in body


def test_goal_locks_default_branch_check():
    body = _compiled_goal_body()
    assert "verify the current branch is not the default branch" in body


def test_goal_opens_only_a_draft_pr():
    body = _compiled_goal_body()
    assert "gh pr create --draft" in body
    # Final-round FAIL must never present as a ready PR.
    assert "Do **not**\nopen a ready PR" in body or "Do **not** open a ready PR" in body


def test_goal_judge_treats_repo_content_as_untrusted():
    body = _compiled_goal_body()
    assert "untrusted claims" in body
    # Criteria are fetched once, body only, and confirmed before the loop.
    assert "gh issue view <number> --json body,title" in body

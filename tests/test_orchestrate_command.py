"""Wording-lock for the compiled /orchestrate command.

/orchestrate is the standard fan-out development protocol: the coordinating
session (Fable) researches, plans, routes tasks to model tiers, and signs off.
Three guards in its body are load-bearing and must never silently regress, in
the style of test_goal_command:

- the **concurrency cap** — never more than 10 agents in flight at once;
- the **coordinator signoff** — worker output is a claim, verified by the
  coordinator re-running tests, never rubber-stamped; and
- the **isolation rule** — parallel writers need disjoint file footprints or
  worktree isolation.

If the wording is reworded, update these assertions deliberately.
"""

from __future__ import annotations

from pathlib import Path

from cohort.compile import compile_ide

REPO = Path(__file__).resolve().parents[1]


def _compiled_orchestrate_body() -> str:
    """Return the compiled Claude /orchestrate command body (fresh from the renderer)."""
    staged = {sf.staged_rel: sf.content for sf in compile_ide(REPO, "claude").staged}
    rel = "commands/orchestrate.md"
    assert rel in staged, f"/orchestrate did not compile for claude; got {sorted(staged)}"
    return staged[rel].decode("utf-8")


def test_orchestrate_locks_the_ten_agent_cap():
    body = _compiled_orchestrate_body()
    assert "no more than 10 agents in flight at once, across all tiers" in body


def test_orchestrate_locks_coordinator_signoff():
    body = _compiled_orchestrate_body()
    # Signoff verifies claims — the coordinator re-runs tests itself.
    assert "claim, not a completion" in body
    assert "re-running tests" in body
    assert "never rubber-stamps" in body


def test_orchestrate_locks_parallel_writer_isolation():
    body = _compiled_orchestrate_body()
    # Reworded (review GT1/GT3): concurrent writers must use per-task worktrees,
    # and workers may not commit in the coordinator's shared checkout.
    assert "Concurrent writers require per-task git worktrees" in body
    assert "Forbids worker commits in the coordinator's shared checkout" in body


def test_orchestrate_locks_the_worker_kickback():
    # Escalation runs both ways: coordinator from above (routing/signoff), worker
    # from below (kickback). A kickback skips the same-tier retry and escalates.
    flat = " ".join(_compiled_orchestrate_body().split())
    assert "kickback rule" in flat
    assert "the worker's check from below" in flat
    assert "skips the retry and escalates a tier immediately" in flat


def test_orchestrate_routes_all_four_tiers():
    body = _compiled_orchestrate_body()
    for tier in ("fable", "opus", "sonnet", "haiku"):
        assert f"**{tier}**" in body, f"tier {tier} missing from routing table"


def test_orchestrate_keeps_coordination_on_the_top_level_session():
    body = _compiled_orchestrate_body()
    assert "never delegates the plan or the signoff to a subagent" in body


def test_orchestrate_opus_is_a_first_class_coordinator():
    # A native Opus session orchestrates in its own right (not just a Fable
    # fallback), operating in Fable mode and handling fable-tier work itself.
    body = _compiled_orchestrate_body()
    flat = " ".join(body.split())
    assert "coordinator-tier" in body
    assert "not a degraded fallback" in body
    assert "Handle fable-tier work yourself, routed to opus" in flat


def test_orchestrate_opus_escalates_a_fable_suited_task_to_the_user():
    # Opus must not silently absorb a task genuinely better suited to Fable:
    # it raises the three-way decision (task it to Fable / save it / skip).
    flat = " ".join(_compiled_orchestrate_body().split())
    assert "genuinely better\n  suited to Fable".replace("\n  ", " ") in flat
    assert "Raise it to the user" in flat
    assert "task that piece to Fable now" in flat
    assert "document and save it as future work" in flat
    assert "skip it" in flat


def test_orchestrate_never_coordinates_below_opus():
    # The pattern must never repeat on Sonnet/Haiku: recommend switching up.
    body = _compiled_orchestrate_body()
    flat = " ".join(_compiled_orchestrate_body().split())
    assert "do not orchestrate" in body
    assert "the pattern never repeats below Opus" in flat

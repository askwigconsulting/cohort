"""Tests for the ratchet loop (cohort.engines.ratchet).

The proposing doer is mocked to write a metric value into the worktree; everything else
is real - the evaluator command runs, the metric is parsed, and git keep/revert actually
commits or resets - so the ratchet's core (climb, keep-only-gains, revert) is exercised
end to end without any external engine or network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cohort.engines import gates, ratchet


def _init_git_repo(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.co", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=root, check=True, capture_output=True,
    )


def _doer_writes(values):
    """A _propose_into_worktree stand-in: each call writes the next value to metric.txt."""
    seq = iter(values)

    def propose(engine, task, worktree, **kwargs):
        (Path(worktree) / "metric.txt").write_text(str(next(seq)), encoding="utf-8")

    return propose


def test_ratchet_keeps_only_improvements_when_minimizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    # i1: 9 (better, keep) - i2: 12 (worse, revert) - i3: 7 (better, keep)
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([9, 12, 7]))

    result = ratchet.run_ratchet(
        "gpt", "lower the number", repo_root=tmp_path,
        evaluator_cmd="cat metric.txt", goal="minimize", budget=3,
    )

    assert result.baseline == 10.0
    assert result.best == 7.0
    assert [s.iteration for s in result.steps if s.kept] == [1, 3]
    assert not result.steps[1].kept  # the worse attempt was reverted
    assert (result.worktree / "metric.txt").read_text(encoding="utf-8").strip() == "7"
    assert result.improved


def test_ratchet_maximizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "5\n"})
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([8, 3, 9]))
    result = ratchet.run_ratchet(
        "gpt", "raise it", repo_root=tmp_path,
        evaluator_cmd="cat metric.txt", goal="maximize", budget=3,
    )
    assert result.best == 9.0
    assert [s.iteration for s in result.steps if s.kept] == [1, 3]  # 3 was worse, reverted


def test_ratchet_reverts_a_tie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([10]))  # no change
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd="cat metric.txt", budget=1,
    )
    assert result.best == 10.0
    assert not any(s.kept for s in result.steps)  # a tie is not a gain


def test_ratchet_writes_the_staircase_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    monkeypatch.setattr(ratchet, "_propose_into_worktree", _doer_writes([9, 8]))
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd="cat metric.txt", budget=2,
    )
    ledger = result.ledger_path.read_text(encoding="utf-8")
    assert "baseline" in ledger
    assert ledger.count("kept") >= 2  # header word + two kept rows


def test_ratchet_a_failed_proposal_is_reverted_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    calls = {"n": 0}

    def flaky(engine, task, worktree, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("doer crashed")
        (Path(worktree) / "metric.txt").write_text("6", encoding="utf-8")

    monkeypatch.setattr(ratchet, "_propose_into_worktree", flaky)
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd="cat metric.txt", budget=2,
    )
    assert result.steps[0].kept is False and "failed" in result.steps[0].note
    assert result.best == 6.0  # the second iteration still improved


def test_ratchet_refuses_when_baseline_metric_is_unreadable(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, {"a.txt": "x\n"})
    with pytest.raises(ratchet.RatchetError, match="baseline"):
        ratchet.run_ratchet(
            "gpt", "t", repo_root=tmp_path, evaluator_cmd="echo no-number-here", budget=1,
        )


def test_ratchet_honors_egress_optout_before_any_doer_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    reached = {"doer": False}

    def must_not_run(*a, **k):
        reached["doer"] = True

    monkeypatch.setattr(ratchet, "_propose_into_worktree", must_not_run)
    with pytest.raises(gates.EgressBlockedError):
        ratchet.run_ratchet(
            "gpt", "t", repo_root=tmp_path, evaluator_cmd="cat metric.txt", budget=1,
            project_context_text="## Egress\n\ncohort:egress=deny\n",
        )
    assert reached["doer"] is False


def test_ratchet_rejects_empty_task_and_evaluator(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "10\n"})
    with pytest.raises(ratchet.RatchetError):
        ratchet.run_ratchet("gpt", "   ", repo_root=tmp_path, evaluator_cmd="cat metric.txt", budget=1)
    with pytest.raises(ratchet.RatchetError):
        ratchet.run_ratchet("gpt", "t", repo_root=tmp_path, evaluator_cmd="  ", budget=1)


def test_ratchet_metric_regex_extracts_the_right_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"metric.txt": "val_bpb=1.5 tokens=999\n"})

    def propose(engine, task, worktree, **kwargs):
        (Path(worktree) / "metric.txt").write_text("val_bpb=1.2 tokens=111", encoding="utf-8")

    monkeypatch.setattr(ratchet, "_propose_into_worktree", propose)
    result = ratchet.run_ratchet(
        "gpt", "t", repo_root=tmp_path, evaluator_cmd="cat metric.txt",
        metric_regex=r"val_bpb=([0-9.]+)", goal="minimize", budget=1,
    )
    assert result.baseline == 1.5 and result.best == 1.2  # not fooled by the token count

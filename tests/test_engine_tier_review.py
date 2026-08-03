"""Behaviour tests for the engine CLI wiring added on the xai-agentic branch:

* ``engine consult`` model-tier selection (``--tier`` / ``--model``);
* the new ``engine review`` agentic-transport command;
* the F5 fail-closed egress-provenance guard shared by both.

Network is always mocked — either by patching ``engine_xai.consult`` /
``xai_agentic.run_agentic``, or (for the transcript-writing test) by injecting a fake
poster into the real agentic loop via ``xai_agentic._post_chat``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cohort import cli
from cohort.cli import app
from cohort.engines import xai
from cohort.engines.cli_doer import DoerResult, GrokReviewResult
from cohort.engines.xai_agentic import AgenticResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def _grok_cli_unavailable_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test here to the xAI API-direct path.

    ``engine consult``/``review``/``propose`` now prefer the local grok CLI whenever
    grok-cli AND bwrap are installed — which they may be on the test host — so pin the CLI
    as 'unavailable' by default, keeping the API-path wiring tests deterministic. The
    CLI-preferred tests below override this by patching _grok_cli_available to True."""
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: False)


# --- engine consult: tier / model selection --------------------------------


def _capture_consult():
    captured: dict[str, Any] = {}

    def fake_consult(
        prompt: str, *, model: str | None, max_tokens: int | None, **_kw
    ) -> str:
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return "reply"

    return captured, fake_consult


def test_engine_consult_cheap_tier_resolves_to_the_cheap_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("hi", encoding="utf-8")
    captured, fake = _capture_consult()
    with patch("cohort.cli.engine_xai.consult", side_effect=fake):
        result = runner.invoke(
            app, ["engine", "consult", "grok", "--prompt-file", str(prompt_file), "--tier", "cheap"]
        )
    assert result.exit_code == 0, result.output
    assert captured["model"] == "grok-4.3"  # the registry's cheap tier


def test_engine_consult_explicit_model_overrides_the_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("hi", encoding="utf-8")
    captured, fake = _capture_consult()
    with patch("cohort.cli.engine_xai.consult", side_effect=fake):
        result = runner.invoke(
            app,
            ["engine", "consult", "grok", "--prompt-file", str(prompt_file), "--model", "grok-custom-9"],
        )
    assert result.exit_code == 0, result.output
    assert captured["model"] == "grok-custom-9"


def test_engine_consult_tier_and_model_are_mutually_exclusive(tmp_path: Path):
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("hi", encoding="utf-8")
    consult_mock = MagicMock(return_value="unreached")
    with patch("cohort.cli.engine_xai.consult", consult_mock):
        result = runner.invoke(
            app,
            [
                "engine", "consult", "grok", "--prompt-file", str(prompt_file),
                "--tier", "cheap", "--model", "grok-custom-9",
            ],
        )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
    consult_mock.assert_not_called()


def test_engine_consult_unknown_tier_is_a_usage_error(tmp_path: Path):
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("hi", encoding="utf-8")
    consult_mock = MagicMock(return_value="unreached")
    with patch("cohort.cli.engine_xai.consult", consult_mock):
        result = runner.invoke(
            app,
            ["engine", "consult", "grok", "--prompt-file", str(prompt_file), "--tier", "titanium"],
        )
    assert result.exit_code == 2
    assert "unknown tier" in result.output and "titanium" in result.output
    consult_mock.assert_not_called()


# --- F5: fail-closed egress provenance -------------------------------------


def test_engine_consult_refuses_egress_with_no_repo_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A bare working dir (no .git and no .cohort ancestor) has no per-repo egress
    # opt-out to consult and no provenance for the piped code — refuse, fail closed.
    monkeypatch.chdir(tmp_path)
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("review this", encoding="utf-8")
    consult_mock = MagicMock(return_value="should never be reached")
    with patch("cohort.cli.engine_xai.consult", consult_mock):
        result = runner.invoke(
            app, ["engine", "consult", "grok", "--prompt-file", str(prompt_file)]
        )
    assert result.exit_code == 1
    assert "no repository context" in result.output
    consult_mock.assert_not_called()  # nothing egressed


def test_cohorts_own_global_state_dir_is_not_repository_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$HOME/.cohort`` is Cohort's own state directory, not a repo.

    It exists on every installed machine, so counting it as provenance made the
    fail-closed guard pass for *every* directory under ``$HOME`` — which is nearly every
    directory a user works in. Windows CI surfaced this because pytest's tmp dir lives
    under the home directory there.
    """
    home = tmp_path / "home"
    (home / ".cohort" / "state").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    bare = home / "scratch"          # under $HOME, but not a repo
    bare.mkdir()
    assert not cli._repo_has_egress_provenance(bare)

    project = home / "proj"          # a real project's .cohort still counts
    (project / ".cohort").mkdir(parents=True)
    assert cli._repo_has_egress_provenance(project / "src")


def test_engine_consult_allow_egress_overrides_missing_repo_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("hello", encoding="utf-8")
    with patch("cohort.cli.engine_xai.consult", return_value="ok") as consult_mock:
        result = runner.invoke(
            app,
            ["engine", "consult", "grok", "--prompt-file", str(prompt_file), "--allow-egress"],
        )
    assert result.exit_code == 0, result.output
    consult_mock.assert_called_once()


def test_engine_review_refuses_egress_with_no_repo_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("audit the auth flow", encoding="utf-8")
    run_mock = MagicMock()
    with patch("cohort.engines.xai_agentic.run_agentic", run_mock):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(task_file)]
        )
    assert result.exit_code == 1
    assert "no repository context" in result.output
    run_mock.assert_not_called()


# --- engine review: the agentic transport ----------------------------------


def test_engine_review_wires_model_root_and_transcript_and_prints_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # find_repo_root → tmp_path so the transcript is stamped/written under tmp, not the
    # real repo. Path.cwd() stays the (git) test repo, so the F5 guard passes.
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("find the highest-risk change", encoding="utf-8")

    captured: dict[str, Any] = {}

    def fake_run_agentic(task: str, **kwargs: Any) -> AgenticResult:
        captured["task"] = task
        captured.update(kwargs)
        return AgenticResult(text="final review\nsecond line", stopped_reason="final")

    with patch("cohort.engines.xai_agentic.run_agentic", side_effect=fake_run_agentic):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(task_file)]
        )

    assert result.exit_code == 0, result.output
    assert captured["task"] == "find the highest-risk change"
    assert captured["model"] == "grok-4.5"  # default flagship tier
    assert captured["root"] == tmp_path
    assert captured["engine_name"] == "grok"
    assert captured["transcript_path"] == tmp_path / ".cohort" / "engine-transcripts" / "0001.jsonl"
    assert "final review" in result.output and "second line" in result.output
    assert "stopped_reason: final" in result.output
    assert "transcript:" in result.output


def test_engine_review_transcript_stamp_increments_past_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    tdir = tmp_path / ".cohort" / "engine-transcripts"
    tdir.mkdir(parents=True)
    (tdir / "0007.jsonl").write_text("", encoding="utf-8")
    task_file = tmp_path / "task.txt"
    task_file.write_text("review", encoding="utf-8")

    captured: dict[str, Any] = {}

    def fake_run_agentic(task: str, **kwargs: Any) -> AgenticResult:
        captured.update(kwargs)
        return AgenticResult(text="ok", stopped_reason="final")

    with patch("cohort.engines.xai_agentic.run_agentic", side_effect=fake_run_agentic):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(task_file)]
        )
    assert result.exit_code == 0, result.output
    assert captured["transcript_path"] == tdir / "0008.jsonl"


def test_engine_review_transcript_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("review", encoding="utf-8")
    override = tmp_path / "custom.jsonl"

    captured: dict[str, Any] = {}

    def fake_run_agentic(task: str, **kwargs: Any) -> AgenticResult:
        captured.update(kwargs)
        return AgenticResult(text="ok", stopped_reason="final")

    with patch("cohort.engines.xai_agentic.run_agentic", side_effect=fake_run_agentic):
        result = runner.invoke(
            app,
            ["engine", "review", "grok", "--task-file", str(task_file), "--transcript", str(override)],
        )
    assert result.exit_code == 0, result.output
    assert captured["transcript_path"] == override


def test_engine_review_blocks_a_task_containing_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text(
        "here is context\nAWS_SECRET_ACCESS_KEY = wJalrXUtnFEMIK7MDENGbPxRfiCY\n",
        encoding="utf-8",
    )
    run_mock = MagicMock()
    with patch("cohort.engines.xai_agentic.run_agentic", run_mock):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(task_file)]
        )
    assert result.exit_code == 1
    run_mock.assert_not_called()
    assert "wJalrXUtnFEMIK" not in result.output  # label names the shape, not the value


def test_engine_review_maps_auth_error_to_exit_1_without_leaking_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("review", encoding="utf-8")
    with patch(
        "cohort.engines.xai_agentic.run_agentic",
        side_effect=xai.EngineAuthError("environment variable GROK_API_KEY is unset"),
    ):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(task_file)]
        )
    assert result.exit_code == 1
    assert "console.x.ai" in result.output and "GROK_API_KEY" in result.output
    assert "Bearer" not in result.output


def test_engine_review_runs_the_real_loop_and_writes_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Exercise the real run_agentic loop with a fake poster (its test seam) so the
    # transcript file is actually produced, without any network.
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    task_file = tmp_path / "task.txt"
    task_file.write_text("summarise the repo", encoding="utf-8")

    def fake_post(spec, key, body):
        # No tool_calls → the loop takes the model's answer and stops.
        return {"choices": [{"message": {"content": "the repo does X"}}]}

    with patch("cohort.engines.xai_agentic._post_chat", side_effect=fake_post):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(task_file)]
        )

    assert result.exit_code == 0, result.output
    assert "the repo does X" in result.output
    assert "stopped_reason: final" in result.output
    transcript = tmp_path / ".cohort" / "engine-transcripts" / "0001.jsonl"
    assert transcript.is_file()  # the audit trail was written


# --- CLI-preferred dispatch: prefer the local grok CLI over the xAI API ------
#
# When the bubblewrap-sandboxed grok CLI is installed it has real, worktree-scoped file
# access the API-direct path lacks, so every grok read/consult/propose path prefers it and
# falls back to the API (with a printed note) only when it is unavailable. grok-only —
# codex/other engines are untouched.


def _prompt_file(tmp_path: Path, text: str = "review this") -> Path:
    p = tmp_path / "task.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_engine_consult_prefers_grok_cli_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: True)
    review = GrokReviewResult(
        engine="grok", analysis="LOCAL-CLI-ANALYSIS", transcript="LOCAL-CLI-ANALYSIS",
        returncode=0,
    )
    review_mock = MagicMock(return_value=review)
    consult_mock = MagicMock(return_value="API-SHOULD-NOT-RUN")
    with patch("cohort.engines.cli_doer.run_grok_review", review_mock), \
         patch("cohort.cli.engine_xai.consult", consult_mock):
        result = runner.invoke(
            app, ["engine", "consult", "grok", "--prompt-file", str(_prompt_file(tmp_path))]
        )
    assert result.exit_code == 0, result.output
    assert "LOCAL-CLI-ANALYSIS" in result.output
    assert "local grok CLI" in result.output   # the CLI-preferred note
    review_mock.assert_called_once()
    consult_mock.assert_not_called()            # the API path was not taken


def test_engine_consult_falls_back_to_api_when_grok_cli_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: False)
    consult_mock = MagicMock(return_value="API-REPLY")
    with patch("cohort.cli.engine_xai.consult", consult_mock):
        result = runner.invoke(
            app, ["engine", "consult", "grok", "--prompt-file", str(_prompt_file(tmp_path))]
        )
    assert result.exit_code == 0, result.output
    assert "API-REPLY" in result.output
    assert "xAI API-direct" in result.output    # the fallback note
    consult_mock.assert_called_once()


def test_engine_review_prefers_grok_cli_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: True)
    review = GrokReviewResult(
        engine="grok", analysis="CLI-REVIEW-OUTPUT", transcript="CLI-REVIEW-OUTPUT",
        returncode=0,
    )
    review_mock = MagicMock(return_value=review)
    agentic_mock = MagicMock()
    with patch("cohort.engines.cli_doer.run_grok_review", review_mock), \
         patch("cohort.engines.xai_agentic.run_agentic", agentic_mock):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(_prompt_file(tmp_path))]
        )
    assert result.exit_code == 0, result.output
    assert "CLI-REVIEW-OUTPUT" in result.output
    assert "local grok CLI" in result.output
    review_mock.assert_called_once()
    agentic_mock.assert_not_called()            # the API agentic loop was not taken


def test_engine_review_falls_back_to_api_when_grok_cli_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: False)
    agentic_mock = MagicMock(
        return_value=AgenticResult(text="API-AGENTIC-OUTPUT", stopped_reason="final")
    )
    with patch("cohort.engines.xai_agentic.run_agentic", agentic_mock):
        result = runner.invoke(
            app, ["engine", "review", "grok", "--task-file", str(_prompt_file(tmp_path))]
        )
    assert result.exit_code == 0, result.output
    assert "API-AGENTIC-OUTPUT" in result.output
    assert "xAI API-direct" in result.output
    agentic_mock.assert_called_once()


def test_engine_propose_prefers_grok_cli_and_emits_its_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: True)
    doer_result = DoerResult(
        engine="grok", worktree=tmp_path / "wt", changed_files=["src/app.py"],
        diff="--- a\n+++ b\n", returncode=0, stdout_tail="", footprint_violations=[],
    )
    doer_mock = MagicMock(return_value=doer_result)
    propose_mock = MagicMock()
    with patch("cohort.engines.cli_doer.run_grok_doer", doer_mock), \
         patch("cohort.engines.patch_proposal.propose_patch", propose_mock):
        result = runner.invoke(
            app,
            ["engine", "propose", "grok", "--footprint", "src",
             "--task-file", str(_prompt_file(tmp_path))],
        )
    assert result.exit_code == 0, result.output
    assert "local grok CLI" in result.output
    assert "changed: src/app.py" in result.output
    assert "nothing was auto-applied" in result.output
    doer_mock.assert_called_once()
    propose_mock.assert_not_called()            # the API patch path was not taken


def test_engine_propose_falls_back_to_api_when_grok_cli_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from cohort.engines import patch_proposal

    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: False)
    # Short-circuit the API path with a known error: it proves routing reached it without
    # having to fabricate a full proposal outcome.
    propose_mock = MagicMock(side_effect=patch_proposal.ProposalError("stop here"))
    with patch("cohort.engines.patch_proposal.propose_patch", propose_mock):
        result = runner.invoke(
            app,
            ["engine", "propose", "grok", "--footprint", "src",
             "--task-file", str(_prompt_file(tmp_path))],
        )
    assert result.exit_code == 1, result.output
    assert "xAI API-direct" in result.output    # the fallback note printed first
    propose_mock.assert_called_once()


def test_gate_refusal_on_cli_path_does_not_fall_back_to_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A gate failure on the preferred CLI path is a refusal, never a reason to retry the
    API — run_grok_review raising a gate error must NOT reach engine_xai.consult."""
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: True)
    from cohort.engines import gates

    review_mock = MagicMock(side_effect=gates.SecretFoundError("task carries a secret"))
    consult_mock = MagicMock(return_value="API-MUST-NOT-RUN")
    with patch("cohort.engines.cli_doer.run_grok_review", review_mock), \
         patch("cohort.cli.engine_xai.consult", consult_mock):
        result = runner.invoke(
            app, ["engine", "consult", "grok", "--prompt-file", str(_prompt_file(tmp_path))]
        )
    assert result.exit_code == 1, result.output
    assert "task carries a secret" in result.output
    consult_mock.assert_not_called()            # gate refusal did NOT fall back to the API

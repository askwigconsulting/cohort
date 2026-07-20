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

from cohort.cli import app
from cohort.engines import xai
from cohort.engines.xai_agentic import AgenticResult

runner = CliRunner()


# --- engine consult: tier / model selection --------------------------------


def _capture_consult():
    captured: dict[str, Any] = {}

    def fake_consult(prompt: str, *, model: str | None, max_tokens: int | None) -> str:
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

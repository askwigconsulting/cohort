"""Tests for the ``patch_proposal`` loop (:mod:`cohort.engines.patch_proposal`).

The loop is the invariant-crossing integration of RFC 0004 Phase 3: an *untrusted*
external engine proposes a change as text, and Cohort — never the engine — gates it,
applies it in an isolated git worktree, and leaves it staged for human review. These
tests use a real temp git repo but **never touch the network or the real xAI API**:
``xai.consult`` is monkeypatched to return canned text.

The safety-critical properties asserted here:

* a successful proposal lands only in the throwaway worktree — the source repo's
  working tree is untouched, and the change is never committed;
* an egress opt-out blocks *before* the engine is ever called (fail closed);
* a path outside the declared footprint, or a ``.git`` path, is rejected and the
  worktree is cleaned up — no leak;
* a malformed engine reply is rejected and the worktree is cleaned up.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cohort.cli import app
from cohort.engines import gates, patch_proposal
from cohort.engines.patch import PatchParseError
from cohort.engines.xai_agentic import AgenticResult

runner = CliRunner()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _init_git_repo(root: Path, files: dict[str, str]) -> None:
    """Create a git repo at ``root`` with ``files`` (relative path -> content) committed."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test",
            "commit", "-q", "-m", "initial",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _worktree_count(root: Path) -> int:
    """Number of git worktrees registered for the repo at ``root`` (1 == just main)."""
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sum(1 for line in out.splitlines() if line.startswith("worktree "))


def _patch_json(
    *,
    summary: str = "bump the value",
    edits: list[dict[str, str]] | None = None,
    new_files: list[dict[str, str]] | None = None,
) -> str:
    document: dict[str, object] = {"summary": summary}
    if edits is not None:
        document["edits"] = edits
    if new_files is not None:
        document["new_files"] = new_files
    return json.dumps(document)


class _RecordingConsult:
    """A stand-in for ``xai.consult`` that records whether it was called."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.called = False

    def __call__(self, prompt: str, **kwargs: object) -> str:
        self.called = True
        self.prompt = prompt
        return self.reply


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_propose_applies_patch_in_worktree_leaving_source_tree_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n", "README.md": "hi\n"})

    reply = _patch_json(
        summary="bump value and add a note",
        edits=[{"path": "src/app.py", "search": "value = 1", "replace": "value = 2"}],
        new_files=[{"path": "src/note.txt", "content": "generated\n"}],
    )
    fake = _RecordingConsult(reply)
    monkeypatch.setattr(patch_proposal.xai, "consult", fake)

    outcome = patch_proposal.propose_patch(
        "grok",
        "make it two",
        repo_root=tmp_path,
        allowed_footprint=["src"],
        project_context_text="",
    )

    # The engine WAS called, and the manifest reflects exactly the proposal.
    assert fake.called
    assert outcome.manifest.changed == ["src/app.py"]
    assert outcome.manifest.created == ["src/note.txt"]
    assert outcome.summary == "bump value and add a note"

    # The change landed in the worktree, NOT the source working tree.
    assert (outcome.worktree / "src/app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (outcome.worktree / "src/note.txt").read_text(encoding="utf-8") == "generated\n"
    assert (tmp_path / "src/app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not (tmp_path / "src/note.txt").exists()

    # Nothing was committed and the worktree is left in place for review.
    assert outcome.worktree.exists()
    assert _worktree_count(tmp_path) == 2

    # Cleanup so no worktree leaks past the test.
    patch_proposal.cleanup_worktree(tmp_path, outcome.worktree)
    assert _worktree_count(tmp_path) == 1
    assert not outcome.worktree.exists()


def test_suggested_commit_message_attributes_the_foreign_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="raise the value",
        edits=[{"path": "src/app.py", "search": "value = 1", "replace": "value = 9"}],
    )
    monkeypatch.setattr(patch_proposal.xai, "consult", _RecordingConsult(reply))

    outcome = patch_proposal.propose_patch(
        "grok",
        "task",
        repo_root=tmp_path,
        allowed_footprint=["src"],
        project_context_text="",
    )
    assert outcome.suggested_commit_message.startswith("raise the value")
    assert "Co-Authored-By: Grok (xAI) via Cohort <noreply@x.ai>" in outcome.suggested_commit_message

    patch_proposal.cleanup_worktree(tmp_path, outcome.worktree)


# --------------------------------------------------------------------------- #
# Fail-closed paths
# --------------------------------------------------------------------------- #


def test_egress_opt_out_blocks_before_the_engine_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    fake = _RecordingConsult(_patch_json())
    monkeypatch.setattr(patch_proposal.xai, "consult", fake)

    with pytest.raises(gates.EgressBlockedError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="cohort:egress=deny",
        )

    # The engine was never called, and no worktree was created (fail closed before I/O).
    assert not fake.called
    assert _worktree_count(tmp_path) == 1


def test_path_outside_footprint_is_rejected_and_worktree_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="sneak a file in",
        new_files=[{"path": "other/evil.py", "content": "print('x')\n"}],
    )
    monkeypatch.setattr(patch_proposal.xai, "consult", _RecordingConsult(reply))

    with pytest.raises(gates.PathViolationError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )

    # The worktree created for the call was cleaned up — no leak.
    assert _worktree_count(tmp_path) == 1
    # And the source tree never gained the file.
    assert not (tmp_path / "other/evil.py").exists()


def test_sensitive_override_cannot_launder_a_git_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `allowed_footprint=["."]` case below proves only the easy version: "." is not
    # sensitive, so it was never an override candidate. This is the real one -- a
    # footprint entry that DOES classify sensitive, in a different class to the path.
    _init_git_repo(tmp_path, {"src/auth/session.py": "value = 1\n"})
    reply = _patch_json(
        summary="tamper with git",
        new_files=[{"path": "src/auth/.git/config", "content": "[core]\n"}],
    )
    monkeypatch.setattr(patch_proposal.xai, "consult", _RecordingConsult(reply))

    with pytest.raises(gates.PathViolationError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src/auth/**"],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1


def test_no_worktree_is_created_when_the_engine_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worktree is created only once the reply is parsed and gated, so an engine
    # failure -- the longest window, and the one a Ctrl-C is most likely to land in --
    # cannot leak one at all.
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise patch_proposal.xai.EngineUnavailableError("upstream is down")

    monkeypatch.setattr(patch_proposal.xai, "consult", _boom)

    with pytest.raises(patch_proposal.xai.EngineUnavailableError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1


def test_worktree_is_cleaned_up_when_the_engine_call_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # KeyboardInterrupt is not an EngineError, so a narrow `except` would let it escape
    # with the worktree still registered -- polluting the user's repo until they run
    # `git worktree prune`.
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})

    def _interrupt(*_args: object, **_kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(patch_proposal.xai, "consult", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1


def test_worktree_is_cleaned_up_on_an_unexpected_apply_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An error type nobody anticipated must still clean up: the handler around
    # `apply_patch` is deliberately `except BaseException`, not `except PatchApplyError`.
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="edit the file",
        edits=[{"path": "src/app.py", "search": "value = 1", "replace": "value = 2"}],
    )
    monkeypatch.setattr(patch_proposal.xai, "consult", _RecordingConsult(reply))

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(patch_proposal.patch, "apply_patch", _unexpected)

    with pytest.raises(RuntimeError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1


def test_git_internal_path_is_rejected_even_within_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="tamper with git",
        new_files=[{"path": ".git/hooks/pre-commit", "content": "#!/bin/sh\n"}],
    )
    monkeypatch.setattr(patch_proposal.xai, "consult", _RecordingConsult(reply))

    with pytest.raises(gates.PathViolationError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            # even a permissive footprint cannot override a sensitive git-internal path
            allowed_footprint=["."],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1


def test_secret_in_proposed_content_is_rejected_and_worktree_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="add a secret",
        new_files=[{"path": "src/config.py", "content": "API_KEY = 'abcdef123456'\n"}],
    )
    monkeypatch.setattr(patch_proposal.xai, "consult", _RecordingConsult(reply))

    with pytest.raises(gates.SecretFoundError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1
    assert not (tmp_path / "src/config.py").exists()


def test_malformed_engine_reply_is_rejected_and_worktree_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    monkeypatch.setattr(
        patch_proposal.xai, "consult", _RecordingConsult("not a patch at all")
    )

    with pytest.raises(PatchParseError):
        patch_proposal.propose_patch(
            "grok",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )
    assert _worktree_count(tmp_path) == 1


def test_unknown_engine_raises_proposal_error(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    with pytest.raises(patch_proposal.ProposalError):
        patch_proposal.propose_patch(
            "not-a-real-engine",
            "task",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="",
        )


def test_empty_footprint_raises_before_any_engine_call_or_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # propose_patch enforces the non-empty-footprint invariant itself (not only the
    # CLI): a caller with no declared write scope is refused before the engine is
    # called and before any worktree is created.
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})

    def _fail_if_called(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("engine must not be called with an empty footprint")

    monkeypatch.setattr(patch_proposal.xai, "consult", _fail_if_called)

    for footprint in ([], ["", "   "]):
        with pytest.raises(patch_proposal.ProposalError):
            patch_proposal.propose_patch(
                "grok",
                "task",
                repo_root=tmp_path,
                allowed_footprint=footprint,
                project_context_text="",
            )
    # Only the main worktree exists — nothing was staged.
    assert _worktree_count(tmp_path) == 1


# --------------------------------------------------------------------------- #
# CLI usage errors
# --------------------------------------------------------------------------- #


def test_display_safe_escapes_terminal_control_sequences() -> None:
    from cohort.cli import _display_safe

    # An engine could embed an ANSI clear-screen / cursor sequence in its summary or a
    # proposed path; it must be neutralised (shown as a visible escape), never emitted
    # raw where it could rewrite the reviewer's terminal.
    assert "\x1b" not in _display_safe("done\x1b[2Jwiped")
    assert _display_safe("done\x1b[2Jwiped") == "done\\x1b[2Jwiped"
    # Ordinary text — including spaces and unicode — is left intact.
    assert _display_safe("Add rate limit to /api") == "Add rate limit to /api"
    assert _display_safe("café — updated") == "café — updated"
    # Other control characters are escaped too.
    assert "\r" not in _display_safe("a\rb")


def test_cli_propose_rejects_unknown_engine_with_exit_2() -> None:
    result = runner.invoke(app, ["engine", "propose", "not-a-real-engine"])
    assert result.exit_code == 2
    assert "unknown engine" in result.output


def test_cli_propose_requires_a_footprint_with_exit_2(tmp_path: Path) -> None:
    task_file = tmp_path / "task.txt"
    task_file.write_text("do something", encoding="utf-8")
    result = runner.invoke(
        app, ["engine", "propose", "grok", "--task-file", str(task_file)]
    )
    assert result.exit_code == 2
    assert "footprint" in result.output


# --- agentic proposal (explore then propose, through the same gates) ---------


def _agentic(text: str, stopped: str = "final") -> AgenticResult:
    return AgenticResult(text=text, transcript=[], iterations=2, stopped_reason=stopped)


def test_agentic_proposal_applies_in_worktree_leaving_source_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="bump the value",
        edits=[{"path": "src/app.py", "search": "value = 1", "replace": "value = 2"}],
    )
    monkeypatch.setattr(
        patch_proposal.xai_agentic, "run_agentic", lambda *a, **k: _agentic(reply)
    )

    outcome = patch_proposal.propose_patch_agentic(
        "grok", "bump the value", repo_root=tmp_path, allowed_footprint=["src"]
    )

    assert (outcome.worktree / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"  # untouched
    assert _worktree_count(tmp_path) == 2  # left in place for review


def test_agentic_proposal_honors_the_egress_optout_before_exploring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    explored = {"called": False}

    def _must_not_run(*_a, **_k):
        explored["called"] = True
        raise AssertionError("exploration must not start when egress is denied")

    monkeypatch.setattr(patch_proposal.xai_agentic, "run_agentic", _must_not_run)

    with pytest.raises(gates.EgressBlockedError):
        patch_proposal.propose_patch_agentic(
            "grok",
            "x",
            repo_root=tmp_path,
            allowed_footprint=["src"],
            project_context_text="## Egress\n\ncohort:egress=deny\n",
        )
    assert explored["called"] is False


def test_agentic_proposal_gate_rejects_out_of_footprint_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="sneak a file in",
        new_files=[{"path": "other/evil.py", "content": "print('x')\n"}],
    )
    monkeypatch.setattr(
        patch_proposal.xai_agentic, "run_agentic", lambda *a, **k: _agentic(reply)
    )

    with pytest.raises(gates.PathViolationError):
        patch_proposal.propose_patch_agentic(
            "grok", "x", repo_root=tmp_path, allowed_footprint=["src"]
        )
    assert _worktree_count(tmp_path) == 1  # no leaked worktree
    assert not (tmp_path / "other" / "evil.py").exists()


def test_agentic_proposal_raises_when_loop_ends_without_a_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    monkeypatch.setattr(
        patch_proposal.xai_agentic,
        "run_agentic",
        lambda *a, **k: _agentic("(stopped: ...)", stopped="max_iterations"),
    )
    with pytest.raises(patch_proposal.ProposalError, match="did not produce a patch"):
        patch_proposal.propose_patch_agentic(
            "grok", "x", repo_root=tmp_path, allowed_footprint=["src"]
        )


def test_agentic_proposal_rejects_an_empty_footprint(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    with pytest.raises(patch_proposal.ProposalError):
        patch_proposal.propose_patch_agentic(
            "grok", "x", repo_root=tmp_path, allowed_footprint=[]
        )


def test_cli_propose_agentic_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercises the CLI --agentic wiring (flag → transcript path → propose_patch_agentic).
    _init_git_repo(tmp_path, {"src/app.py": "value = 1\n"})
    reply = _patch_json(
        summary="bump",
        edits=[{"path": "src/app.py", "search": "value = 1", "replace": "value = 2"}],
    )
    monkeypatch.setattr(
        patch_proposal.xai_agentic, "run_agentic", lambda *a, **k: _agentic(reply)
    )
    monkeypatch.setattr("cohort.cli.find_repo_root", lambda _cwd: tmp_path)
    task_file = tmp_path / "task.txt"
    task_file.write_text("bump the value", encoding="utf-8")

    result = runner.invoke(
        app,
        ["engine", "propose", "grok", "--agentic", "--footprint", "src",
         "--task-file", str(task_file)],
    )

    assert result.exit_code == 0, result.output
    assert "transcript" in result.output  # the audit path is surfaced
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"

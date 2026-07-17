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


# --------------------------------------------------------------------------- #
# CLI usage errors
# --------------------------------------------------------------------------- #


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

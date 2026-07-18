"""Tests for the structured-patch parser and applier (:mod:`cohort.engines.patch`).

These tests are the point of the module: an engine returns *text*, Cohort parses and
applies it, and a malicious or malformed proposal must never write outside the
caller-supplied worktree. Every test uses ``tmp_path`` as the root — there is no
network and no mutation of any real repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cohort.engines.patch import (
    Edit,
    NewFile,
    PatchApplyError,
    PatchParseError,
    PatchProposal,
    PatchResult,
    apply_patch,
    parse_patch,
)

from conftest import requires_symlinks


def _patch_json(
    *,
    summary: str = "a change",
    edits: list[dict[str, str]] | None = None,
    new_files: list[dict[str, str]] | None = None,
) -> str:
    """Serialize a well-formed patch document to JSON text."""
    document: dict[str, object] = {"summary": summary}
    if edits is not None:
        document["edits"] = edits
    if new_files is not None:
        document["new_files"] = new_files
    return json.dumps(document)


# --------------------------------------------------------------------------- parse


def test_parse_reads_plain_json_object() -> None:
    text = _patch_json(
        edits=[{"path": "a.py", "search": "old", "replace": "new"}],
        new_files=[{"path": "b.py", "content": "print()\n"}],
    )
    proposal = parse_patch(text)

    assert proposal.summary == "a change"
    assert proposal.edits == (Edit("a.py", "old", "new"),)
    assert proposal.new_files == (NewFile("b.py", "print()\n"),)


def test_parse_tolerates_json_fence_and_prose() -> None:
    inner = _patch_json(edits=[{"path": "a.py", "search": "x", "replace": "y"}])
    text = f"Here is the patch you asked for:\n```json\n{inner}\n```\nHope that helps!"

    proposal = parse_patch(text)

    assert proposal.summary == "a change"
    assert proposal.edits[0].path == "a.py"


def test_parse_defaults_edits_and_new_files_to_empty() -> None:
    proposal = parse_patch(json.dumps({"summary": "noop"}))

    assert proposal.summary == "noop"
    assert proposal.edits == ()
    assert proposal.new_files == ()


def test_parse_treats_braces_and_fence_markers_in_content_as_literal() -> None:
    # A file body full of braces and ``` markers must not confuse extraction; the
    # balanced-brace scanner respects JSON strings.
    body = "def f():\n    return {'k': {'nested': 1}}\n# ```json trap```\n"
    text = _patch_json(new_files=[{"path": "trap.py", "content": body}])

    proposal = parse_patch(text)

    assert proposal.new_files == (NewFile("trap.py", body),)


def test_parse_rejects_non_json() -> None:
    with pytest.raises(PatchParseError):
        parse_patch("this is just prose, no object here")


def test_parse_rejects_truncated_json() -> None:
    with pytest.raises(PatchParseError):
        parse_patch('{"summary": "x", "edits": [')


def test_parse_rejects_invalid_json_after_extraction() -> None:
    with pytest.raises(PatchParseError):
        parse_patch('{"summary": "x", bad}')


def test_parse_rejects_top_level_array() -> None:
    with pytest.raises(PatchParseError):
        parse_patch("[1, 2, 3]")


def test_parse_rejects_missing_summary() -> None:
    with pytest.raises(PatchParseError):
        parse_patch(json.dumps({"edits": []}))


def test_parse_rejects_non_string_summary() -> None:
    with pytest.raises(PatchParseError):
        parse_patch(json.dumps({"summary": 42}))


def test_parse_rejects_edits_not_a_list() -> None:
    with pytest.raises(PatchParseError):
        parse_patch(json.dumps({"summary": "x", "edits": {"path": "a"}}))


def test_parse_rejects_edit_with_missing_field() -> None:
    with pytest.raises(PatchParseError):
        parse_patch(_patch_json(edits=[{"path": "a.py", "search": "x"}]))


def test_parse_rejects_edit_with_wrong_type() -> None:
    with pytest.raises(PatchParseError):
        parse_patch(
            json.dumps(
                {"summary": "x", "edits": [{"path": "a", "search": 1, "replace": "y"}]}
            )
        )


def test_parse_rejects_new_file_with_wrong_type() -> None:
    with pytest.raises(PatchParseError):
        parse_patch(
            json.dumps(
                {"summary": "x", "new_files": [{"path": "a", "content": ["not str"]}]}
            )
        )


def test_parse_error_message_does_not_echo_payload() -> None:
    secret_marker = "SUPER_SECRET_SOURCE_LINE_12345"
    text = json.dumps({"summary": 0, "note": secret_marker})

    with pytest.raises(PatchParseError) as excinfo:
        parse_patch(text)

    assert secret_marker not in str(excinfo.value)


# --------------------------------------------------------------------------- apply


def test_apply_empty_proposal_writes_nothing(tmp_path: Path) -> None:
    result = apply_patch(PatchProposal(summary="noop"), tmp_path)

    assert result == PatchResult(changed=[], created=[])
    assert list(tmp_path.iterdir()) == []


def test_apply_single_edit_replaces_the_match(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello world\n", encoding="utf-8")
    proposal = PatchProposal(summary="s", edits=(Edit("a.txt", "world", "there"),))

    result = apply_patch(proposal, tmp_path)

    assert target.read_text(encoding="utf-8") == "hello there\n"
    assert result.changed == ["a.txt"]
    assert result.created == []


def test_apply_creates_new_file_and_parent_dirs(tmp_path: Path) -> None:
    proposal = PatchProposal(
        summary="s", new_files=(NewFile("pkg/sub/mod.py", "X = 1\n"),)
    )

    result = apply_patch(proposal, tmp_path)

    created = tmp_path / "pkg" / "sub" / "mod.py"
    assert created.read_text(encoding="utf-8") == "X = 1\n"
    assert result.created == ["pkg/sub/mod.py"]
    assert result.changed == []


def test_apply_edit_and_new_file_together(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    proposal = PatchProposal(
        summary="s",
        edits=(Edit("a.txt", "v1", "v2"),),
        new_files=(NewFile("b.txt", "new"),),
    )

    result = apply_patch(proposal, tmp_path)

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "new"
    assert result.changed == ["a.txt"]
    assert result.created == ["b.txt"]


def test_edit_missing_target_fails(tmp_path: Path) -> None:
    proposal = PatchProposal(summary="s", edits=(Edit("nope.txt", "a", "b"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)


def test_edit_not_found_fails_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    proposal = PatchProposal(summary="s", edits=(Edit("a.txt", "absent", "x"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)
    assert target.read_text(encoding="utf-8") == "hello"


def test_edit_ambiguous_match_fails(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x and x again", encoding="utf-8")
    proposal = PatchProposal(summary="s", edits=(Edit("a.txt", "x", "y"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)
    assert target.read_text(encoding="utf-8") == "x and x again"


def test_apply_is_all_or_nothing_on_one_bad_edit(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    bad = tmp_path / "bad.txt"
    good.write_text("keep me original", encoding="utf-8")
    bad.write_text("no match here", encoding="utf-8")
    proposal = PatchProposal(
        summary="s",
        edits=(
            Edit("good.txt", "original", "changed"),
            Edit("bad.txt", "ABSENT", "x"),
        ),
    )

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)

    # The good edit must NOT have landed — all-or-nothing.
    assert good.read_text(encoding="utf-8") == "keep me original"
    assert bad.read_text(encoding="utf-8") == "no match here"


def test_apply_is_all_or_nothing_when_new_file_collides(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text("original", encoding="utf-8")
    (tmp_path / "exists.txt").write_text("do not touch", encoding="utf-8")
    proposal = PatchProposal(
        summary="s",
        edits=(Edit("good.txt", "original", "changed"),),
        new_files=(NewFile("exists.txt", "clobber"),),
    )

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)

    assert good.read_text(encoding="utf-8") == "original"
    assert (tmp_path / "exists.txt").read_text(encoding="utf-8") == "do not touch"


def test_new_file_already_exists_fails(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("here", encoding="utf-8")
    proposal = PatchProposal(summary="s", new_files=(NewFile("a.txt", "x"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "here"


def test_duplicate_new_file_in_proposal_fails(tmp_path: Path) -> None:
    proposal = PatchProposal(
        summary="s",
        new_files=(NewFile("dup.txt", "one"), NewFile("dup.txt", "two")),
    )

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)
    assert not (tmp_path / "dup.txt").exists()


@pytest.mark.parametrize("bad_path", ["../escape.txt", "a/../../b.txt", "sub/../../x"])
def test_apply_rejects_path_traversal(tmp_path: Path, bad_path: str) -> None:
    proposal = PatchProposal(summary="s", new_files=(NewFile(bad_path, "x"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)
    # Nothing outside the worktree should ever be created.
    assert not (tmp_path.parent / "escape.txt").exists()


@requires_symlinks
def test_apply_rejects_new_file_through_symlink_redirect_inside_worktree(
    tmp_path: Path,
) -> None:
    # Containment-in-worktree is not enough. The scope gate classifies the *lexical*
    # path, so a committed symlink lets an in-footprint docs path redirect a write to a
    # sensitive location that is still inside the worktree -- and the manifest would
    # name the lexical path, showing the reviewer a file that is not the one on disk.
    root = tmp_path / "worktree"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "ci").symlink_to(
        root / ".github" / "workflows", target_is_directory=True
    )

    proposal = PatchProposal(
        summary="add docs note",
        new_files=(NewFile("docs/ci/release.yml", "malicious: workflow"),),
    )

    with pytest.raises(PatchApplyError, match="symlink"):
        apply_patch(proposal, root)
    assert not (root / ".github" / "workflows" / "release.yml").exists()


@requires_symlinks
def test_apply_rejects_edit_through_symlink_redirect_inside_worktree(
    tmp_path: Path,
) -> None:
    # The edit variant needs no exotic layout: `docs/README.md -> ../README.md` is a
    # common repo pattern and would let an in-footprint edit rewrite a file outside it.
    root = tmp_path / "worktree"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("original", encoding="utf-8")
    (root / "docs" / "README.md").symlink_to(root / "README.md")

    proposal = PatchProposal(
        summary="s", edits=(Edit("docs/README.md", "original", "rewritten"),)
    )

    with pytest.raises(PatchApplyError, match="symlink"):
        apply_patch(proposal, root)
    assert (root / "README.md").read_text(encoding="utf-8") == "original"


def test_apply_writes_lf_line_endings_regardless_of_platform(tmp_path: Path) -> None:
    # Asserted at the BYTE level on purpose: `read_text` translates CRLF back to "\n",
    # so a text-level assertion cannot detect the newline translation `write_text`
    # performs by default -- which on Windows rewrites every touched file in CRLF and
    # breaks this repo's `eol=lf` byte-stability invariant.
    (tmp_path / "existing.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    proposal = PatchProposal(
        summary="s",
        edits=(Edit("existing.py", "a = 1", "a = 99"),),
        new_files=(NewFile("created.py", "x = 1\ny = 2\n"),),
    )

    apply_patch(proposal, tmp_path)

    assert b"\r" not in (tmp_path / "existing.py").read_bytes()
    assert b"\r" not in (tmp_path / "created.py").read_bytes()


def test_edit_target_with_invalid_utf8_fails_as_patch_apply_error(
    tmp_path: Path,
) -> None:
    # A binary edit target raises UnicodeDecodeError -- a ValueError, not a PatchError.
    # Uncaught it escapes the caller's handler and leaks the proposal's worktree.
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe binary")
    proposal = PatchProposal(summary="s", edits=(Edit("image.png", "PNG", "GIF"),))

    with pytest.raises(PatchApplyError, match="UTF-8"):
        apply_patch(proposal, tmp_path)


@pytest.mark.parametrize("bad_path", ["a\\b.py", "C:\\Windows\\x", "sub\\..\\..\\x"])
def test_apply_rejects_backslash_paths(tmp_path: Path, bad_path: str) -> None:
    # `gates` folds "\" to "/" before classifying but `Path` on posix does not, so
    # `a\b.py` gated as `a/b.py` and was written as one oddly-named file at the root.
    proposal = PatchProposal(summary="s", new_files=(NewFile(bad_path, "x"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_apply_rejects_absolute_path(tmp_path: Path) -> None:
    proposal = PatchProposal(
        summary="s", new_files=(NewFile("/etc/passwd_cohort", "x"),)
    )

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)


def test_apply_rejects_empty_path(tmp_path: Path) -> None:
    proposal = PatchProposal(summary="s", new_files=(NewFile("", "x"),))

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, tmp_path)


@requires_symlinks
def test_apply_rejects_new_file_through_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    proposal = PatchProposal(
        summary="s", new_files=(NewFile("link/evil.py", "pwned"),)
    )

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, root)
    # The symlinked-to directory outside the worktree must remain empty.
    assert not (outside / "evil.py").exists()


@requires_symlinks
def test_apply_rejects_edit_through_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    proposal = PatchProposal(
        summary="s", edits=(Edit("link/secret.txt", "classified", "leaked"),)
    )

    with pytest.raises(PatchApplyError):
        apply_patch(proposal, root)
    assert secret.read_text(encoding="utf-8") == "classified"


def test_apply_treats_search_and_replace_as_literal_not_regex(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    # Regex-special characters that would misbehave if interpreted as a pattern.
    target.write_text("value = a.*(b)+ ok", encoding="utf-8")
    proposal = PatchProposal(
        summary="s", edits=(Edit("a.txt", "a.*(b)+", "PLAIN"),)
    )

    apply_patch(proposal, tmp_path)

    assert target.read_text(encoding="utf-8") == "value = PLAIN ok"


def test_apply_multiple_edits_same_file_apply_in_order(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one two", encoding="utf-8")
    proposal = PatchProposal(
        summary="s",
        edits=(Edit("a.txt", "one", "1"), Edit("a.txt", "two", "2")),
    )

    result = apply_patch(proposal, tmp_path)

    assert target.read_text(encoding="utf-8") == "1 2"
    # A file touched by two edits appears once in the manifest.
    assert result.changed == ["a.txt"]


def test_apply_manifest_lists_changed_and_created(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_text("edit me", encoding="utf-8")
    proposal = PatchProposal(
        summary="s",
        edits=(Edit("e.txt", "edit me", "edited"),),
        new_files=(NewFile("n1.txt", "a"), NewFile("n2.txt", "b")),
    )

    result = apply_patch(proposal, tmp_path)

    assert result.changed == ["e.txt"]
    assert result.created == ["n1.txt", "n2.txt"]

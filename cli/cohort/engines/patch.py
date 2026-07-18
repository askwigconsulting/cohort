"""Parse and apply the structured patch an engine returns for the ``patch_proposal`` role.

RFC 0004's design cross-examination rejected model-authored *unified diffs* — they
fail to apply because the model guesses line numbers and drifts context. Instead the
engine (e.g. Grok) is prompted to return a JSON document of **exact** edits, and
*Cohort* — never the engine — parses and applies it. The engine only produces text.

The locked wire contract (an engine emits exactly this, optionally wrapped in a
```json`` fence and/or surrounding prose):

.. code-block:: json

    {
      "summary": "one-line description of the change",
      "edits": [
        {"path": "relative/path.py", "search": "<exact existing substring>",
         "replace": "<new text>"}
      ],
      "new_files": [
        {"path": "relative/new.py", "content": "<full file content>"}
      ]
    }

``edits`` and ``new_files`` may each be empty or absent. All ``search``/``replace``/
``content`` values are treated as **literal text** — never as regex or diff syntax.

Security invariant (non-negotiable): :func:`apply_patch` writes only inside the
caller-supplied ``root`` worktree. Every target path must be relative, free of ``..``,
and must *resolve* — following symlinks — to a location inside ``root``. Application is
all-or-nothing: every check runs before any byte is written, so a proposal that is
partly invalid leaves the filesystem untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# `Path("C:/x").is_absolute()` is False on posix, so a drive-qualified path would read
# as relative here. Mirrors the same check in `cohort.engines.gates`.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PatchError(Exception):
    """Base class for every patch parse or apply failure."""


class PatchParseError(PatchError):
    """The engine text is not a valid patch document (bad JSON, shape, or types).

    Messages are deliberately short and never echo the full payload — a proposal can
    be large and may quote source that should not be duplicated into logs.
    """


class PatchApplyError(PatchError):
    """A validated patch could not be applied safely; the filesystem is unchanged.

    Raised for path-safety violations, a missing edit target, a ``search`` that
    matches zero or several times, or a ``new_files`` collision. Because all checks
    run before any write, raising this leaves ``root`` exactly as it was found.
    """


@dataclass(frozen=True)
class Edit:
    """One exact-substring replacement within an existing file under the worktree.

    Attributes:
        path: Worktree-relative path to the file to edit.
        search: The exact substring to find; must occur exactly once in the file.
        replace: The literal text to substitute for that single occurrence.
    """

    path: str
    search: str
    replace: str


@dataclass(frozen=True)
class NewFile:
    """A file to create under the worktree.

    Attributes:
        path: Worktree-relative path that must not already exist.
        content: The full literal file content to write.
    """

    path: str
    content: str


@dataclass(frozen=True)
class PatchProposal:
    """A parsed, structurally-valid patch document (not yet applied).

    Attributes:
        summary: One-line human description of the change.
        edits: Exact-substring edits to apply, in order.
        new_files: Files to create.
    """

    summary: str
    edits: tuple[Edit, ...] = ()
    new_files: tuple[NewFile, ...] = ()


@dataclass(frozen=True)
class PatchResult:
    """Manifest of what :func:`apply_patch` changed.

    Attributes:
        changed: Worktree-relative paths of files modified by an edit (deduplicated,
            in first-touched order).
        created: Worktree-relative paths of files created from ``new_files``.
    """

    changed: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


def _find_json_object(text: str) -> str:
    """Return the first balanced ``{...}`` object embedded in ``text``.

    Scans from the first ``{`` and tracks brace depth while respecting JSON string
    literals, so braces (or ```` ``` ```` fence markers) *inside* a string value do not
    unbalance the count. This lets us pull the object out of a ```json`` fence or
    surrounding prose without a fragile fence regex.

    Raises:
        PatchParseError: if there is no ``{``, or the object never closes (truncated).
    """
    start = text.find("{")
    if start == -1:
        raise PatchParseError("no JSON object found in engine output")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise PatchParseError("JSON object in engine output is truncated")


def _require_str(container: dict[str, Any], key: str, where: str) -> str:
    """Return ``container[key]`` when it is a string, else raise a short parse error."""
    value = container.get(key)
    if not isinstance(value, str):
        raise PatchParseError(f"{where} field {key!r} must be a string")
    return value


def _parse_edits(raw: Any) -> tuple[Edit, ...]:
    """Validate and convert the ``edits`` array into :class:`Edit` objects."""
    if not isinstance(raw, list):
        raise PatchParseError("'edits' must be a JSON array")
    edits: list[Edit] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PatchParseError(f"edit {position} must be a JSON object")
        where = f"edit {position}"
        edits.append(
            Edit(
                path=_require_str(item, "path", where),
                search=_require_str(item, "search", where),
                replace=_require_str(item, "replace", where),
            )
        )
    return tuple(edits)


def _parse_new_files(raw: Any) -> tuple[NewFile, ...]:
    """Validate and convert the ``new_files`` array into :class:`NewFile` objects."""
    if not isinstance(raw, list):
        raise PatchParseError("'new_files' must be a JSON array")
    new_files: list[NewFile] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PatchParseError(f"new_file {position} must be a JSON object")
        where = f"new_file {position}"
        new_files.append(
            NewFile(
                path=_require_str(item, "path", where),
                content=_require_str(item, "content", where),
            )
        )
    return tuple(new_files)


def parse_patch(text: str) -> PatchProposal:
    """Parse engine ``text`` into a validated :class:`PatchProposal`.

    Tolerates a ```json`` fence and surrounding prose by extracting the first balanced
    JSON object. Validates the document shape and the type of every field; ``edits``
    and ``new_files`` are optional and default to empty.

    Args:
        text: The raw assistant text returned by the engine.

    Returns:
        The parsed proposal (not yet checked for path safety — see :func:`apply_patch`).

    Raises:
        PatchParseError: if no JSON object can be extracted, the JSON is invalid, the
            top level is not an object, a required key is missing, or any field has the
            wrong type. The message is short and never echoes the full payload.
    """
    snippet = _find_json_object(text)
    try:
        parsed = json.loads(snippet)
    except ValueError:
        raise PatchParseError("engine output is not valid JSON") from None

    if not isinstance(parsed, dict):
        raise PatchParseError("patch must be a JSON object")

    if "summary" not in parsed:
        raise PatchParseError("patch is missing required key 'summary'")
    summary = _require_str(parsed, "summary", "patch")

    edits = _parse_edits(parsed["edits"]) if "edits" in parsed else ()
    new_files = _parse_new_files(parsed["new_files"]) if "new_files" in parsed else ()

    return PatchProposal(summary=summary, edits=edits, new_files=new_files)


def _resolve_within(root: Path, root_resolved: Path, rel_path: str) -> Path:
    """Return the absolute target for ``rel_path`` inside ``root``, or reject it.

    The path must be non-empty, relative, free of ``..`` components and backslashes,
    and must not traverse a symlink at any depth; after joining onto ``root`` it must
    land strictly inside ``root_resolved``. This is the write-containment security
    boundary.

    Symlink traversal is refused outright rather than merely contained. Containment
    alone is not enough: the scope gate in :mod:`cohort.engines.gates` classifies the
    *lexical* path, so a committed symlink (``docs/ci -> ../.github/workflows``) would
    let an in-footprint path redirect a write to a sensitive location that is still
    inside the worktree — and the reported manifest would name the lexical path,
    showing the human reviewer a file that is not the one on disk. A machine-generated
    patch into a throwaway worktree has no legitimate need to write through a symlink.

    Raises:
        PatchApplyError: for an empty, absolute, ``..``-bearing, backslash-bearing,
            symlink-traversing, or escaping path.
    """
    if not rel_path:
        raise PatchApplyError("patch path is empty")

    # `gates` folds backslashes to `/` before classifying; `Path` on posix does not,
    # so `a\b.py` would gate as `a/b.py` and be written as a single oddly-named file
    # at the worktree root. Refusing the character keeps both modules on one grammar.
    if "\\" in rel_path:
        raise PatchApplyError(f"patch path must not contain a backslash: {rel_path!r}")

    candidate = Path(rel_path)
    if candidate.is_absolute() or _WINDOWS_DRIVE_RE.match(rel_path) is not None:
        raise PatchApplyError(f"patch path must be relative: {rel_path!r}")
    if ".." in candidate.parts:
        raise PatchApplyError(f"patch path must not contain '..': {rel_path!r}")

    walked = root_resolved
    for part in candidate.parts:
        walked = walked / part
        if walked.is_symlink():
            raise PatchApplyError(
                f"patch path traverses a symlink: {rel_path!r} (at {part!r})"
            )

    resolved = (root / candidate).resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise PatchApplyError(f"patch path escapes the worktree: {rel_path!r}")
    return resolved


def apply_patch(proposal: PatchProposal, root: Path) -> PatchResult:
    """Apply ``proposal`` inside the ``root`` worktree, all-or-nothing.

    Every check — path safety, edit-target existence, exactly-once ``search`` matches,
    and ``new_files`` non-collision — runs *before* any write. If any check fails the
    function raises and the filesystem is left unchanged.

    Note the guarantee is "all checks precede all writes", not filesystem atomicity: an
    ``OSError`` partway through the write phase (ENOSPC, a read-only mount) leaves the
    earlier writes in place. That is acceptable only because ``root`` is a throwaway
    worktree the caller discards on failure — do not rely on this for in-place edits.

    Edits to the same file are applied in order against a running in-memory copy, so a
    later edit's exactly-once requirement is measured against the text left by earlier
    edits.

    Args:
        proposal: The parsed patch to apply.
        root: The worktree directory to write into. Must already exist.

    Returns:
        A :class:`PatchResult` listing the changed and created worktree-relative paths.

    Raises:
        PatchApplyError: on any path-safety violation, a missing edit target, a
            ``search`` that matches zero or multiple times, or a ``new_files``
            collision. On failure nothing is written.
    """
    root_resolved = root.resolve()

    # --- Phase 1: validate everything and stage the writes. Write nothing here. ---
    edit_targets: dict[Path, str] = {}  # absolute path -> current working text
    changed: list[str] = []  # relative paths, first-touched order
    for edit in proposal.edits:
        target = _resolve_within(root, root_resolved, edit.path)
        if target not in edit_targets:
            if not target.is_file():
                raise PatchApplyError(f"edit target does not exist: {edit.path!r}")
            try:
                edit_targets[target] = target.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                # A binary or non-UTF-8 edit target raises UnicodeDecodeError, which is
                # a ValueError — not a PatchError. Uncaught, it escapes both the caller's
                # handler and the worktree cleanup in `patch_proposal`, leaking a
                # registered worktree and surfacing a raw traceback.
                raise PatchApplyError(
                    f"edit target is not readable as UTF-8 text: {edit.path!r}"
                ) from exc
            changed.append(Path(edit.path).as_posix())

        working = edit_targets[target]
        occurrences = working.count(edit.search)
        if occurrences == 0:
            raise PatchApplyError(f"edit 'search' not found in {edit.path!r}")
        if occurrences > 1:
            raise PatchApplyError(
                f"edit 'search' is ambiguous in {edit.path!r} "
                f"({occurrences} occurrences)"
            )
        edit_targets[target] = working.replace(edit.search, edit.replace, 1)

    new_targets: dict[Path, str] = {}  # absolute path -> content
    created: list[str] = []
    for new_file in proposal.new_files:
        target = _resolve_within(root, root_resolved, new_file.path)
        if target in new_targets:
            raise PatchApplyError(
                f"duplicate new file in proposal: {new_file.path!r}"
            )
        if target.exists():
            raise PatchApplyError(f"new file already exists: {new_file.path!r}")
        new_targets[target] = new_file.content
        created.append(Path(new_file.path).as_posix())

    # --- Phase 2: all checks passed — commit the writes. ---
    # `newline=""` disables newline translation. Without it `write_text` rewrites every
    # "\n" as os.linesep, so on Windows every touched file lands entirely in CRLF —
    # violating this repo's `.gitattributes` `eol=lf` invariant and producing
    # whole-file diffs that break the byte-stable golden/parity tests.
    try:
        for target, content in edit_targets.items():
            target.write_text(content, encoding="utf-8", newline="")
        for target, content in new_targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise PatchApplyError(f"could not write the patch: {exc}") from exc

    return PatchResult(changed=changed, created=created)

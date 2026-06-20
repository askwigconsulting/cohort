"""Canonical artifact loader: split YAML frontmatter from the freeform body.

A canonical artifact is a single ``.md`` file::

    ---
    <frontmatter: YAML mapping, schema-validated>
    ---
    <body: freeform markdown, never validated>

Only the frontmatter is validated. The body is returned verbatim and may itself
contain ``---`` lines (the closing delimiter is the *first* ``---`` after the
opening one).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from .errors import ArtifactError, E001_FRONTMATTER_PARSE


class FrontmatterError(Exception):
    """Raised when frontmatter cannot be split or parsed into a mapping."""


def split_frontmatter(raw: str) -> tuple[str, str]:
    """Split ``raw`` file text into ``(frontmatter_text, body_text)``.

    Tolerates a leading UTF-8 BOM and CRLF / CR line endings. The body is the
    text after the second ``---`` delimiter and is ``""`` when the file is
    frontmatter-only.

    Raises:
        FrontmatterError: if the opening or closing ``---`` delimiter is absent.
    """
    text = raw.lstrip("﻿")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("missing opening '---' frontmatter delimiter")

    close_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        raise FrontmatterError("missing closing '---' frontmatter delimiter")

    frontmatter_text = "\n".join(lines[1:close_idx])
    body = "\n".join(lines[close_idx + 1 :])
    return frontmatter_text, body


def parse_frontmatter(frontmatter_text: str) -> dict[str, Any]:
    """Parse frontmatter YAML and require it to be a mapping.

    Raises:
        FrontmatterError: on invalid YAML or non-mapping (list, scalar, empty).
    """
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:  # noqa: BLE001 - re-wrapped intentionally
        raise FrontmatterError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a mapping")
    return data


@dataclass(frozen=True)
class LoadResult:
    """Outcome of loading one artifact file.

    On success ``frontmatter`` is the parsed mapping and ``load_error`` is None.
    On a parse/split failure ``frontmatter``/``body`` are None and ``load_error``
    carries the E001 error (a hard stop — no further validation runs).
    """

    path: Path
    name_stem: str
    frontmatter: Optional[dict[str, Any]]
    body: Optional[str]
    load_error: Optional[ArtifactError]


def load_artifact(path: Path | str) -> LoadResult:
    """Load and split one artifact file by path.

    Never raises for content problems: a malformed file yields a ``LoadResult``
    whose ``load_error`` is an E001 ``ArtifactError``.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    return load_artifact_text(raw, name_stem=path.stem, path=path)


def load_artifact_text(
    raw: str, name_stem: str, path: Path | str = "<memory>"
) -> LoadResult:
    """Load and split artifact text (no filesystem read); used by unit tests."""
    p = Path(path)
    try:
        frontmatter_text, body = split_frontmatter(raw)
        frontmatter = parse_frontmatter(frontmatter_text)
    except FrontmatterError as exc:
        return LoadResult(
            path=p,
            name_stem=name_stem,
            frontmatter=None,
            body=None,
            load_error=ArtifactError(E001_FRONTMATTER_PARSE, field=None, message=str(exc)),
        )
    return LoadResult(
        path=p,
        name_stem=name_stem,
        frontmatter=frontmatter,
        body=body,
        load_error=None,
    )

"""The renderer descriptor the compile pipeline drives off (P7-R1).

Each per-IDE renderer declares its dest root and merge targets and implements
``compile(irs) -> (staged_files, skipped_names)``. The pipeline (compile + ops)
is generic over this descriptor, so adding Codex/Cursor is "one more renderer"
rather than another ``if ide == "claude"`` branch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MergeTarget:
    """A staged payload that merges into a (possibly user-owned) dest file.

    ``payload_rel`` is under the staging ``.merge/`` subdir; ``dest_name`` is the
    file under the renderer's dest root; ``strategy`` is ``block`` (comment-
    bearing text: markdown/TOML/YAML) or ``json`` (key-merge — JSON only).
    """

    payload_rel: str
    dest_name: str
    strategy: str

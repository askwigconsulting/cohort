"""Data-safe merge primitives for files Cohort shares with the user.

Two strategies, both reversible with ownership re-verification:

- **managed-block** (text, e.g. ``CLAUDE.md``): Cohort owns a delimited region;
  everything outside is the user's and is never touched. Reverse removes the
  block only if its content still hashes to what Cohort recorded.
- **key-merge** (JSON, e.g. ``settings.json``): Cohort *appends* its hook
  entries; user keys/entries are preserved (semantic preservation, not byte —
  formatting normalizes). Reverse removes only entries whose hash matches a
  recorded tag.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Optional

BLOCK_BEGIN = "<!-- >>> cohort (managed) — do not edit inside this block >>> -->"
BLOCK_END = "<!-- <<< cohort (managed) <<< -->"

_BLOCK_RE = re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END), re.DOTALL)


# --- managed-block (text) ---------------------------------------------------


def _render_block(inner: str) -> str:
    return f"{BLOCK_BEGIN}\n{inner.strip(chr(10))}\n{BLOCK_END}"


def extract_block(text: str) -> Optional[str]:
    """Return the inner content of Cohort's managed block, or None if absent."""
    m = _BLOCK_RE.search(text)
    if m is None:
        return None
    full = m.group(0)
    inner = full[len(BLOCK_BEGIN) : len(full) - len(BLOCK_END)]
    return inner.strip("\n")


def upsert_block(text: str, inner: str) -> str:
    """Insert or replace Cohort's managed block with ``inner``; return new text."""
    block = _render_block(inner)
    if _BLOCK_RE.search(text) is not None:
        return _BLOCK_RE.sub(lambda _m: block, text, count=1)
    if text.strip() == "":
        return block + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


def remove_block(text: str) -> str:
    """Remove Cohort's managed block, collapsing leftover blank lines."""
    new = _BLOCK_RE.sub("", text, count=1)
    new = re.sub(r"\n{3,}", "\n\n", new).strip("\n")
    return new + "\n" if new else ""


def block_hash(inner: str) -> str:
    return hashlib.sha256(inner.strip("\n").encode("utf-8")).hexdigest()


# --- key-merge (JSON) -------------------------------------------------------


def entry_hash(entry: Any) -> str:
    """Stable hash of a JSON-serializable hook entry (order-insensitive)."""
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def merge_hooks(existing: dict, fragment: dict) -> tuple[dict, list[dict]]:
    """Append Cohort's hook entries into ``existing`` settings.

    ``fragment`` is ``{"hooks": {<Event>: [entry, ...]}}``. Returns the new
    settings object and the tags (``[{event, entry_hash}]``) Cohort added.
    Entries already present (by hash) are not re-added (idempotent). User keys
    and entries are preserved; Cohort only appends.
    """
    new = copy.deepcopy(existing)
    hooks = new.setdefault("hooks", {})
    added: list[dict] = []
    for event, entries in fragment.get("hooks", {}).items():
        arr = hooks.setdefault(event, [])
        present = {entry_hash(e) for e in arr}
        for entry in entries:
            h = entry_hash(entry)
            if h in present:
                continue
            arr.append(copy.deepcopy(entry))
            present.add(h)
            added.append({"event": event, "entry_hash": h})
    return new, added


def remove_tagged(existing: dict, tags: list[dict]) -> tuple[dict, int, int]:
    """Remove entries matching recorded tags; return (new, removed, skipped).

    An entry whose hash no longer matches (user-altered) or is gone is left
    untouched and counted as ``skipped`` (ownership re-verify).
    """
    new = copy.deepcopy(existing)
    hooks = new.get("hooks", {})
    removed = skipped = 0
    for tag in tags:
        arr = hooks.get(tag["event"], [])
        idx = next((i for i, e in enumerate(arr) if entry_hash(e) == tag["entry_hash"]), None)
        if idx is None:
            skipped += 1
        else:
            arr.pop(idx)
            removed += 1
    for event in list(hooks.keys()):
        if not hooks[event]:
            del hooks[event]
    if "hooks" in new and not new["hooks"]:
        del new["hooks"]
    return new, removed, skipped


def dumps_json(obj: dict) -> str:
    """Deterministic JSON serialization for settings files (insertion order)."""
    return json.dumps(obj, indent=2) + "\n"

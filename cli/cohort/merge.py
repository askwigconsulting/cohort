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

# TODO(review): GK-F1 nonce — a per-install nonce inside the sentinel would let
# concurrent installs disambiguate ownership without a content hash. Out of scope
# for this pass (it changes the on-disk output format); tracked for a later review.
BLOCK_BEGIN = "<!-- >>> cohort (managed) — do not edit inside this block >>> -->"
BLOCK_END = "<!-- <<< cohort (managed) <<< -->"

_BLOCK_RE = re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END), re.DOTALL)


# --- managed-block (text) ---------------------------------------------------


def _normalize_eol(text: str) -> str:
    """Fold CRLF/CR line endings to ``\\n`` so detection and hashing are EOL-agnostic.

    Managed-block ownership is verified by hashing the block's inner content
    (GK-F4). Without this, a CRLF file hashes differently from its LF equivalent,
    the ownership check never matches, and the block becomes frozen/unremovable.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _render_block(inner: str) -> str:
    """Render Cohort's managed block around ``inner``.

    Rejects ``inner`` that embeds either sentinel literal (R2-N1): a stray
    ``BLOCK_END`` would truncate the rendered region so its ownership hash could
    never match, freezing the block as unremovable. Fail loudly at render time
    instead of writing a self-corrupting block.
    """
    if BLOCK_BEGIN in inner or BLOCK_END in inner:
        raise ValueError(
            "managed-block content contains a Cohort sentinel marker "
            "(BLOCK_BEGIN/BLOCK_END); refusing to render a self-corrupting block"
        )
    return f"{BLOCK_BEGIN}\n{inner.strip(chr(10))}\n{BLOCK_END}"


def extract_block(text: str) -> Optional[str]:
    """Return the inner content of Cohort's managed block, or None if absent.

    Line endings are normalized to ``\\n`` first so a CRLF file's block is
    detected and its inner content extracted identically to the LF equivalent
    (GK-F4)."""
    text = _normalize_eol(text)
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
    """Remove Cohort's managed block, folding only the local seam.

    Normalizes just the newlines immediately adjacent to the block (the seam
    ``upsert_block`` introduced) and leaves the rest of the file byte-identical
    (O2). A whole-file blank-run collapse here would destroy user-authored blank
    runs far from the block, so it is deliberately avoided."""
    m = _BLOCK_RE.search(text)
    if m is None:
        return text
    before = text[: m.start()].rstrip("\n")
    after = text[m.end() :].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    if before:
        return before + "\n"
    return after


def block_hash(inner: str) -> str:
    """Ownership hash of a managed block's inner content, EOL-agnostic (GK-F4)."""
    return hashlib.sha256(_normalize_eol(inner).strip("\n").encode("utf-8")).hexdigest()


# --- key-merge (JSON) -------------------------------------------------------


def entry_hash(entry: Any) -> str:
    """Stable hash of a JSON-serializable hook entry (order-insensitive)."""
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def merge_hooks(
    existing: dict,
    fragment: dict,
    prior_tags: Optional[list[dict]] = None,
    force: bool = False,
) -> tuple[dict, list[dict], int]:
    """Merge Cohort's hook entries into ``existing`` settings (re-merge safe).

    ``fragment`` is ``{"hooks": {<Event>: [entry, ...]}}``. ``prior_tags`` are the
    ``[{event, entry_hash}]`` Cohort recorded last time, which let re-merge tell
    *our* entries from the user's by content hash (decision K — no in-file
    marker). Returns ``(new_settings, owned_tags, skipped)`` where:

    - **owned_tags** are every Cohort entry currently in the file (for reverse).
    - **skipped** counts canonical entries we declined to re-add because the user
      edited or removed the entry we had placed there (divergence → never
      duplicate, never overwrite the user's version).

    Idempotent: re-merging an unchanged roster yields ``new == existing``.
    """
    prior_tags = prior_tags or []
    prior_by_event: dict[str, set] = {}
    for t in prior_tags:
        prior_by_event.setdefault(t["event"], set()).add(t["entry_hash"])

    new = copy.deepcopy(existing)
    hooks = new.setdefault("hooks", {})
    owned: list[dict] = []
    skipped = 0
    for event, canon_entries in fragment.get("hooks", {}).items():
        arr = hooks.setdefault(event, [])
        prior_set = prior_by_event.get(event, set())
        canon_hashes = [entry_hash(e) for e in canon_entries]
        canon_set = set(canon_hashes)
        existing_set = {entry_hash(e) for e in arr}

        # 1. Drop our prior entries that canonical no longer includes (order kept).
        kept = [e for e in arr if not (entry_hash(e) in prior_set and entry_hash(e) not in canon_set)]
        kept_set = {entry_hash(e) for e in kept}

        # 2. Prior entries no longer present unchanged → user edited/removed them.
        diverged = {h for h in prior_set if h not in existing_set}

        # 3. Add canonical entries not already present; suppress diverged ones.
        for entry, h in zip(canon_entries, canon_hashes):
            if h in kept_set:
                # Already in the file. Claim ownership only if it was ours before
                # (in prior_set); a byte-identical entry the user authored and we
                # never placed must NOT be claimed (GK-F2) — else reverse would
                # delete the user's own entry on uninstall.
                if h in prior_set:
                    owned.append({"event": event, "entry_hash": h})
                continue
            if h in diverged and not force:
                skipped += 1  # the user took over this entry → don't re-add
                continue
            kept.append(copy.deepcopy(entry))
            kept_set.add(h)
            owned.append({"event": event, "entry_hash": h})
        hooks[event] = kept

    for event in list(hooks.keys()):
        if not hooks[event]:
            del hooks[event]
    if "hooks" in new and not new["hooks"]:
        del new["hooks"]
    return new, owned, skipped


def plan_block_merge(
    text: str, desired_inner: str, prior_block_hash: Optional[str], force: bool = False
) -> dict:
    """Plan a managed-block merge (re-merge safe).

    Returns ``{new_text, changed, skipped, block_hash}``. A block whose content
    no longer matches what Cohort recorded — and isn't already the desired
    content — is treated as a user edit: left untouched (``skipped=1``), never
    overwritten. A block that matches the prior hash (or has no prior, i.e. our
    delimited namespace) is updated to the desired content. ``force`` re-asserts
    Cohort's block regardless of divergence (the explicit restore path).
    """
    desired_hash = block_hash(desired_inner)
    if force:
        new_text = upsert_block(text, desired_inner)
        return {"new_text": new_text, "changed": new_text != text, "skipped": 0,
                "block_hash": desired_hash}
    current = extract_block(text)
    if current is None:
        # No block. Insert on first install; but if we recorded one before, the
        # user deleted it — treat removal as divergence (don't silently re-add),
        # consistent with the hook key-merge.
        if prior_block_hash is not None:
            return {"new_text": text, "changed": False, "skipped": 1, "block_hash": prior_block_hash}
        return {"new_text": upsert_block(text, desired_inner), "changed": True,
                "skipped": 0, "block_hash": desired_hash}
    current_hash = block_hash(current)
    if current_hash == desired_hash:
        return {"new_text": text, "changed": False, "skipped": 0, "block_hash": desired_hash}
    if prior_block_hash is None or current_hash == prior_block_hash:
        return {"new_text": upsert_block(text, desired_inner), "changed": True,
                "skipped": 0, "block_hash": desired_hash}
    # divergence: user edited inside our block → leave it, keep the prior identity
    return {"new_text": text, "changed": False, "skipped": 1, "block_hash": prior_block_hash}


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

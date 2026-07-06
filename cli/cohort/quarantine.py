"""Durable quarantine for artifacts pulled by ``cohort my-office sync`` (#107).

``my-office sync`` fast-forward-pulls the personal layer from a Git remote. On a
*shared / multi-writer* remote, anyone with push access can commit an
auto-activating artifact — a **hook** (its action runs on IDE events) or a
**memory** (compiled into every session's always-loaded corpus, a prompt-
injection→RCE path). Placing such a pull without review is the threat #107 tracks.

This records the identity — ``(kind, name, content-hash)`` — of every gated
artifact a pull introduced or changed, so the withhold is **durable and IDE-
agnostic**: *every* ``compile_ide`` (not just the sync recompile) withholds those
exact artifacts until the user clears them with ``cohort my-office approve``.
The hash pins the reviewed bytes: if the same name is later pulled with different
content, its new identity is not approved and is quarantined afresh.

Design notes:
* Gated kinds are the auto-activating sinks only (hooks, memories). Skills, agents,
  and commands are not gated (agents/commands are advisory text a session reads;
  the maintainer decision scoped skills out).
* Identity is content-addressed, so approving a name approves the bytes reviewed —
  not the name forever. A locally *authored* gated artifact is never recorded: sync
  reconciles the remote before committing local work, so the pull delta that feeds
  this module contains only what came from the remote.
* State lives at ``~/.cohort/state/quarantine.json`` beside the install manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .manifest import now_iso
from .schema import KIND_DIRS

# Auto-activating sinks that must not place without review (maintainer decision,
# #107): hooks run on IDE events, memories load into every session's corpus.
GATED_KINDS: tuple[str, ...] = ("hook", "memory")

# Reverse of KIND_DIRS, restricted to gated kinds — maps a canonical directory
# name back to its kind so a changed file path resolves to (kind, name).
_DIR_TO_GATED_KIND = {KIND_DIRS[k]: k for k in GATED_KINDS}


@dataclass(frozen=True)
class QuarantinedArtifact:
    """A pulled-but-unreviewed artifact, pinned by content so approving it
    approves the exact bytes reviewed."""

    kind: str
    name: str
    content_hash: str
    first_seen: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.name, self.content_hash)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "content_hash": self.content_hash,
            "first_seen": self.first_seen,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuarantinedArtifact":
        return cls(
            kind=d["kind"],
            name=d["name"],
            content_hash=d["content_hash"],
            first_seen=d.get("first_seen", ""),
        )


def content_hash(path: Path) -> str:
    """SHA-256 of a canonical artifact's raw bytes — the identity anchor."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gated_kind_and_name(path: Path) -> tuple[str, str] | None:
    """``(kind, name)`` for a canonical file under a gated kind directory, else
    None. ``…/canonical/hooks/foo.md`` → ``("hook", "foo")``. Gated kinds are flat
    ``<dir>/<name>.md`` files, so the parent directory names the kind."""
    kind = _DIR_TO_GATED_KIND.get(path.parent.name)
    if kind is None:
        return None
    return kind, path.stem


def _state_file(state_dir: Path) -> Path:
    return state_dir / "quarantine.json"


def load_pending(state_dir: Path) -> list[QuarantinedArtifact]:
    """The quarantined artifacts recorded under ``state_dir``. A missing or
    unreadable file yields [] — the caller (compile) then withholds nothing, which
    is correct: absence of a record is absence of a pending pull, and sync
    fail-closes by *re-recording* on the next pull rather than by assuming here."""
    path = _state_file(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [QuarantinedArtifact.from_dict(d) for d in data.get("pending", [])]
    except Exception:  # noqa: BLE001 - corrupt/partial file → treat as empty
        return []


def pending_keys(state_dir: Path) -> set[tuple[str, str, str]]:
    """The ``(kind, name, content_hash)`` identities currently withheld."""
    return {a.key for a in load_pending(state_dir)}


def _save_pending(state_dir: Path, items: Iterable[QuarantinedArtifact]) -> None:
    """Atomically persist the pending list (mirrors ``Manifest.persist``). A no-op
    if ``state_dir`` does not exist yet (nothing is installed)."""
    if not state_dir.exists():
        return
    path = _state_file(state_dir)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps({"pending": [a.to_dict() for a in items]}, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    if os.name != "nt":
        dir_fd = os.open(str(state_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def add_pending(
    state_dir: Path, artifacts: Iterable[QuarantinedArtifact]
) -> list[QuarantinedArtifact]:
    """Union ``artifacts`` into the pending set (dedup by identity). Returns only
    the newly-added ones, so a caller can report exactly what this pull quarantined."""
    if not state_dir.exists():  # nothing installed → nothing durably recordable
        return []
    existing = load_pending(state_dir)
    seen = {a.key for a in existing}
    added = [a for a in artifacts if a.key not in seen and not seen.add(a.key)]
    if added:
        _save_pending(state_dir, existing + added)
    return added


def approve(state_dir: Path, names: Iterable[str] | None = None, *, all: bool = False) -> list[str]:
    """Clear quarantine for the given ``names`` (any pending hash of that name), or
    every pending artifact when ``all``. Returns the names actually cleared."""
    pending = load_pending(state_dir)
    if all:
        cleared = sorted({a.name for a in pending})
        _save_pending(state_dir, [])
        return cleared
    wanted = set(names or ())
    cleared = sorted({a.name for a in pending if a.name in wanted})
    if cleared:
        _save_pending(state_dir, [a for a in pending if a.name not in wanted])
    return cleared


def reconcile(state_dir: Path, my_root: Path) -> list[QuarantinedArtifact]:
    """Drop pending records whose exact bytes are no longer present on disk (the
    artifact was deleted or changed by a later pull/edit), returning the survivors.
    Keeps ``review`` honest without a git query — a stale pin can't linger as a
    phantom, and a changed artifact is re-recorded by the next sync."""
    live_hash: dict[tuple[str, str], str] = {}
    canonical = my_root / "canonical"
    for kind, subdir in ((k, KIND_DIRS[k]) for k in GATED_KINDS):
        d = canonical / subdir
        if not d.exists():
            continue
        for path in d.glob("*.md"):
            live_hash[(kind, path.stem)] = content_hash(path)
    pending = load_pending(state_dir)
    survivors = [a for a in pending if live_hash.get((a.kind, a.name)) == a.content_hash]
    if len(survivors) != len(pending):
        _save_pending(state_dir, survivors)
    return survivors

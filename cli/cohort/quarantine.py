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

Classification matches the compiler exactly. The compiler discovers artifacts with
a whole-tree ``rglob('*.md')`` and dispatches on the frontmatter ``kind`` — the
on-disk *directory* is not authoritative (nothing enforces ``kind: hook`` living in
``hooks/``). So this module also classifies by frontmatter ``kind``: a hook hidden
in ``canonical/agents/`` still renders as a hook, and must still be quarantined.

Design notes:
* Gated kinds are the auto-activating sinks only (hooks, memories). Skills, agents,
  and commands are not gated (advisory text a session reads; the maintainer
  decision scoped skills out).
* Identity is content-addressed, so approving a name approves the bytes reviewed —
  not the name forever. A locally *authored* gated artifact is never recorded: sync
  reconciles the remote before committing local work, so the pull delta that feeds
  this module contains only what came from the remote.
* State lives at ``~/.cohort/state/quarantine.json`` beside the install manifest. A
  present-but-unparseable file raises ``QuarantineStateError`` — callers fail closed
  (withhold), never treat a corrupt file as "nothing pending."
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .loader import load_artifact
from .manifest import now_iso

# Auto-activating sinks that must not place without review (maintainer decision,
# #107): hooks run on IDE events, memories load into every session's corpus.
GATED_KINDS: tuple[str, ...] = ("hook", "memory")


class QuarantineStateError(Exception):
    """The quarantine state file exists but could not be parsed. Callers must fail
    closed (withhold every gated artifact), never read it as an empty pending set."""


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
    """SHA-256 of a canonical artifact's raw bytes — the identity anchor. Matches the
    bytes ``compile_ide`` hashes for the same file, so the two sides agree."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gated_identity(path: Path) -> Optional[tuple[str, str]]:
    """``(kind, name)`` from an artifact's **frontmatter** when its kind is gated,
    else None — classifying the file the same way the compiler will (by ``kind``,
    not by directory). A file that cannot be loaded returns None: the compiler would
    raise on it too, so it never renders and needs no quarantine."""
    try:
        fm = load_artifact(path).frontmatter
    except Exception:  # noqa: BLE001 - unparseable → compile rejects it too → not placed
        return None
    if not isinstance(fm, dict):  # no/empty frontmatter → not an artifact the compiler renders
        return None
    kind, name = fm.get("kind"), fm.get("name")
    if kind in GATED_KINDS and isinstance(name, str) and name:
        return kind, name
    return None


def gated_artifacts(paths: Iterable[Path]) -> list[tuple[str, str, Path]]:
    """``(kind, name, path)`` for every given path that frontmatter-classifies as a
    gated artifact. Skips non-gated and unloadable files."""
    out: list[tuple[str, str, Path]] = []
    for p in paths:
        ident = gated_identity(p)
        if ident is not None:
            out.append((ident[0], ident[1], p))
    return out


def all_gated_in(canonical_root: Path) -> list[tuple[str, str, Path]]:
    """Every gated artifact anywhere under a canonical tree — whole-tree, so a hook
    misfiled outside ``hooks/`` is still found (the fail-closed enumeration)."""
    if not canonical_root.exists():
        return []
    return gated_artifacts(sorted(canonical_root.rglob("*.md")))


def _state_file(state_dir: Path) -> Path:
    return state_dir / "quarantine.json"


def load_pending(state_dir: Path) -> list[QuarantinedArtifact]:
    """The quarantined artifacts recorded under ``state_dir``. A *missing* file
    yields [] (no install / no pull yet). A *present-but-unparseable* file raises
    ``QuarantineStateError`` so the caller fails closed rather than silently
    activating records it can no longer read."""
    path = _state_file(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [QuarantinedArtifact.from_dict(d) for d in data["pending"]]
    except Exception as exc:  # noqa: BLE001 - corrupt/partial/schema drift → fail closed
        raise QuarantineStateError(f"unreadable quarantine state at {path}: {exc}") from exc


def pending_keys(state_dir: Path) -> set[tuple[str, str, str]]:
    """The ``(kind, name, content_hash)`` identities currently withheld. Propagates
    ``QuarantineStateError`` on a corrupt file (the caller must fail closed)."""
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
    the newly-added ones, so a caller can report exactly what this pull quarantined.
    Propagates ``QuarantineStateError`` if the existing state is unreadable."""
    if not state_dir.exists():  # nothing installed → nothing durably recordable
        return []
    existing = load_pending(state_dir)
    seen = {a.key for a in existing}
    added: list[QuarantinedArtifact] = []
    for a in artifacts:
        if a.key not in seen:
            seen.add(a.key)
            added.append(a)
    if added:
        _save_pending(state_dir, existing + added)
    return added


def approve(
    state_dir: Path, names: Iterable[str] | None = None, *, approve_all: bool = False
) -> list[str]:
    """Clear quarantine for the given ``names`` (any pending hash of that name), or
    every pending artifact when ``approve_all``. Returns the names actually cleared.
    Propagates ``QuarantineStateError`` on a corrupt file (refuse rather than reset)."""
    pending = load_pending(state_dir)
    if approve_all:
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
    Classifies on-disk artifacts by frontmatter kind (whole-tree), matching how they
    were recorded, so the two sides can never disagree on what "gated" means."""
    live_hash: dict[tuple[str, str], str] = {
        (kind, name): content_hash(path)
        for kind, name, path in all_gated_in(my_root / "canonical")
    }
    pending = load_pending(state_dir)
    survivors = [a for a in pending if live_hash.get((a.kind, a.name)) == a.content_hash]
    if len(survivors) != len(pending):
        _save_pending(state_dir, survivors)
    return survivors

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
* Gated kinds are the auto-activating sinks: hooks, memories, skills, and agents.
  A skill's description auto-loads into every session's context and its body is
  model-invocable; an agent's description auto-loads too and it is
  model-spawnable — both are prompt-injection sinks equal to memories. Commands
  are user-invoked (a human must run them), so they are not gated here.
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

from .filelock import file_lock
from .loader import load_artifact
from .manifest import now_iso

# Auto-activating sinks that must not place without review (maintainer decision,
# #107, extended by adversarial review): hooks run on IDE events, memories load
# into every session's corpus, a skill's description auto-loads into every
# session's context and its body is model-invocable, and an agent's description
# auto-loads too and it is model-spawnable — all four are prompt-injection sinks.
# Commands are user-invoked (a human must explicitly run them), so they carry
# lower risk than these auto-activating kinds and are not gated here; that could
# change if evidence shows otherwise.
GATED_KINDS: tuple[str, ...] = ("hook", "memory", "skill", "agent")


class QuarantineStateError(Exception):
    """The quarantine state file exists but could not be parsed. Callers must fail
    closed (withhold every gated artifact), never read it as an empty pending set."""


class AmbiguousApprovalError(Exception):
    """A name selector passed to ``approve`` matches more than one pending
    content-hash and no hash prefix was given to disambiguate. Approving would
    have to guess which record was actually reviewed — and clearing all of them
    would also clear an unreviewed record that merely shares the name (the risk
    ``approve`` must never take). Callers must re-invoke with a hash-prefix
    selector (``"name@hashprefix"``) naming the specific record reviewed."""


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


_QUARANTINE_FILE = "quarantine.json"  # my-office (personal overlay) pull quarantine
_OFFICE_QUARANTINE_FILE = "office_quarantine.json"  # office/source-layer pull quarantine


def _state_file(state_dir: Path) -> Path:
    return state_dir / _QUARANTINE_FILE


def _read_pending_file(path: Path) -> list[QuarantinedArtifact]:
    """Parse a pending-list file. A *missing* file yields [] (no install / no pull
    yet). A *present-but-unparseable* file raises ``QuarantineStateError`` so the
    caller fails closed rather than silently activating records it can no longer read."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [QuarantinedArtifact.from_dict(d) for d in data["pending"]]
    except Exception as exc:  # noqa: BLE001 - corrupt/partial/schema drift → fail closed
        raise QuarantineStateError(f"unreadable quarantine state at {path}: {exc}") from exc


def _write_pending_file(state_dir: Path, path: Path, items: Iterable[QuarantinedArtifact]) -> None:
    """Atomically persist a pending list (mirrors ``Manifest.persist``). A no-op if
    ``state_dir`` does not exist yet (nothing is installed)."""
    if not state_dir.exists():
        return
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


def load_pending(state_dir: Path) -> list[QuarantinedArtifact]:
    """The quarantined artifacts recorded under ``state_dir``. A *missing* file
    yields [] (no install / no pull yet). A *present-but-unparseable* file raises
    ``QuarantineStateError`` so the caller fails closed rather than silently
    activating records it can no longer read."""
    return _read_pending_file(_state_file(state_dir))


def pending_keys(state_dir: Path) -> set[tuple[str, str, str]]:
    """The ``(kind, name, content_hash)`` identities currently withheld. Propagates
    ``QuarantineStateError`` on a corrupt file (the caller must fail closed)."""
    return {a.key for a in load_pending(state_dir)}


def _save_pending(state_dir: Path, items: Iterable[QuarantinedArtifact]) -> None:
    """Atomically persist the my-office pending list. No-op if ``state_dir`` absent."""
    _write_pending_file(state_dir, _state_file(state_dir), items)


def add_pending(
    state_dir: Path, artifacts: Iterable[QuarantinedArtifact]
) -> list[QuarantinedArtifact]:
    """Union ``artifacts`` into the pending set (dedup by identity). Returns only
    the newly-added ones, so a caller can report exactly what this pull quarantined.
    Propagates ``QuarantineStateError`` if the existing state is unreadable."""
    if not state_dir.exists():  # nothing installed → nothing durably recordable
        return []
    # Serialize the load→union→save cycle across processes: a lost add here is a
    # gate bypass (an unreviewed gated artifact never recorded, so never withheld),
    # not merely lost bookkeeping. state_dir exists, so its lock-file can be created.
    with file_lock(_state_file(state_dir)):
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
    """Clear quarantine for the given ``names``, or every pending artifact when
    ``approve_all``. Returns the names actually cleared.

    Each entry in ``names`` is either a bare artifact name, or ``"name@hash-prefix"``
    (a prefix of the record's ``content_hash``) to name one specific record. A bare
    name that currently has only one pending hash clears unambiguously — the common
    case. But ``reconcile``/``add_pending`` deliberately key on the full
    ``(kind, name, content_hash)`` identity, so two *different* pulls can share a
    name while differing in bytes (e.g. a name pulled once, reviewed and left
    pending, then pulled again from a compromised or racing pusher with different
    content). Clearing by bare name alone would approve BOTH — silently
    re-activating the unreviewed record. So when a bare name (or a hash prefix that
    still matches more than one distinct hash) is ambiguous, ``approve`` clears
    NOTHING for that selector and raises ``AmbiguousApprovalError`` listing the
    pending hashes, rather than guessing. ``approve_all`` is unaffected — it is the
    explicit "clear everything" escape hatch and stays name-only.

    Propagates ``QuarantineStateError`` on a corrupt file (refuse rather than reset).
    """
    if not state_dir.exists():
        return []  # nothing installed → nothing to clear
    # Serialize the load→clear→save cycle so a concurrent ``add_pending`` (a fresh
    # pull recording new gated artifacts) can't be clobbered by this approve's save.
    with file_lock(_state_file(state_dir)):
        pending = load_pending(state_dir)
        if approve_all:
            cleared = sorted({a.name for a in pending})
            _save_pending(state_dir, [])
            return cleared

        to_clear: set[tuple[str, str, str]] = set()
        cleared_names: set[str] = set()
        for selector in names or ():
            name, sep, hash_prefix = selector.partition("@")
            matches = [a for a in pending if a.name == name]
            if sep:
                matches = [a for a in matches if a.content_hash.startswith(hash_prefix)]
            if not matches:
                continue  # unknown name/hash → no-op, matches prior behavior
            distinct_hashes = sorted({a.content_hash for a in matches})
            if len(distinct_hashes) > 1:
                shown = ", ".join(h[:12] + "…" for h in distinct_hashes)
                raise AmbiguousApprovalError(
                    f"{name!r} matches {len(distinct_hashes)} pending records with "
                    f"different content — refusing to guess which was reviewed. "
                    f"Re-run with '{name}@<hash-prefix>' to pick one (pending hashes: {shown})."
                )
            for a in matches:
                to_clear.add(a.key)
                cleared_names.add(a.name)

        if to_clear:
            _save_pending(state_dir, [a for a in pending if a.key not in to_clear])
        return sorted(cleared_names)


def reconcile(state_dir: Path, my_root: Path) -> list[QuarantinedArtifact]:
    """Drop pending records whose exact bytes are no longer present on disk (the
    artifact was deleted or changed by a later pull/edit), returning the survivors.
    Classifies on-disk artifacts by frontmatter kind (whole-tree), matching how they
    were recorded, so the two sides can never disagree on what "gated" means.

    Keyed on the FULL ``(kind, name, hash)`` identity — matching ``add_pending`` and
    the ``compile_ide`` withhold — so two gated files that share a kind+name but
    differ in bytes (a correctly-filed hook and a misfiled duplicate) both survive.
    Collapsing them by ``(kind, name)`` would drop one live record and re-activate
    an unreviewed artifact on the next recompile."""
    if not state_dir.exists():
        return []  # nothing installed → nothing to reconcile
    live: set[tuple[str, str, str]] = {
        (kind, name, content_hash(path))
        for kind, name, path in all_gated_in(my_root / "canonical")
    }
    # Serialize the load→prune→save cycle against a concurrent add/approve.
    with file_lock(_state_file(state_dir)):
        pending = load_pending(state_dir)
        survivors = [a for a in pending if a.key in live]
        if len(survivors) != len(pending):
            _save_pending(state_dir, survivors)
    return survivors


# --- office/source-layer quarantine (F3) ------------------------------------
#
# The my-office quarantine above guards artifacts pulled into the PERSONAL overlay.
# The same threat applies to the shared OFFICE/source layer: ``cohort update``
# fast-forwards the office source, and on a shared office remote an update pull can
# introduce an auto-activating gated artifact (hook/memory/skill/agent) that a
# recompile would place with no review. This mirror guards that layer.
#
# Two deliberate design choices keep it correct and separate:
#   * A SEPARATE store file (``office_quarantine.json``). The my-office ``reconcile``
#     prunes records whose bytes are absent from ``my/canonical``; office records
#     point at the office source tree, so sharing one store would let ``reconcile``
#     silently drop — and thus re-activate — them. Separate stores can't collide.
#   * A ``office_baseline.json`` recording the office gated identities already trusted
#     (the shipped set at first install, grown as records are approved). It exists so
#     ``record_office_delta`` can tell a FIRST install (baseline absent ⇒ trust the
#     shipped office, quarantine nothing) from an UPDATE pull (baseline present ⇒
#     quarantine only identities not yet trusted — the delta).
#
# The office store is populated by ``record_office_delta`` (wired into ``cohort update``,
# after ``seed_office_baseline_if_absent`` records the pre-pull tree as the trusted
# baseline) and cleared by ``approve_office`` — the office analogue of what
# ``my-office sync``/``approve`` call. compile READS this store (``office_pending_keys``)
# and withholds, exactly as it reads ``pending_keys`` for my-office. The gate is LIVE: an
# update pull that introduces a gated office artifact (hook/memory/skill/agent) is
# withheld from the recompile until reviewed. The mechanism is fail-closed and tested here.

_OFFICE_BASELINE_FILE = "office_baseline.json"


def _office_state_file(state_dir: Path) -> Path:
    return state_dir / _OFFICE_QUARANTINE_FILE


def _office_baseline_file(state_dir: Path) -> Path:
    return state_dir / _OFFICE_BASELINE_FILE


def load_office_pending(state_dir: Path) -> list[QuarantinedArtifact]:
    """Office-layer analogue of ``load_pending``. Missing file ⇒ []; corrupt ⇒
    ``QuarantineStateError`` (caller fails closed)."""
    return _read_pending_file(_office_state_file(state_dir))


def office_pending_keys(state_dir: Path) -> set[tuple[str, str, str]]:
    """The gated-office ``(kind, name, content_hash)`` identities currently withheld.
    Propagates ``QuarantineStateError`` on a corrupt store (caller must fail closed)."""
    return {a.key for a in load_office_pending(state_dir)}


def _save_office_pending(state_dir: Path, items: Iterable[QuarantinedArtifact]) -> None:
    _write_pending_file(state_dir, _office_state_file(state_dir), items)


def load_office_baseline(state_dir: Path) -> Optional[set[tuple[str, str, str]]]:
    """The trusted office gated identities, or ``None`` when no baseline exists yet
    (⇒ first install: nothing has been established as trusted). A present-but-corrupt
    baseline raises ``QuarantineStateError`` so callers fail closed."""
    path = _office_baseline_file(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {tuple(x) for x in data["baseline"]}  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 - corrupt/schema drift → fail closed
        raise QuarantineStateError(f"unreadable office baseline at {path}: {exc}") from exc


def _save_office_baseline(state_dir: Path, identities: Iterable[tuple[str, str, str]]) -> None:
    """Atomically persist the trusted-office baseline. No-op if ``state_dir`` absent."""
    if not state_dir.exists():
        return
    path = _office_baseline_file(state_dir)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps({"baseline": sorted(identities)}, indent=2)
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


def _current_office_identities(office_root: Path) -> dict[tuple[str, str, str], Path]:
    """``(kind, name, content_hash) -> path`` for every gated artifact in the office
    canonical tree, classified by frontmatter kind (matching the compiler)."""
    out: dict[tuple[str, str, str], Path] = {}
    for kind, name, path in all_gated_in(office_root / "canonical"):
        out[(kind, name, content_hash(path))] = path
    return out


def seed_office_baseline_if_absent(state_dir: Path, office_root: Path) -> bool:
    """Establish the trusted-office baseline from the CURRENT office tree if no
    baseline exists yet; return whether it seeded.

    This is the #6 fix. ``cohort update`` calls it BEFORE the fast-forward merge,
    so ``office_root`` is still the *pre-pull* (install-time shipped) tree. Seeding
    the baseline from that tree means the pull that follows is measured as a delta
    against what the user already had — so an auto-activating hook/memory/skill/
    agent the pull *introduces* is quarantined by ``record_office_delta``, not
    silently folded into "trusted".

    Without this pre-pull seed, ``record_office_delta``'s own first-call branch
    (baseline absent ⇒ trust the current set) would run AFTER the merge and adopt
    the freshly-pulled set — trusting a shared-remote artifact introduced in that
    same pull, unreviewed. The residual trust boundary is deliberate: the pre-pull
    tree is the source the user chose to clone and install from, so trusting it is
    correct; only *later* pulls from a shared remote are gated.

    No-op returning False if a baseline already exists (never clobbers it) or
    ``state_dir`` is absent. A present-but-corrupt baseline is treated as existing
    (returns False); ``record_office_delta`` then raises ``QuarantineStateError``
    and the caller fails closed."""
    if not state_dir.exists():
        return False
    with file_lock(_office_state_file(state_dir)):
        if _office_baseline_file(state_dir).exists():
            return False
        _save_office_baseline(state_dir, _current_office_identities(office_root).keys())
    return True


def record_office_delta(state_dir: Path, office_root: Path) -> list[QuarantinedArtifact]:
    """Record the gated-office artifacts an update pull introduced (the office
    analogue of ``my-office sync``'s ``add_pending``). Call this AFTER an office
    ``cohort update`` pull, before recompiling.

    Normal flow: ``cohort update`` calls ``seed_office_baseline_if_absent`` on the
    PRE-pull tree first, so by the time this runs a baseline always exists and the
    just-pulled set is measured against the install-time shipped set. Every current
    gated identity not already in the baseline is the pull delta; each is added to
    the office pending store (withheld by compile until approved) and folded into
    the baseline so it is not re-flagged (it stays withheld via the pending store,
    not by re-detection). Returns the newly-quarantined records.

    Fallback (no baseline — a direct caller/test that did not pre-seed): establish
    the baseline as the current gated set and quarantine nothing. This trusts the
    current tree, which is only safe when it is the install-time shipped set; the
    #6 fix (``seed_office_baseline_if_absent``, called pre-pull) is what guarantees
    that in the real update path. No-op returning [] if ``state_dir`` is absent."""
    if not state_dir.exists():
        return []
    current = _current_office_identities(office_root)
    # Serialize the baseline+pending read-modify-write against a concurrent office
    # approve/reconcile/seed (all anchor on the same office lock file).
    with file_lock(_office_state_file(state_dir)):
        baseline = load_office_baseline(state_dir)
        if baseline is None:  # unseeded fallback: trust the current set, quarantine nothing
            _save_office_baseline(state_dir, current.keys())
            return []
        new_identities = [ident for ident in current if ident not in baseline]
        if not new_identities:
            return []
        existing = load_office_pending(state_dir)
        seen = {a.key for a in existing}
        added: list[QuarantinedArtifact] = []
        for kind, name, chash in new_identities:
            if (kind, name, chash) not in seen:
                added.append(QuarantinedArtifact(kind, name, chash, now_iso()))
        if added:
            _save_office_pending(state_dir, existing + added)
            # Fold the delta into the baseline: it is now "seen" and must not be
            # re-detected on the next pull. It remains WITHHELD by the pending store
            # until approved — approval removes it from pending, not from the baseline.
            _save_office_baseline(state_dir, set(baseline) | set(current))
    return added


def approve_office(
    state_dir: Path, names: Iterable[str] | None = None, *, approve_all: bool = False
) -> list[str]:
    """Clear the office quarantine for reviewed artifacts (office analogue of
    ``approve``). Same content-addressed, ambiguity-refusing semantics: a bare name
    matching two distinct pending hashes raises ``AmbiguousApprovalError`` rather than
    guess. Approving removes the record from the office pending store; the identity
    stays in the baseline, so it is not re-quarantined and compile places it next
    recompile. Propagates ``QuarantineStateError`` on a corrupt store."""
    if not state_dir.exists():
        return []  # nothing installed → nothing to clear
    # Serialize the load→clear→save cycle against a concurrent record_office_delta.
    with file_lock(_office_state_file(state_dir)):
        pending = load_office_pending(state_dir)
        if approve_all:
            cleared = sorted({a.name for a in pending})
            _save_office_pending(state_dir, [])
            return cleared

        to_clear: set[tuple[str, str, str]] = set()
        cleared_names: set[str] = set()
        for selector in names or ():
            name, sep, hash_prefix = selector.partition("@")
            matches = [a for a in pending if a.name == name]
            if sep:
                matches = [a for a in matches if a.content_hash.startswith(hash_prefix)]
            if not matches:
                continue
            distinct_hashes = sorted({a.content_hash for a in matches})
            if len(distinct_hashes) > 1:
                shown = ", ".join(h[:12] + "…" for h in distinct_hashes)
                raise AmbiguousApprovalError(
                    f"{name!r} matches {len(distinct_hashes)} pending office records with "
                    f"different content — refusing to guess which was reviewed. "
                    f"Re-run with '{name}@<hash-prefix>' to pick one (pending hashes: {shown})."
                )
            for a in matches:
                to_clear.add(a.key)
                cleared_names.add(a.name)
        if to_clear:
            _save_office_pending(state_dir, [a for a in pending if a.key not in to_clear])
        return sorted(cleared_names)


def office_reconcile(state_dir: Path, office_root: Path) -> list[QuarantinedArtifact]:
    """Drop office pending records whose exact bytes are no longer in the office tree
    (deleted or superseded by a later pull), returning survivors. Office analogue of
    ``reconcile`` — but against ``office_root/canonical``, never ``my/canonical`` (the
    reason the two stores are separate)."""
    if not state_dir.exists():
        return []  # nothing installed → nothing to reconcile
    live = set(_current_office_identities(office_root))
    # Serialize the load→prune→save cycle against a concurrent record/approve.
    with file_lock(_office_state_file(state_dir)):
        pending = load_office_pending(state_dir)
        survivors = [a for a in pending if a.key in live]
        if len(survivors) != len(pending):
            _save_office_pending(state_dir, survivors)
    return survivors

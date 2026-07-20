"""The compile pipeline: canonical → IR → staged Claude files → ops.

Renderers are pure and byte-stable; staging is a derived mirror of each IDE's
native layout under ``~/.cohort/compiled/<ide>/``. The installer then links (or
copies) staging → the IDE dests, so a ``git pull`` + ``recompile`` propagates
through the symlinks. ``install`` places whatever is staged; ``recompile`` =
compile-then-install.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .adapters.base import FIELD_CONTRACTS, FieldContract
from .adapters.claude import MERGE_SUBDIR, ClaudeRenderer, MarkerError, StagedFile
from .adapters.codex import CodexRenderer
from .adapters.cursor import CursorRenderer
from .executor import path_hash
from .install_model import CohortPaths, Op, OpType
from .ir import build_ir
from .loader import load_artifact
from .quarantine import (
    GATED_KINDS,
    QuarantineStateError,
    content_hash,
    office_pending_keys,
    pending_keys,
)
from .schema import KINDS, discover_artifacts, kind_schema, shared_schema, validate_frontmatter

# Renderers by IDE — each is a descriptor the pipeline drives off (P7-R1).
RENDERERS: dict = {
    "claude": ClaudeRenderer(),
    "codex": CodexRenderer(),
    "cursor": CursorRenderer(),
}


class CompileError(Exception):
    """Raised when a canonical artifact fails to load or validate during compile."""


# --- IR field-contract check (a new field can't silently go Claude-only) ------


@lru_cache(maxsize=1)
def canonical_field_universe() -> frozenset[str]:
    """Every canonical field an IR can carry: the union of the shared and per-kind
    schema properties, plus ``body`` (IR-carried content, not a frontmatter key).

    Derived from the schema files, so adding a schema property automatically widens
    the universe — which forces ``assert_field_contract`` to fail until every
    renderer classifies the new field (the fail-closed property)."""
    fields: set[str] = set(shared_schema()["properties"])
    for kind in KINDS:
        fields |= set(kind_schema(kind)["properties"])
    fields.add("body")
    return frozenset(fields)


def assert_field_contract(
    universe: Optional[frozenset[str]] = None,
    contracts: Optional[dict[str, FieldContract]] = None,
) -> None:
    """Fail closed unless every renderer classifies every canonical field.

    For each renderer's ``FieldContract``, every canonical field must be either
    ``handled`` or ``declined`` — never silently unaccounted for (that is exactly how
    a field added for Claude would vanish for Codex/Cursor). Also rejects a field
    declared both handled and declined, a phantom field that is not canonical, and a
    renderer with no contract at all. Raises ``CompileError`` naming the field(s) +
    renderer(s). Parameters are injectable for testing; both default to the live
    schema-derived universe and the declared contracts."""
    universe = canonical_field_universe() if universe is None else universe
    contracts = FIELD_CONTRACTS if contracts is None else contracts
    problems: list[str] = []
    for ide in RENDERERS:
        if ide not in contracts:
            problems.append(f"{ide}: renderer declares no field contract")
    for ide, contract in sorted(contracts.items()):
        classified = contract.classified()
        for f in sorted(universe - classified):
            problems.append(
                f"{ide}: canonical field {f!r} is neither handled nor declined "
                "(it would render for some IDEs only)"
            )
        for f in sorted(contract.handled & contract.declined):
            problems.append(f"{ide}: field {f!r} is declared both handled and declined")
        for f in sorted(classified - universe):
            problems.append(
                f"{ide}: field {f!r} is classified but is not a canonical field"
            )
    if problems:
        raise CompileError("IR field-contract violation:\n  " + "\n  ".join(problems))


@dataclass
class CompileResult:
    ide: str
    staged: list[StagedFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # names skipped (wrong target / deferred kind)
    # Excluded by the tier partition (valid scope, wrong tier) — surfaced so an
    # artifact authored in the wrong tree never vanishes without a trace.
    scope_filtered: list[str] = field(default_factory=list)
    # Office artifacts deliberately replaced by a personalized my-layer copy.
    overridden: list[str] = field(default_factory=list)
    # Pulled-but-unreviewed my-layer artifacts held back by the quarantine (#107),
    # as "<kind> <name>" — surfaced so sync/status can tell the user what awaits
    # `cohort my-office approve` instead of silently vanishing.
    withheld: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": "compile",
            "ide": self.ide,
            "staged": [s.staged_rel for s in self.staged],
            "skipped": self.skipped,
            "scope_filtered": self.scope_filtered,
            "overridden": self.overridden,
            "withheld": self.withheld,
        }


def _load_irs(source: Path, scope: Optional[str] = None, layer: str = "office"):
    irs = []
    scope_filtered = []
    for p in discover_artifacts(source / "canonical"):
        result = load_artifact(p)
        if result.load_error is not None:
            raise CompileError(f"{p}: {result.load_error.message}")
        errors = validate_frontmatter(result.frontmatter, p.stem)
        if errors:
            raise CompileError(f"{p}: {errors[0].code} {errors[0].message}")
        ir = build_ir(result.frontmatter, result.body, p)
        ir.layer = layer
        # Tier partition: a tier only ever compiles its own scope. This is the leak
        # guard — a scope:project artifact in the global canonical can never reach the
        # global office, and vice versa. Runs per layer, BEFORE any merge, so a
        # mis-scoped my-layer artifact can never displace an office original.
        if scope is not None and ir.scope != scope:
            scope_filtered.append(f"{ir.name} (scope: {ir.scope})")
            continue
        irs.append(ir)
    return irs, scope_filtered


def merge_layers(office_irs: list, my_irs: list) -> tuple[list, list]:
    """Merge the my layer over the office layer (#84).

    An unmarked ``(kind, name)`` collision is a hard error, not a silent mask.
    A my artifact carrying ``overrides: true`` (set by ``cohort personalize``)
    deliberately REPLACES its office counterpart in place — surfaced on
    ``CompileResult.overridden`` so status/dashboard can badge it. Order is
    deterministic: office artifacts (discovery order, overrides swapped in
    place) then my additions (discovery order); renderers sort downstream.
    Returns ``(merged, overridden_names)``.
    """
    position = {(ir.kind, ir.name): i for i, ir in enumerate(office_irs)}
    merged = list(office_irs)
    additions = []
    collisions = []
    overridden = []
    for ir in my_irs:
        key = (ir.kind, ir.name)
        marked = ir.fields.get("overrides") is True
        if key in position:
            if marked:
                merged[position[key]] = ir  # deliberate override, my wins
                overridden.append(ir.name)
            else:
                collisions.append(f"{ir.kind} {ir.name!r}")
        else:
            # includes a dangling override (office counterpart gone): still the
            # user's content — compile it; `cohort status` flags the dangle
            additions.append(ir)
    if collisions:
        raise CompileError(
            "my-office artifacts collide with office artifacts: "
            + ", ".join(sorted(collisions))
            + " — rename yours, or make the override deliberate with "
            "`cohort personalize`"
        )
    return merged + additions, sorted(overridden)


def _apply_withhold(
    candidates: list,
    keys: set[tuple[str, str, str]],
    fail_closed: bool,
    result: CompileResult,
) -> list:
    """Drop every gated artifact whose content identity is withheld (or all gated
    artifacts when ``fail_closed``), recording each on ``result.withheld``. Shared by
    the my-layer and office-layer quarantine gates so both fail closed identically:
    a gated artifact with no ``source_path`` (unverifiable identity) or an exact
    content-hash match is withheld; every ambiguous case errs toward withholding."""
    kept = []
    for ir in candidates:
        gated = ir.kind in GATED_KINDS
        if gated and (
            fail_closed
            or ir.source_path is None
            or (ir.kind, ir.name, content_hash(ir.source_path)) in keys
        ):
            result.withheld.append(f"{ir.kind} {ir.name}")
            continue
        kept.append(ir)
    return kept


def compile_ide(
    source: Path, ide: str, scope: Optional[str] = None,
    only_agents: Optional[frozenset[str]] = None, project_tier: bool = False,
    overlay: Optional[Path] = None,
    withhold: Optional[set[tuple[str, str, str]]] = None,
    office_withhold: Optional[set[tuple[str, str, str]]] = None,
) -> CompileResult:
    """Render every targeting canonical artifact of ``scope`` into staged files for
    ``ide``. The global install passes ``scope="global"`` (the leak guard — project
    artifacts never reach the global office); a project-tier compile passes
    ``"project"`` with ``project_tier=True`` (no office directory, no generalist,
    no CLAUDE.md memory merge); ``None`` (default) compiles all scopes, for
    direct/test use.

    ``overlay`` is the my-office layer root (``~/.cohort/my``): its canonical/ is
    loaded additively over the office layer (collisions refuse — see
    ``merge_layers``). Callers pass it explicitly; compile never derives it from
    ``Path.home()``, so in-process tests and goldens stay hermetic. The project
    tier never passes an overlay.

    ``withhold`` is the quarantine (#107): a set of ``(kind, name, content-hash)``
    identities — pulled-but-unreviewed my-layer hooks/memories — to hold back so no
    recompile silently activates them. When ``None`` and an ``overlay`` is given, it
    is derived from the overlay's sibling ``state/`` dir, so *every* compile path
    withholds without each caller wiring it; pass an explicit set (or one derived
    from a hermetic overlay) in tests. No overlay ⇒ nothing is withheld.

    ``office_withhold`` (F3) is the same gate for the **office/source layer**: a set
    of gated-office identities a source *update pull* introduced but that have not
    been reviewed, held back so a recompile does not auto-activate them. When ``None``
    and an ``overlay`` is given, it is derived from the office quarantine store beside
    the state dir (``office_pending_keys``); a corrupt store fails closed (withholds
    every gated office artifact). It runs only for a global compile (``overlay`` given
    — the project tier passes none): the state dir is where the office pending set
    lives. On a first install the store is empty ⇒ the shipped office is NOT withheld.
    The recorder that *populates* the store from the pull delta is
    ``quarantine.record_office_delta``; wiring it into ``cohort update`` is the
    residual gap noted there — until wired, the store stays empty and this gate is a
    safe no-op.

    ``only_agents`` restricts *office-layer* agent artifacts to the named subset
    (a tailored roster); my-layer agents always compile — the subset exists to
    tailor the company roster, and a personal agent was opted in by authoring
    it. Every other kind still compiles. Filtering happens before the renderer,
    so an injected office directory lists only the installed set.

    Generic over the renderer descriptor (P7-R1): ``renderer.compile(irs)`` owns
    the IDE-specific 1:1 + aggregate staging; this function just loads/validates
    the IR and wraps render errors.
    """
    # Fail closed if any renderer leaves a canonical field unclassified — a NEW IR
    # field must not be able to render for some IDEs only (the IR-contract check).
    assert_field_contract()
    result = CompileResult(ide=ide)
    renderer = RENDERERS.get(ide)
    if renderer is None:
        return result  # no renderer for this IDE
    irs, result.scope_filtered = _load_irs(source, scope)
    # Office-layer quarantine gate (F3): withhold gated OFFICE artifacts an update
    # pull introduced but that have not been reviewed, so a recompile cannot
    # auto-activate them. Reads the SEPARATE office store (so the my-only
    # ``reconcile`` never prunes these), keyed on the state dir beside the overlay.
    # Runs only for a global compile (overlay given); on a first install the store is
    # empty, so the shipped office is never withheld.
    if overlay is not None:
        if office_withhold is not None:
            office_keys, office_fail_closed = office_withhold, False
        else:
            try:
                office_keys, office_fail_closed = office_pending_keys(overlay.parent / "state"), False
            except QuarantineStateError:
                office_keys, office_fail_closed = set(), True
        if office_keys or office_fail_closed:
            irs = _apply_withhold(irs, office_keys, office_fail_closed, result)
    if overlay is not None and (overlay / "canonical").exists():
        my_irs, my_filtered = _load_irs(overlay, scope, layer="my")
        result.scope_filtered.extend(f"{entry} [my]" for entry in my_filtered)
        # Quarantine gate (#107): withhold pulled-but-unreviewed my-layer
        # hooks/memories at the single compile chokepoint, so no recompile from any
        # command silently activates them. Derived from the overlay's sibling
        # state/ dir unless the caller passes an explicit set. A corrupt state file
        # (keys is None) fails CLOSED — withhold every gated my-layer artifact —
        # rather than read as "nothing pending" and activate them.
        fail_closed = False
        if withhold is not None:
            keys = withhold
        else:
            try:
                keys = pending_keys(overlay.parent / "state")
            except QuarantineStateError:
                keys, fail_closed = set(), True
        if keys or fail_closed:
            # A gated artifact is withheld when the state is corrupt, when its
            # identity is unverifiable (no source_path), or when its exact bytes are
            # quarantined — every ambiguous case fails closed (see _apply_withhold).
            my_irs = _apply_withhold(my_irs, keys, fail_closed, result)
        irs, result.overridden = merge_layers(irs, my_irs)
    result.withheld.sort()  # stable order across the office + my gates
    if only_agents is not None:
        excluded = [
            ir.name for ir in irs
            if ir.kind == "agent" and ir.layer == "office" and ir.name not in only_agents
        ]
        irs = [
            ir for ir in irs
            if not (ir.kind == "agent" and ir.layer == "office" and ir.name not in only_agents)
        ]
        result.skipped.extend(sorted(excluded))
    try:
        staged, skipped = renderer.compile(irs, project_tier=project_tier)
    except MarkerError as exc:
        raise CompileError(str(exc)) from exc
    result.staged = staged
    result.skipped.extend(skipped)
    return result


def _assert_staging_contained(paths: CohortPaths, staging_root: Path) -> None:
    """Refuse a redirected staging tree (the repo-escape guard).

    A hostile repo can pre-plant ``.cohort`` or ``.cohort/compiled`` as a symlink
    into ``$HOME`` so a project-tier compile rmtrees and rewrites the *global*
    staging (which the global office links into ``~/.claude``). Staging is
    Cohort-derived and never legitimately a symlink, so any symlink component —
    or a compiled dir resolving outside the install base — is refused.
    """
    if paths.cohort_home.is_symlink() or paths.compiled.is_symlink() or staging_root.is_symlink():
        raise CompileError(
            f"refusing to write staging: {paths.compiled} is (or is under) a symlink"
        )
    base = paths.base.resolve()
    resolved = paths.compiled.resolve()
    if base != resolved and base not in resolved.parents:
        raise CompileError(
            f"refusing to write staging: {paths.compiled} resolves outside {base}"
        )


def write_staging(paths: CohortPaths, result: CompileResult) -> None:
    """Write a compile result to ``compiled/<ide>/``, replacing prior staging.

    Staging is derived and disposable, so the IDE subtree is rebuilt wholesale —
    a canonical artifact removed since last compile leaves no stale staged file.
    """
    staging_root = paths.compiled_ide(result.ide)
    _assert_staging_contained(paths, staging_root)
    if staging_root.exists():
        shutil.rmtree(staging_root)
    for sf in result.staged:
        dest = staging_root / sf.staged_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(sf.content)


def staging_tree_hash(paths: CohortPaths, ide: str) -> str:
    """Hash of an IDE's staging tree (for byte-stability assertions)."""
    root = paths.compiled_ide(ide)
    return path_hash(root) if root.exists() else ""


def planned_dests(paths: CohortPaths, results: list[CompileResult]) -> set[str]:
    """The install dests a set of *in-memory* compile results would place.

    Mirrors ``scan_staging_ops``' staged→dest mapping but reads the results
    directly, so a dry-run (which never writes staging) can still compute the
    authoritative post-compile dest set for stale-artifact planning."""
    dests: set[str] = set()
    for result in results:
        renderer = RENDERERS.get(result.ide)
        if renderer is None:
            continue
        dest_root = renderer.dest_root(paths.base)
        for sf in result.staged:
            if MERGE_SUBDIR in Path(sf.staged_rel).parts:
                continue
            dests.add(str(dest_root / sf.staged_rel))
    return dests


# --- staging → install ops --------------------------------------------------


def scan_staging_ops(paths: CohortPaths, ide: str, mode: str) -> list[Op]:
    """Emit Phase-1 ops to place an IDE's staged files at their dests.

    Staging mirrors the IDE's native layout, so a file at
    ``compiled/<ide>/<rel>`` maps to ``~/.<ide>/<rel>`` (here ``~/.claude/<rel>``).
    Files under the ``.merge`` subdir are payloads consumed by merge ops (T3),
    not mirrored. mkdir ops cover the dest dirs (user-owned, usually satisfied).
    """
    renderer = RENDERERS.get(ide)
    staging = paths.compiled_ide(ide)
    if renderer is None or not staging.exists():
        return []
    dest_root = renderer.dest_root(paths.base)
    files = [
        p
        for p in sorted(staging.rglob("*"))
        if p.is_file() and MERGE_SUBDIR not in p.relative_to(staging).parts
    ]
    dirs: set[Path] = {dest_root}
    file_ops: list[Op] = []
    op_type = OpType.COPY.value if mode == "copy" else OpType.LINK.value
    for f in files:
        dest = dest_root / f.relative_to(staging)
        d = dest.parent
        while True:
            dirs.add(d)
            if d == dest_root:
                break
            d = d.parent
        file_ops.append(Op(op_type, ide, str(dest), src=str(f)))
    mkdir_ops = [
        Op(OpType.MKDIR.value, ide, str(d)) for d in sorted(dirs, key=lambda p: len(p.parts))
    ]
    return mkdir_ops + file_ops + _merge_ops(renderer, staging, dest_root, ide)


def _merge_ops(renderer, staging: Path, dest_root: Path, ide: str) -> list[Op]:
    """Merge ops for the renderer's declared merge targets whose payload was staged."""
    ops: list[Op] = []
    for mt in getattr(renderer, "merge_targets", ()):
        payload = staging / mt.payload_rel
        if payload.exists():
            ops.append(
                Op(
                    OpType.MERGE.value,
                    ide,
                    str(dest_root / mt.dest_name),
                    src=str(payload),
                    strategy=mt.strategy,
                )
            )
    return ops

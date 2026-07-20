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


# --- the IR field contract (the "shared contract" made checkable) ------------


@dataclass(frozen=True)
class FieldContract:
    """Which canonical IR/schema fields a renderer renders vs. deliberately declines.

    "The IR is the shared contract" was only a *data shape*: nothing forced a newly
    added canonical field to be *considered* by every target, so a field added for
    Claude could silently become Claude-only for Codex/Cursor. This makes the
    contract explicit and machine-checkable per renderer:

    * ``handled``  — fields this renderer emits/consumes for at least one kind it
      renders (e.g. Claude ``model`` → the ``model:`` frontmatter key).
    * ``declined`` — fields this renderer knowingly does NOT emit: unsupported, or a
      documented gap (e.g. Codex/Cursor ``model``, whose per-agent model key is not
      doc-verified; ``matcher``, which Codex/Cursor omit because their hooks match
      all invocations; every artifact's ``version``/``owner``, which no renderer
      emits).

    ``compile.assert_field_contract`` fails **closed** if any canonical field is in
    neither set for some renderer — so a NEW schema field cannot silently vanish for
    a subset of IDEs; the author is forced to classify it (handle or decline) for
    every renderer. Classification is per-renderer and flat across kinds: the goal is
    that every field is a *deliberate* decision, not per-kind rendering coverage.

    (These tables live here, beside the renderer descriptor, rather than on each
    renderer class, because the completeness check is a cross-renderer invariant and
    the concrete renderers are a separate compilation unit. A future refactor may
    move each renderer's contract onto its class; the check would be unchanged.)
    """

    handled: frozenset[str]
    declined: frozenset[str]

    def classified(self) -> frozenset[str]:
        """Every field this renderer has explicitly accounted for."""
        return self.handled | self.declined


# Fields no renderer emits (metadata/plumbing consumed elsewhere): ``version`` and
# ``owner`` are never rendered; ``overrides``/``office_sha256`` drive the my-layer
# merge and stale-override detection; ``invocation``/``dry_run`` are command
# metadata + a safety invariant, not emitted bytes.
_UNIVERSAL_DECLINED = frozenset(
    {"version", "owner", "overrides", "office_sha256", "invocation", "dry_run"}
)
# Fields every renderer handles (shared routing/content + the agent/skill/memory/
# hook fields all three targets render).
_COMMON_HANDLED = frozenset(
    {
        "name", "kind", "scope", "description", "targets", "display_name", "body",
        "department", "topology", "advisory", "tools", "triggers",
        "event", "action", "priority",
    }
)

FIELD_CONTRACTS: dict[str, FieldContract] = {
    # Claude is the reference target: it renders every canonical field it can, incl.
    # ``model`` (model: key), ``matcher`` (hook matcher), and ``args`` (argument-hint).
    "claude": FieldContract(
        handled=_COMMON_HANDLED | frozenset({"model", "matcher", "args"}),
        declined=_UNIVERSAL_DECLINED,
    ),
    # Codex: ``command`` is a declared gap (→ ``args`` declined); no doc-verified
    # per-subagent model key (``model`` declined); hooks match all (``matcher`` declined).
    "codex": FieldContract(
        handled=_COMMON_HANDLED,
        declined=_UNIVERSAL_DECLINED | frozenset({"model", "matcher", "args"}),
    ),
    # Cursor: commands are plain markdown with no frontmatter (``args`` declined); no
    # doc-verified per-agent model key (``model`` declined); hooks match all (``matcher``).
    "cursor": FieldContract(
        handled=_COMMON_HANDLED,
        declined=_UNIVERSAL_DECLINED | frozenset({"model", "matcher", "args"}),
    ),
}

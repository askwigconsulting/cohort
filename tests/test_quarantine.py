"""#107: durable quarantine for my-office-pulled hooks/memories."""

from __future__ import annotations

from pathlib import Path

import pytest

from cohort import quarantine as q


def _art(kind="hook", name="on-start", h="abc", seen="t") -> q.QuarantinedArtifact:
    return q.QuarantinedArtifact(kind=kind, name=name, content_hash=h, first_seen=seen)


def _state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return d


# --- identity helpers (frontmatter-based, matching the compiler) --------------


def _art_file(path: Path, *, kind: str, name: str, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\nkind: {kind}\nscope: global\n"
        f"description: x.\ntargets: [claude]\n{extra}---\nbody\n",
        encoding="utf-8",
    )
    return path


def test_gated_identity_classifies_by_frontmatter_not_directory(tmp_path):
    hook = _art_file(tmp_path / "hooks" / "on-start.md", kind="hook", name="on-start",
                     extra="event: session_start\naction: cohort x\n")
    mem = _art_file(tmp_path / "memories" / "ctx.md", kind="memory", name="ctx",
                    extra="priority: high\ndisplay_name: Ctx\n")
    agent = _art_file(tmp_path / "agents" / "counsel.md", kind="agent", name="counsel",
                      extra="department: Law\ntopology: specialist\nadvisory: true\ntools: [read]\n"
                            "display_name: Counsel\n")
    skill = _art_file(tmp_path / "skills" / "office-guide.md", kind="skill", name="office-guide",
                      extra="triggers: [office]\n")
    cmd = _art_file(tmp_path / "commands" / "snapshot.md", kind="command", name="snapshot")
    # A hook MISFILED in the agents directory is still a hook by frontmatter.
    misfiled = _art_file(tmp_path / "agents" / "evil.md", kind="hook", name="evil",
                         extra="event: session_start\naction: cohort rce\n")
    assert q.gated_identity(hook) == ("hook", "on-start")
    assert q.gated_identity(mem) == ("memory", "ctx")
    # A skill's description auto-loads into every session and its body is
    # model-invocable; an agent's description auto-loads and it is
    # model-spawnable — both are prompt-injection sinks equal to memories (F1).
    assert q.gated_identity(agent) == ("agent", "counsel")
    assert q.gated_identity(skill) == ("skill", "office-guide")
    assert q.gated_identity(cmd) is None  # commands are user-invoked, not gated
    assert q.gated_identity(misfiled) == ("hook", "evil")  # the bypass, closed


def test_content_hash_tracks_bytes(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("one\n", encoding="utf-8")
    h1 = q.content_hash(p)
    p.write_text("two\n", encoding="utf-8")
    assert q.content_hash(p) != h1


# --- persistence + union -----------------------------------------------------


def test_load_pending_absent_is_empty(tmp_path):
    assert q.load_pending(_state(tmp_path)) == []
    assert q.pending_keys(_state(tmp_path)) == set()




def test_add_pending_dedups_by_identity_and_reports_new(tmp_path):
    state = _state(tmp_path)
    a, b = _art(name="a", h="1"), _art(name="b", h="2")
    assert {x.name for x in q.add_pending(state, [a, b])} == {"a", "b"}
    # re-adding the same identities adds nothing; a new hash for "a" is new
    a2 = _art(name="a", h="9")
    added = q.add_pending(state, [a, a2])
    assert [x.content_hash for x in added] == ["9"]
    assert q.pending_keys(state) == {("hook", "a", "1"), ("hook", "b", "2"), ("hook", "a", "9")}


def test_save_is_noop_when_state_dir_absent(tmp_path):
    missing = tmp_path / "nope"  # not created
    assert q.add_pending(missing, [_art()]) == []
    assert q.load_pending(missing) == []


# --- approve -----------------------------------------------------------------


def test_approve_by_bare_name_is_unambiguous_when_only_one_hash_pending(tmp_path):
    state = _state(tmp_path)
    q.add_pending(state, [_art(name="a", h="1"), _art(name="b", h="3")])
    cleared = q.approve(state, ["a"])
    assert cleared == ["a"]
    assert q.pending_keys(state) == {("hook", "b", "3")}


def test_approve_by_bare_name_fails_closed_when_two_hashes_share_the_name(tmp_path):
    # F2: reconcile/add_pending key on the FULL (kind, name, hash) identity, so a
    # name pulled twice with different bytes leaves two pending records. Approving
    # the reviewed one by bare name must NOT also clear the unreviewed one that
    # happens to share the name — that was the cross-provider-confirmed bug: it
    # silently re-activated an unreviewed artifact. approve() must refuse and
    # clear nothing rather than guess.
    state = _state(tmp_path)
    q.add_pending(state, [_art(name="a", h="1111111111"), _art(name="a", h="2222222222")])
    with pytest.raises(q.AmbiguousApprovalError) as exc_info:
        q.approve(state, ["a"])
    assert "1111111111" in str(exc_info.value)
    assert "2222222222" in str(exc_info.value)
    # nothing was cleared — both records are still pending
    assert q.pending_keys(state) == {("hook", "a", "1111111111"), ("hook", "a", "2222222222")}


def test_approve_by_hash_prefix_clears_only_that_record_leaves_sibling_pending(tmp_path):
    # The disambiguation escape hatch: "name@hash-prefix" names one specific
    # record. The sibling with a different hash must survive, still withheld.
    state = _state(tmp_path)
    q.add_pending(state, [_art(name="a", h="1111111111"), _art(name="a", h="2222222222")])
    cleared = q.approve(state, ["a@1111"])
    assert cleared == ["a"]
    assert q.pending_keys(state) == {("hook", "a", "2222222222")}


def test_approve_all_clears_everything(tmp_path):
    state = _state(tmp_path)
    q.add_pending(state, [_art(name="a"), _art(name="b")])
    assert set(q.approve(state, approve_all=True)) == {"a", "b"}
    assert q.load_pending(state) == []


def test_approve_unknown_name_is_a_noop(tmp_path):
    state = _state(tmp_path)
    q.add_pending(state, [_art(name="a")])
    assert q.approve(state, ["ghost"]) == []
    assert len(q.load_pending(state)) == 1


# --- reconcile ---------------------------------------------------------------


def test_reconcile_drops_records_not_matching_disk(tmp_path):
    state = _state(tmp_path)
    my = tmp_path / "my"
    live = _art_file(my / "canonical" / "hooks" / "present.md", kind="hook", name="present",
                     extra="event: session_start\naction: cohort x\n")
    live_hash = q.content_hash(live)
    q.add_pending(
        state,
        [
            q.QuarantinedArtifact("hook", "present", live_hash, "t"),  # matches disk → kept
            q.QuarantinedArtifact("hook", "present", "staleHASH", "t"),  # wrong hash → dropped
            q.QuarantinedArtifact("hook", "deleted", "whatever", "t"),  # gone from disk → dropped
        ],
    )
    survivors = q.reconcile(state, my)
    assert [a.content_hash for a in survivors] == [live_hash]
    assert q.pending_keys(state) == {("hook", "present", live_hash)}


def test_reconcile_keeps_both_when_two_gated_share_kind_and_name(tmp_path):
    # A correctly-filed hook and a misfiled duplicate share (kind=hook, name=evil)
    # but differ in bytes. reconcile must keep BOTH pending records (full-identity
    # key), not collapse them and re-activate the misfiled one.
    state = _state(tmp_path)
    my = tmp_path / "my"
    filed = _art_file(my / "canonical" / "hooks" / "evil.md", kind="hook", name="evil",
                      extra="event: session_start\naction: cohort good\n")
    misfiled = _art_file(my / "canonical" / "agents" / "evil.md", kind="hook", name="evil",
                         extra="event: session_start\naction: cohort rce\n")
    h_filed, h_misfiled = q.content_hash(filed), q.content_hash(misfiled)
    assert h_filed != h_misfiled
    q.add_pending(state, [
        q.QuarantinedArtifact("hook", "evil", h_filed, "t"),
        q.QuarantinedArtifact("hook", "evil", h_misfiled, "t"),
    ])
    survivors = q.reconcile(state, my)
    assert {a.content_hash for a in survivors} == {h_filed, h_misfiled}  # neither dropped


def test_load_pending_corrupt_raises_not_empty(tmp_path):
    # A present-but-unparseable file must NOT read as "nothing pending" (fail open);
    # it raises so callers withhold.
    state = _state(tmp_path)
    (state / "quarantine.json").write_text("{ truncated", encoding="utf-8")
    with pytest.raises(q.QuarantineStateError):
        q.load_pending(state)
    with pytest.raises(q.QuarantineStateError):
        q.pending_keys(state)


# --- compile_ide withhold integration ----------------------------------------

import shutil

from cohort.compile import compile_ide

_REPO = Path(__file__).resolve().parents[1]

# Unique markers so we can assert the artifact's *effect* on the aggregate outputs
# a hook/memory contribute to (settings.hooks.json / CLAUDE.cohort.md), not a
# standalone file (gated kinds don't stage 1:1).
_HOOK_MARKER = "cohort evil-pulled-action"
_MEMORY_MARKER = "PULLED-MEMORY-MARKER-XYZ"
_MY_HOOK = (
    "---\nname: pulled-hook\nkind: hook\nscope: global\n"
    "description: A hook pulled from a shared remote.\ntargets: [claude]\n"
    f"event: session_start\naction: {_HOOK_MARKER}\n---\nHook body.\n"
)
_MY_MEMORY = (
    "---\nname: pulled-memory\nkind: memory\nscope: global\n"
    "description: A memory pulled from a shared remote.\ntargets: [claude]\n"
    f"priority: high\ndisplay_name: Pulled memory\n---\n{_MEMORY_MARKER} body.\n"
)


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    shutil.copytree(_REPO / "canonical", src / "canonical")
    return src


def _my_overlay(tmp_path: Path) -> Path:
    """A my-office overlay (with sibling state/) holding a gated hook + memory."""
    my = tmp_path / "home" / ".cohort" / "my"
    (my.parent / "state").mkdir(parents=True)
    for sub, name, text in (
        ("hooks", "pulled-hook", _MY_HOOK),
        ("memories", "pulled-memory", _MY_MEMORY),
    ):
        d = my / "canonical" / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    return my


def _placed_text(result) -> str:
    return "\n".join(s.content.decode() for s in result.staged)


def test_unquarantined_my_hook_and_memory_place_normally(tmp_path):
    src, my = _source(tmp_path), _my_overlay(tmp_path)
    result = compile_ide(src, "claude", scope="global", overlay=my)
    # no pending records → nothing withheld; the pulled artifacts take effect
    assert result.withheld == []
    text = _placed_text(result)
    assert _HOOK_MARKER in text and _MEMORY_MARKER in text


def test_quarantined_artifacts_are_withheld_from_every_compile(tmp_path):
    src, my = _source(tmp_path), _my_overlay(tmp_path)
    state = my.parent / "state"
    hook = my / "canonical" / "hooks" / "pulled-hook.md"
    mem = my / "canonical" / "memories" / "pulled-memory.md"
    q.add_pending(state, [
        q.QuarantinedArtifact("hook", "pulled-hook", q.content_hash(hook), "t"),
        q.QuarantinedArtifact("memory", "pulled-memory", q.content_hash(mem), "t"),
    ])
    # derived from overlay's sibling state/ — no caller wiring needed
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == ["hook pulled-hook", "memory pulled-memory"]
    text = _placed_text(result)
    assert _HOOK_MARKER not in text  # the hook's action never reaches settings.json
    assert _MEMORY_MARKER not in text  # the memory never reaches the loaded corpus


def test_withhold_is_content_pinned_edit_bypasses_stale_record(tmp_path):
    # A record pins bytes: if the on-disk artifact no longer matches, it is not the
    # reviewed-and-refused content, so it is NOT withheld (a local edit re-activates).
    src, my = _source(tmp_path), _my_overlay(tmp_path)
    state = my.parent / "state"
    q.add_pending(state, [q.QuarantinedArtifact("hook", "pulled-hook", "STALEHASH", "t")])
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == []  # hash mismatch → not this record's bytes


def test_approve_lets_the_artifact_through_next_compile(tmp_path):
    src, my = _source(tmp_path), _my_overlay(tmp_path)
    state = my.parent / "state"
    hook = my / "canonical" / "hooks" / "pulled-hook.md"
    q.add_pending(state, [q.QuarantinedArtifact("hook", "pulled-hook", q.content_hash(hook), "t")])
    assert compile_ide(src, "claude", scope="global", overlay=my).withheld == ["hook pulled-hook"]
    q.approve(state, ["pulled-hook"])
    assert compile_ide(src, "claude", scope="global", overlay=my).withheld == []


def test_misfiled_hook_is_still_gated_by_frontmatter(tmp_path):
    # The bypass the review caught: a hook in the AGENTS directory renders as a hook
    # (its action reaches settings.json), so it must be quarantinable and withheld.
    src = _source(tmp_path)
    my = tmp_path / "home" / ".cohort" / "my"
    (my.parent / "state").mkdir(parents=True)
    d = my / "canonical" / "agents"
    d.mkdir(parents=True)
    (d / "evil.md").write_text(
        "---\nname: evil\nkind: hook\nscope: global\ndescription: rce.\n"
        f"targets: [claude]\nevent: session_start\naction: {_HOOK_MARKER}\n---\nbody\n",
        encoding="utf-8",
    )
    state = my.parent / "state"
    q.add_pending(state, [
        q.QuarantinedArtifact("hook", "evil", q.content_hash(d / "evil.md"), "t"),
    ])
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == ["hook evil"]
    assert _HOOK_MARKER not in _placed_text(result)  # never reaches settings.json


# --- F1: skill/agent are gated sinks equal to hook/memory --------------------

_SKILL_MARKER = "PULLED-SKILL-MARKER-XYZ"
_AGENT_MARKER = "PULLED-AGENT-MARKER-XYZ"
_MY_SKILL = (
    "---\nname: pulled-skill\nkind: skill\nscope: global\n"
    "description: A skill pulled from a shared remote.\ntargets: [claude]\n"
    f"---\n{_SKILL_MARKER} body.\n"
)
_MY_AGENT = (
    "---\nname: pulled-agent\nkind: agent\nscope: global\n"
    "description: An agent pulled from a shared remote.\ntargets: [claude]\n"
    "department: Ops\ntopology: specialist\nadvisory: true\ntools: [read]\n"
    f"display_name: PulledAgent\n---\n{_AGENT_MARKER} body.\n"
)


def _my_overlay_skill_agent(tmp_path: Path) -> Path:
    """A my-office overlay holding a gated skill + agent (the F1 sinks)."""
    my = tmp_path / "home" / ".cohort" / "my"
    (my.parent / "state").mkdir(parents=True)
    for sub, name, text in (
        ("skills", "pulled-skill", _MY_SKILL),
        ("agents", "pulled-agent", _MY_AGENT),
    ):
        d = my / "canonical" / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    return my


def test_synced_skill_and_agent_are_recorded_as_gated(tmp_path):
    # A synced SKILL's description auto-loads into every session and its body is
    # model-invocable; a synced AGENT's description auto-loads too and it is
    # model-spawnable — both are prompt-injection sinks equal to a memory (F1).
    # Verify via the recording functions themselves: gated_identity/all_gated_in
    # classify them as gated, and add_pending durably records them as pending.
    my = _my_overlay_skill_agent(tmp_path)
    state = my.parent / "state"
    skill = my / "canonical" / "skills" / "pulled-skill.md"
    agent = my / "canonical" / "agents" / "pulled-agent.md"

    assert q.gated_identity(skill) == ("skill", "pulled-skill")
    assert q.gated_identity(agent) == ("agent", "pulled-agent")
    gated = q.all_gated_in(my / "canonical")
    assert {(k, n) for k, n, _p in gated} == {("skill", "pulled-skill"), ("agent", "pulled-agent")}

    added = q.add_pending(state, [
        q.QuarantinedArtifact("skill", "pulled-skill", q.content_hash(skill), "t"),
        q.QuarantinedArtifact("agent", "pulled-agent", q.content_hash(agent), "t"),
    ])
    assert {a.name for a in added} == {"pulled-skill", "pulled-agent"}
    assert q.pending_keys(state) == {
        ("skill", "pulled-skill", q.content_hash(skill)),
        ("agent", "pulled-agent", q.content_hash(agent)),
    }


def test_synced_skill_and_agent_are_withheld_from_compile(tmp_path):
    # Full-pipeline confirmation: GATED_KINDS is the single source consumed by
    # compile's withhold, so a pending skill/agent record must never reach the
    # staged output (the skill's description would auto-load; the agent would
    # become model-spawnable).
    src, my = _source(tmp_path), _my_overlay_skill_agent(tmp_path)
    state = my.parent / "state"
    skill = my / "canonical" / "skills" / "pulled-skill.md"
    agent = my / "canonical" / "agents" / "pulled-agent.md"
    q.add_pending(state, [
        q.QuarantinedArtifact("skill", "pulled-skill", q.content_hash(skill), "t"),
        q.QuarantinedArtifact("agent", "pulled-agent", q.content_hash(agent), "t"),
    ])
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == ["agent pulled-agent", "skill pulled-skill"]
    text = _placed_text(result)
    assert _SKILL_MARKER not in text
    assert _AGENT_MARKER not in text


def test_unquarantined_synced_skill_and_agent_place_normally(tmp_path):
    # Without a pending record, a synced skill/agent compiles like any other
    # artifact — gating only withholds what is actually recorded as pending.
    src, my = _source(tmp_path), _my_overlay_skill_agent(tmp_path)
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == []
    text = _placed_text(result)
    assert _SKILL_MARKER in text
    assert _AGENT_MARKER in text


def test_corrupt_state_fails_closed_withholding_every_gated(tmp_path):
    # A corrupt quarantine file must not read as "nothing pending" — every gated
    # my-layer artifact is withheld until the state is repaired.
    src, my = _source(tmp_path), _my_overlay(tmp_path)
    (my.parent / "state" / "quarantine.json").write_text("{ truncated", encoding="utf-8")
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == ["hook pulled-hook", "memory pulled-memory"]
    text = _placed_text(result)
    assert _HOOK_MARKER not in text and _MEMORY_MARKER not in text


# --- F3: office/source-layer quarantine --------------------------------------
#
# The threat: `cohort update` fast-forwards the office SOURCE; on a shared office
# remote an update pull can introduce a gated artifact (hook/memory/skill/agent)
# that a recompile auto-activates with no review. These tests exercise the office
# store + compile's office withhold gate directly (the recorder wiring into
# `cohort update` is the documented residual gap in quarantine.py).

_OFFICE_HOOK_MARKER = "cohort office-evil-pulled-action"
_OFFICE_HOOK = (
    "---\nname: office-hook\nkind: hook\nscope: global\n"
    "description: A hook introduced by an office update pull.\ntargets: [claude]\n"
    f"event: session_start\naction: {_OFFICE_HOOK_MARKER}\n---\nHook body.\n"
)


def _office_source_with_new_hook(tmp_path: Path) -> tuple[Path, Path]:
    """A full office source (real canonical) plus one NEW gated hook, and its path."""
    src = _source(tmp_path)
    d = src / "canonical" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    hook = d / "office-hook.md"
    hook.write_text(_OFFICE_HOOK, encoding="utf-8")
    return src, hook


def _state_dir(tmp_path: Path) -> Path:
    # compile derives the office store from `overlay.parent / "state"`, so the overlay
    # sits at <home>/.cohort/my and the store at <home>/.cohort/state.
    my = tmp_path / "home" / ".cohort" / "my"
    (my.parent / "state").mkdir(parents=True)
    return my.parent / "state"


def test_office_pending_absent_is_empty(tmp_path):
    state = _state(tmp_path)
    assert q.load_office_pending(state) == []
    assert q.office_pending_keys(state) == set()
    assert q.load_office_baseline(state) is None  # no baseline yet ⇒ first install


def test_first_install_establishes_baseline_and_quarantines_nothing(tmp_path):
    # The shipped office (baseline absent) must NOT be quarantined wholesale on the
    # first compile/update — only later-introduced deltas are.
    state = _state_dir(tmp_path)
    src, _hook = _office_source_with_new_hook(tmp_path)
    added = q.record_office_delta(state, src)
    assert added == []  # nothing quarantined on first install
    baseline = q.load_office_baseline(state)
    assert baseline is not None
    assert ("hook", "office-hook", q.content_hash(src / "canonical" / "hooks" / "office-hook.md")) in baseline


def test_update_pull_quarantines_only_the_new_office_gated_artifact(tmp_path):
    # Establish a baseline WITHOUT the new hook (simulating the office before the
    # update), then introduce the hook and record the delta: only it is quarantined.
    state = _state_dir(tmp_path)
    src = _source(tmp_path)
    assert q.record_office_delta(state, src) == []  # baseline = shipped office
    # the update pull adds a new gated hook
    d = src / "canonical" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    (d / "office-hook.md").write_text(_OFFICE_HOOK, encoding="utf-8")
    added = q.record_office_delta(state, src)
    assert [(a.kind, a.name) for a in added] == [("hook", "office-hook")]
    assert q.office_pending_keys(state) == {
        ("hook", "office-hook", q.content_hash(d / "office-hook.md"))
    }


def test_office_withhold_holds_back_the_pending_office_hook_at_compile(tmp_path):
    # End-to-end: a recorded office pending hook is withheld from the staged output,
    # so its action never reaches settings.json on recompile.
    state = _state_dir(tmp_path)
    src, hook = _office_source_with_new_hook(tmp_path)
    my = state.parent / "my"  # overlay so compile derives the state dir (no my/canonical)
    q.record_office_delta(state, src)  # first: baseline incl. the hook → not withheld
    # Force the hook to be treated as newly-introduced: drop it from the baseline.
    baseline = q.load_office_baseline(state)
    baseline.discard(("hook", "office-hook", q.content_hash(hook)))
    q._save_office_baseline(state, baseline)
    q.record_office_delta(state, src)  # now records it as delta → pending

    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert "hook office-hook" in result.withheld
    assert _OFFICE_HOOK_MARKER not in _placed_text(result)


def test_office_withhold_is_a_noop_on_first_install(tmp_path):
    # With no office store populated (fresh clone), the shipped office compiles
    # normally — the gate must never withhold the whole shipped office.
    state = _state_dir(tmp_path)
    src, hook = _office_source_with_new_hook(tmp_path)
    my = state.parent / "my"
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == []
    assert _OFFICE_HOOK_MARKER in _placed_text(result)  # the office hook DOES place


def test_explicit_office_withhold_set_is_honored(tmp_path):
    # The hermetic path: pass office_withhold directly (like the my-layer `withhold`).
    src, hook = _office_source_with_new_hook(tmp_path)
    my = tmp_path / "home" / ".cohort" / "my"
    my.mkdir(parents=True)
    ident = ("hook", "office-hook", q.content_hash(hook))
    result = compile_ide(src, "claude", scope="global", overlay=my, office_withhold={ident})
    assert "hook office-hook" in result.withheld
    assert _OFFICE_HOOK_MARKER not in _placed_text(result)


def test_office_withhold_is_content_pinned(tmp_path):
    # A stale office pending record (wrong bytes) does NOT withhold — the on-disk
    # artifact is not the reviewed-and-refused content.
    src, hook = _office_source_with_new_hook(tmp_path)
    my = tmp_path / "home" / ".cohort" / "my"
    my.mkdir(parents=True)
    result = compile_ide(
        src, "claude", scope="global", overlay=my,
        office_withhold={("hook", "office-hook", "STALEHASH")},
    )
    assert result.withheld == []
    assert _OFFICE_HOOK_MARKER in _placed_text(result)


def test_corrupt_office_store_fails_closed(tmp_path):
    # A corrupt office store must not read as "nothing pending": every gated office
    # artifact is withheld until it is repaired (the shipped office goes dark — the
    # safe direction).
    state = _state_dir(tmp_path)
    src, _hook = _office_source_with_new_hook(tmp_path)
    my = state.parent / "my"
    (state / "office_quarantine.json").write_text("{ truncated", encoding="utf-8")
    result = compile_ide(src, "claude", scope="global", overlay=my)
    # every gated office artifact (agents/skills/memories/hooks) is held back
    assert "hook office-hook" in result.withheld
    assert _OFFICE_HOOK_MARKER not in _placed_text(result)


def test_approve_office_lets_the_artifact_through_next_record(tmp_path):
    # Approving clears the office pending record (compile stops withholding); the
    # identity stays in the baseline, so it is not re-quarantined on the next pull.
    state = _state_dir(tmp_path)
    src, hook = _office_source_with_new_hook(tmp_path)
    my = state.parent / "my"
    q.record_office_delta(state, src)
    baseline = q.load_office_baseline(state)
    baseline.discard(("hook", "office-hook", q.content_hash(hook)))
    q._save_office_baseline(state, baseline)
    q.record_office_delta(state, src)
    assert "hook office-hook" in compile_ide(src, "claude", scope="global", overlay=my).withheld

    cleared = q.approve_office(state, ["office-hook"])
    assert cleared == ["office-hook"]
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert "hook office-hook" not in result.withheld
    assert _OFFICE_HOOK_MARKER in _placed_text(result)
    # not re-quarantined on a subsequent pull (identity is in the baseline)
    assert q.record_office_delta(state, src) == []


def test_office_store_is_separate_from_my_reconcile(tmp_path):
    # The office records must survive a my-office `reconcile` (which prunes against
    # my/canonical): they live in a separate store keyed on the office tree.
    state = _state_dir(tmp_path)
    src, hook = _office_source_with_new_hook(tmp_path)
    my = state.parent / "my"
    (my / "canonical").mkdir(parents=True)  # empty my overlay
    q._save_office_pending(
        state, [q.QuarantinedArtifact("hook", "office-hook", q.content_hash(hook), "t")]
    )
    q.reconcile(state, my)  # my-office reconcile — must NOT touch the office store
    assert q.office_pending_keys(state) == {("hook", "office-hook", q.content_hash(hook))}


def test_project_tier_compile_has_no_office_withhold(tmp_path):
    # No overlay ⇒ no state dir ⇒ the office gate never runs (project tier / hermetic).
    src, _hook = _office_source_with_new_hook(tmp_path)
    result = compile_ide(src, "claude", scope="global")  # no overlay
    assert result.withheld == []
    assert _OFFICE_HOOK_MARKER in _placed_text(result)

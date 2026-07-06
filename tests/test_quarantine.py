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
    # A hook MISFILED in the agents directory is still a hook by frontmatter.
    misfiled = _art_file(tmp_path / "agents" / "evil.md", kind="hook", name="evil",
                         extra="event: session_start\naction: cohort rce\n")
    assert q.gated_identity(hook) == ("hook", "on-start")
    assert q.gated_identity(mem) == ("memory", "ctx")
    assert q.gated_identity(agent) is None  # agents are not gated
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


def test_approve_by_name_clears_all_hashes_of_that_name(tmp_path):
    state = _state(tmp_path)
    q.add_pending(state, [_art(name="a", h="1"), _art(name="a", h="2"), _art(name="b", h="3")])
    cleared = q.approve(state, ["a"])
    assert cleared == ["a"]
    assert q.pending_keys(state) == {("hook", "b", "3")}


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


def test_corrupt_state_fails_closed_withholding_every_gated(tmp_path):
    # A corrupt quarantine file must not read as "nothing pending" — every gated
    # my-layer artifact is withheld until the state is repaired.
    src, my = _source(tmp_path), _my_overlay(tmp_path)
    (my.parent / "state" / "quarantine.json").write_text("{ truncated", encoding="utf-8")
    result = compile_ide(src, "claude", scope="global", overlay=my)
    assert result.withheld == ["hook pulled-hook", "memory pulled-memory"]
    text = _placed_text(result)
    assert _HOOK_MARKER not in text and _MEMORY_MARKER not in text

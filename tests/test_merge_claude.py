"""P2-T3: aggregating kinds (hook→settings.json, memory→CLAUDE.md) + merge op.

Unit tests cover the merge primitives directly; behavioral/integration tests
exercise the data-safe round-trip via the real CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort import merge
from cohort.executor import classify
from cohort.install_model import Op, OpStatus, OpType

REPO_ROOT = Path(__file__).resolve().parents[1]
PHASE2_SRC = REPO_ROOT / "tests" / "fixtures" / "phase2"
USER = PHASE2_SRC / "user"
GOLDEN = REPO_ROOT / "tests" / "golden" / "claude"


def run_cli(*args, home, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("COHORT_SOURCE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], capture_output=True, text=True, env=env
    )


@pytest.fixture
def home_with_user_files(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text(
        (USER / "CLAUDE.md").read_text(), encoding="utf-8"
    )
    (home / ".claude" / "settings.json").write_text(
        (USER / "settings.json").read_text(), encoding="utf-8"
    )
    return home


def recompile(home):
    return run_cli("recompile", "--ide", "claude", "--source", str(PHASE2_SRC), home=home)


# --- unit: managed-block ----------------------------------------------------


def test_block_insert_then_extract_round_trips():
    text = "user content\n"
    merged = merge.upsert_block(text, "@cohort/x.md")
    assert "user content" in merged
    assert merge.extract_block(merged) == "@cohort/x.md"


def test_block_replace_only_touches_block():
    text = merge.upsert_block("user\n", "v1")
    text2 = merge.upsert_block(text, "v2")
    assert merge.extract_block(text2) == "v2"
    assert text2.startswith("user")


def test_block_remove_restores_user_content():
    text = merge.upsert_block("user content\n", "@x")
    assert merge.remove_block(text).strip() == "user content"


def test_block_insert_into_empty_creates_just_block():
    text = merge.upsert_block("", "@x")
    assert merge.extract_block(text) == "@x"
    assert merge.remove_block(text) == ""


# --- unit: json key-merge ---------------------------------------------------


def test_json_merge_appends_and_preserves_user_keys():
    existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}, "theme": "dark"}
    fragment = {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}]}}
    new, added = merge.merge_hooks(existing, fragment)
    assert new["theme"] == "dark"
    assert len(new["hooks"]["PreToolUse"]) == 2  # appended, not replaced
    assert len(added) == 1


def test_json_merge_is_idempotent():
    existing = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "x"}]}]}}
    fragment = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "x"}]}]}}
    new, added = merge.merge_hooks(existing, fragment)
    assert new == existing
    assert added == []


def test_json_remove_tagged_skips_altered_entry():
    entry = {"matcher": "", "hooks": [{"type": "command", "command": "x"}]}
    existing = {"hooks": {"SessionStart": [entry]}}
    tag = {"event": "SessionStart", "entry_hash": merge.entry_hash(entry)}
    # unaltered → removed
    _new, removed, skipped = merge.remove_tagged(existing, [tag])
    assert removed == 1 and skipped == 0
    # altered → skipped (ownership re-verify)
    altered = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "EDITED"}]}]}}
    _n2, rem2, skip2 = merge.remove_tagged(altered, [tag])
    assert rem2 == 0 and skip2 == 1


# --- unit: merge classify ---------------------------------------------------


def test_merge_classify_noop_vs_apply(tmp_path):
    payload = tmp_path / "block.txt"
    payload.write_text("@cohort/x.md", encoding="utf-8")
    dest = tmp_path / "CLAUDE.md"
    op = Op(OpType.MERGE.value, "claude", str(dest), src=str(payload), strategy="block")
    assert classify(op, {}) == OpStatus.APPLY  # file absent → apply
    dest.write_text(merge.upsert_block("u\n", "@cohort/x.md"), encoding="utf-8")
    assert classify(op, {}) == OpStatus.SATISFIED  # already merged → no-op
    # merge is never a clobber
    dest.write_text("totally foreign content\n", encoding="utf-8")
    assert classify(op, {}) == OpStatus.APPLY


# --- behavioral: merged outputs match golden -------------------------------


def test_merged_files_match_golden(home_with_user_files):
    proc = recompile(home_with_user_files)
    assert proc.returncode == 0, proc.stderr
    claude = home_with_user_files / ".claude"
    assert (claude / "CLAUDE.md").read_text() == (GOLDEN / "merged" / "CLAUDE.md").read_text()
    assert (claude / "settings.json").read_text() == (GOLDEN / "merged" / "settings.json").read_text()
    assert (claude / "cohort" / "CLAUDE.cohort.md").read_bytes() == (
        GOLDEN / "cohort" / "CLAUDE.cohort.md"
    ).read_bytes()


def test_user_content_preserved_in_both_files(home_with_user_files):
    recompile(home_with_user_files)
    claude = home_with_user_files / ".claude"
    assert "My personal memory" in (claude / "CLAUDE.md").read_text()
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["theme"] == "dark"
    assert any(
        e["hooks"][0]["command"] == "echo my-own-guard" for e in settings["hooks"]["PreToolUse"]
    )


def test_merge_region_is_byte_stable_across_recompiles(home_with_user_files):
    recompile(home_with_user_files)
    first = (home_with_user_files / ".claude" / "settings.json").read_text()
    recompile(home_with_user_files)
    second = (home_with_user_files / ".claude" / "settings.json").read_text()
    assert first == second


# --- behavioral: reverse, ownership-checked --------------------------------


def test_uninstall_restores_user_files(home_with_user_files):
    recompile(home_with_user_files)
    run_cli("uninstall", home=home_with_user_files)
    claude = home_with_user_files / ".claude"
    assert (claude / "CLAUDE.md").read_text() == (USER / "CLAUDE.md").read_text()
    assert json.loads((claude / "settings.json").read_text()) == json.loads(
        (USER / "settings.json").read_text()
    )


def test_merge_created_file_is_removed_on_uninstall(tmp_path):
    # no pre-existing CLAUDE.md / settings.json → merge creates them
    home = tmp_path / "home"
    home.mkdir()
    recompile(home)
    claude = home / ".claude"
    assert (claude / "CLAUDE.md").exists()
    assert (claude / "settings.json").exists()
    run_cli("uninstall", home=home)
    assert not (claude / "CLAUDE.md").exists()  # created → removed
    assert not (claude / "settings.json").exists()


def test_user_divergence_inside_block_is_skipped(home_with_user_files):
    recompile(home_with_user_files)
    claude_md = home_with_user_files / ".claude" / "CLAUDE.md"
    # user edits *inside* the managed block
    text = claude_md.read_text().replace("@cohort/CLAUDE.cohort.md", "@cohort/CLAUDE.cohort.md\nMINE")
    claude_md.write_text(text, encoding="utf-8")
    proc = run_cli("uninstall", "--json", home=home_with_user_files)
    assert proc.returncode == 0
    # block preserved (their edit not destroyed)
    assert "MINE" in claude_md.read_text()


def test_user_divergence_in_settings_entry_is_skipped(home_with_user_files):
    recompile(home_with_user_files)
    settings_path = home_with_user_files / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    # user alters Cohort's SessionStart command
    settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "EDITED BY USER"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    run_cli("uninstall", home=home_with_user_files)
    after = json.loads(settings_path.read_text())
    # the altered entry is left intact (ownership re-verify skipped it)
    assert after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "EDITED BY USER"


# --- integration: full round-trip ------------------------------------------


def test_end_to_end_merge_round_trip(home_with_user_files):
    assert recompile(home_with_user_files).returncode == 0
    second = recompile(home_with_user_files)
    assert "applied: 0" in second.stdout  # idempotent no-op
    assert run_cli("uninstall", home=home_with_user_files).returncode == 0
    claude = home_with_user_files / ".claude"
    assert (claude / "CLAUDE.md").read_text() == (USER / "CLAUDE.md").read_text()
    assert not (home_with_user_files / ".cohort").exists()  # staging gone too

"""Copilot CLI renderer: IR reuse, byte-stability, advisory strip, memory merge, parity.

Mirrors the Codex/Cursor coverage in ``test_phase7.py`` — see that file's module
docstring for the phase-7 invariant-gate rationale this suite extends to the
fourth renderer added 2026-07-24.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.compile import RENDERERS, compile_ide, scan_staging_ops, staging_tree_hash, write_staging
from cohort.install_model import CohortPaths
from cohort.parity import check_parity, load_gaps

COHORT_SRC = Path(__file__).resolve().parents[1]
PHASE2_SRC = COHORT_SRC / "tests" / "fixtures" / "phase2"  # has all five kinds


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
    env.pop("COHORT_SOURCE", None)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


# === renderer consumes the same IR, filters targets =========================


def test_renderer_consumes_same_ir_and_filters_targets():
    result = compile_ide(PHASE2_SRC, "copilot")
    names = {Path(s.staged_rel).stem for s in result.staged}
    # weekly-report targets [claude] only → excluded from copilot
    assert "weekly-report" not in names
    # agents target [all] → present
    assert any("security-engineer" in s.staged_rel for s in result.staged)


def test_renderer_is_byte_stable(home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "copilot"))
    h1 = staging_tree_hash(paths, "copilot")
    write_staging(paths, compile_ide(PHASE2_SRC, "copilot"))
    assert staging_tree_hash(paths, "copilot") == h1 and h1 != ""


def test_ops_are_tagged_with_ide(home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "copilot"))
    ops = scan_staging_ops(paths, "copilot", "link")
    assert ops and all(o.ide == "copilot" for o in ops)


# === agent rendering: tools alias list + advisory strip ======================


def test_copilot_agent_renders_tools_alias_list():
    result = compile_ide(PHASE2_SRC, "copilot")
    agent = next(s for s in result.staged if s.staged_rel.endswith("security-engineer.md"))
    text = agent.content.decode()
    assert "agents/" in agent.staged_rel
    assert "name: security-engineer" in text
    # advisory enforced mechanically: fixture requests [read, grep, write, bash]
    # but is not a project-scoped doer, so only the read-only aliases survive.
    assert "tools:" in text
    assert "- edit" not in text
    assert "- execute" not in text
    assert "- read" in text
    assert "- search" in text


def test_copilot_project_doer_keeps_write_tools():
    from cohort.adapters.copilot import render_agent
    from cohort.ir import build_ir
    from cohort.loader import load_artifact

    p = PHASE2_SRC / "canonical" / "agents" / "security-engineer.md"
    lr = load_artifact(p)
    ir = build_ir(lr.frontmatter, lr.body, p)
    ir.scope = "project"
    ir.fields["advisory"] = False
    text = render_agent(ir).content.decode()
    assert "- edit" in text
    assert "- execute" in text


# === model tier (#143): omitted gracefully, no compile break ================


def _ir_with_model(tier: str):
    from cohort.ir import build_ir
    from cohort.loader import load_artifact

    p = PHASE2_SRC / "canonical" / "agents" / "security-engineer.md"
    lr = load_artifact(p)
    ir = build_ir(lr.frontmatter, lr.body, p)
    ir.fields["model"] = tier
    return ir


def _has_model_field(text: str) -> bool:
    # field-line check, not substring — the body legitimately says "threat-model".
    return any(ln.startswith("model:") for ln in text.splitlines())


@pytest.mark.parametrize("tier", ["fast", "default", "top"])
def test_copilot_agent_render_omits_model_for_every_tier(tier):
    from cohort.adapters.copilot import render_agent as copilot_render_agent

    text = copilot_render_agent(_ir_with_model(tier)).content.decode()
    assert not _has_model_field(text)


# === hooks: own dedicated file, no merge =====================================


def test_copilot_hooks_render_as_dedicated_file():
    from cohort.adapters.copilot import render_hooks_fragment
    from cohort.ir import build_ir

    ir = build_ir(
        {
            "name": "secret-scan", "kind": "hook", "scope": "global", "description": "d",
            "targets": ["all"], "event": "pre_write", "action": "cohort scan",
        },
        "body",
        None,
    )
    fragment = render_hooks_fragment([ir])
    assert fragment["version"] == 1
    assert fragment["hooks"]["preToolUse"] == [{"type": "command", "command": "cohort scan"}]


# === copilot-instructions.md merge data-safety ([K]/[L]) ====================


def test_copilot_instructions_merge_preserves_user_content(home, tmp_path):
    # pre-existing user copilot-instructions.md at the Copilot dest
    (home / ".copilot").mkdir(parents=True)
    (home / ".copilot" / "copilot-instructions.md").write_text(
        "# my instructions\n- be terse\n", encoding="utf-8"
    )
    proc = run_cli("recompile", "--ide", "copilot", "--source", str(PHASE2_SRC), home=home)
    assert proc.returncode == 0, proc.stderr
    text = (home / ".copilot" / "copilot-instructions.md").read_text()
    assert "my instructions" in text  # user content preserved (K)
    assert "@cohort/copilot-instructions.cohort.md" in text  # Cohort import merged in
    corpus = (home / ".copilot" / "cohort" / "copilot-instructions.cohort.md").read_text()
    assert "Cohort office memories" in corpus  # the imported corpus itself
    # deinit-equivalent: slice uninstall restores the user file
    run_cli("uninstall", "--ide", "copilot", home=home)
    after = (home / ".copilot" / "copilot-instructions.md").read_text()
    assert "my instructions" in after and "@cohort/copilot-instructions.cohort.md" not in after


# === parity ==================================================================


def test_parity_passes_with_declared_gap():
    # Copilot over the all-kinds fixture: command is a declared gap → parity ok
    result = check_parity(PHASE2_SRC, "copilot", RENDERERS)
    assert result.ok
    assert "command" in result.declared_gaps


def test_real_copilot_gap_file_loads():
    assert load_gaps("copilot") == {"command": load_gaps("copilot")["command"]}
    assert "command" in load_gaps("copilot")


# === multi-IDE install alongside claude/codex/cursor =========================


def test_multi_ide_install_with_copilot(home):
    proc = run_cli(
        "recompile", "--ide", "claude,codex,cursor,copilot", "--source", str(COHORT_SRC), home=home
    )
    assert proc.returncode == 0, proc.stderr
    assert len(list((home / ".copilot" / "agents").glob("*.md"))) == 17


def test_slice_uninstall_removes_copilot_only(home):
    run_cli("recompile", "--ide", "claude,copilot", "--source", str(COHORT_SRC), home=home)
    assert run_cli("uninstall", "--ide", "copilot", home=home).returncode == 0
    assert not (home / ".copilot" / "agents").exists()  # copilot layer removed
    assert (home / ".claude" / "agents" / "counsel.md").exists()  # claude intact

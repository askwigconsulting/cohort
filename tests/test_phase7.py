"""Phase 7: Codex & Cursor adapters + parity (invariant gate).

Concrete Codex/Cursor *bytes* are deferred to the golden-lock sub-gate (after the
field-level ‹verify› pass); these tests cover the invariants buildable now — IR
reuse, byte-stability, merge data-safety, ide-tagged ops, parity coverage logic,
and multi-IDE install/slice-uninstall.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
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


# === P7-T1/T2: Codex & Cursor renderers consume the same IR =================


@pytest.mark.parametrize("ide", ["codex", "cursor"])
def test_renderer_consumes_same_ir_and_filters_targets(ide):
    result = compile_ide(PHASE2_SRC, ide)
    names = {Path(s.staged_rel).stem for s in result.staged}
    # weekly-report targets [claude] only → excluded from codex/cursor
    assert "weekly-report" not in names
    # agents target [all] → present
    assert any("security-engineer" in s.staged_rel for s in result.staged)


@pytest.mark.parametrize("ide", ["codex", "cursor"])
def test_renderer_is_byte_stable(ide, home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, ide))
    h1 = staging_tree_hash(paths, ide)
    write_staging(paths, compile_ide(PHASE2_SRC, ide))
    assert staging_tree_hash(paths, ide) == h1 and h1 != ""


@pytest.mark.parametrize("ide", ["codex", "cursor"])
def test_ops_are_tagged_with_ide(ide, home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, ide))
    ops = scan_staging_ops(paths, ide, "link")
    assert ops and all(o.ide == ide for o in ops)


def test_codex_agent_renders_as_toml():
    result = compile_ide(PHASE2_SRC, "codex")
    agent = next(s for s in result.staged if s.staged_rel.endswith("security-engineer.toml"))
    text = agent.content.decode()
    assert text.startswith("name = ")
    assert ".codex/agents/" in agent.staged_rel
    # advisory enforced mechanically, not prose-only (sandbox_mode read-only)
    assert 'sandbox_mode = "read-only"' in text


def test_cursor_agent_advisory_readonly():
    result = compile_ide(PHASE2_SRC, "cursor")
    agent = next(s for s in result.staged if s.staged_rel.endswith("agents/security-engineer.md"))
    assert "readonly: true" in agent.content.decode()


# === model tier (#143): Codex/Cursor omit it gracefully, no compile break ===


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
    return any(
        ln.startswith("model =") or ln.startswith("model:") for ln in text.splitlines()
    )


@pytest.mark.parametrize("tier", ["fast", "default", "top"])
def test_codex_agent_render_omits_model_for_every_tier(tier):
    from cohort.adapters.codex import render_agent as codex_render_agent

    text = codex_render_agent(_ir_with_model(tier)).content.decode()
    assert not _has_model_field(text)


@pytest.mark.parametrize("tier", ["fast", "default", "top"])
def test_cursor_agent_render_omits_model_for_every_tier(tier):
    from cohort.adapters.cursor import render_agent as cursor_render_agent

    text = cursor_render_agent(_ir_with_model(tier)).content.decode()
    assert not _has_model_field(text)


# === P7-T1: Codex AGENTS.md merge data-safety ([K]/[L]) =====================


def test_codex_agents_md_merge_preserves_user_content(home, tmp_path):
    # pre-existing user AGENTS.md at the Codex dest
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("# my instructions\n- be terse\n", encoding="utf-8")
    proc = run_cli("recompile", "--ide", "codex", "--source", str(PHASE2_SRC), home=home)
    assert proc.returncode == 0, proc.stderr
    text = (home / ".codex" / "AGENTS.md").read_text()
    assert "my instructions" in text  # user content preserved (K)
    assert "Cohort office memories" in text  # Cohort block merged in
    # deinit-equivalent: slice uninstall restores the user file
    run_cli("uninstall", "--ide", "codex", home=home)
    after = (home / ".codex" / "AGENTS.md").read_text()
    assert "my instructions" in after and "Cohort office memories" not in after


# === P7-T3: parity coverage logic ===========================================


@dataclass
class _StubRenderer:
    ide: str
    supported_kinds: frozenset

    def matches(self, ir):
        return ir.targets_ide(self.ide)


def test_parity_passes_with_declared_gap():
    # Codex over the all-kinds fixture: command is a declared gap → parity ok
    result = check_parity(PHASE2_SRC, "codex", RENDERERS)
    assert result.ok
    assert "command" in result.declared_gaps


def test_parity_fails_on_undeclared_gap(monkeypatch, tmp_path):
    monkeypatch.setenv("COHORT_ADAPTERS_DIR", str(tmp_path))  # no gap files
    stub = _StubRenderer(ide="codex", supported_kinds=frozenset({"agent"}))
    result = check_parity(PHASE2_SRC, "codex", {"codex": stub})
    assert not result.ok
    assert "memory" in result.undeclared  # present, not rendered, not declared


def test_parity_fails_on_stale_declaration(monkeypatch, tmp_path):
    gaps_dir = tmp_path / "codex"
    gaps_dir.mkdir(parents=True)
    (gaps_dir / "parity-gaps.toml").write_text(
        '[[gaps]]\nkind = "agent"\nreason = "stale: agent actually renders"\n', encoding="utf-8"
    )
    monkeypatch.setenv("COHORT_ADAPTERS_DIR", str(tmp_path))
    result = check_parity(PHASE2_SRC, "codex", RENDERERS)
    assert not result.ok
    assert "agent" in result.stale  # declared gap that the renderer renders


def test_real_codex_gap_file_loads():
    assert load_gaps("codex") == {"command": load_gaps("codex")["command"]}
    assert "command" in load_gaps("codex")


# === P7-T3: multi-IDE install + slice uninstall =============================


def test_multi_ide_install_three_offices(home):
    proc = run_cli("recompile", "--ide", "claude,codex,cursor", "--source", str(COHORT_SRC), home=home)
    assert proc.returncode == 0, proc.stderr
    assert len(list((home / ".claude" / "agents").glob("*.md"))) == 17
    assert len(list((home / ".codex" / "agents").glob("*.toml"))) == 17
    assert len(list((home / ".cursor" / "agents").glob("*.md"))) == 17


def test_recompile_multi_ide_byte_stable(home):
    run_cli("recompile", "--ide", "claude,codex,cursor", "--source", str(COHORT_SRC), home=home)
    second = run_cli("recompile", "--ide", "claude,codex,cursor", "--source", str(COHORT_SRC), home=home)
    assert "applied: 0" in second.stdout  # idempotent across all three


def test_slice_uninstall_removes_one_ide(home):
    run_cli("recompile", "--ide", "claude,codex,cursor", "--source", str(COHORT_SRC), home=home)
    assert run_cli("uninstall", "--ide", "codex", home=home).returncode == 0
    assert not (home / ".codex" / "agents").exists()  # codex layer removed
    assert (home / ".claude" / "agents" / "counsel.md").exists()  # claude intact
    assert (home / ".cursor" / "agents" / "counsel.md").exists()  # cursor intact


def test_parity_green_for_all_three_real_canonical():
    for ide in ("claude", "codex", "cursor"):
        result = check_parity(COHORT_SRC, ide, RENDERERS)
        assert result.ok, result.to_dict()

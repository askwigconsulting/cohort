"""P2-T1 + P2-T2: compile pipeline (IR + staging) and the 1:1 Claude renderer.

Behavioral + integration tests over the Phase 2 fixture roster and checked-in
golden outputs; unit tests cover IR/render/ops directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.adapters.claude import ClaudeRenderer, claude_tools, render_agent
from cohort.compile import compile_ide, scan_staging_ops, staging_tree_hash, write_staging
from cohort.install_model import CohortPaths
from cohort.ir import build_ir
from cohort.loader import load_artifact

REPO_ROOT = Path(__file__).resolve().parents[1]
PHASE2_SRC = REPO_ROOT / "tests" / "fixtures" / "phase2"
GOLDEN = REPO_ROOT / "tests" / "golden" / "claude"
CANON = PHASE2_SRC / "canonical"


def load_ir(relpath: str):
    p = CANON / relpath
    lr = load_artifact(p)
    return build_ir(lr.frontmatter, lr.body, p)


def run_cli(*args, home, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows: Path.home() reads USERPROFILE, not HOME
    env.pop("COHORT_SOURCE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], capture_output=True, text=True, env=env
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


# --- P2-T1: IR --------------------------------------------------------------


def test_ir_preserves_fields_and_applies_defaults():
    ir = load_ir("agents/security-engineer.md")
    assert ir.kind == "agent"
    assert ir.name == "security-engineer"
    assert ir.display_name == "SecurityEngineer"
    assert ir.version == "0.1.0"  # default applied
    assert ir.fields["department"] == "Security"
    assert ir.fields["advisory"] is True
    assert ir.fields["tools"] == ["read", "grep", "write", "bash"]


def test_ir_applies_kind_defaults_when_absent():
    ir = load_ir("agents/chief-of-staff.md")
    assert ir.fields["topology"] == "generalist"
    assert ir.fields["tools"] == []  # agent tools default


# --- P2-T1: byte-stability & staging ---------------------------------------


def test_render_is_byte_stable():
    ir = load_ir("agents/security-engineer.md")
    assert render_agent(ir).content == render_agent(ir).content


def test_compile_twice_yields_identical_staging_tree(home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "claude"))
    h1 = staging_tree_hash(paths, "claude")
    write_staging(paths, compile_ide(PHASE2_SRC, "claude"))
    h2 = staging_tree_hash(paths, "claude")
    assert h1 == h2 and h1 != ""


def test_staging_lands_under_cohort_compiled(home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "claude"))
    base = paths.compiled_ide("claude")
    assert (base / "agents" / "security-engineer.md").exists()
    assert (base / "skills" / "weekly-report" / "SKILL.md").exists()
    assert (base / "commands" / "snapshot.md").exists()


def test_compile_dry_run_writes_nothing(home):
    proc = run_cli("compile", "--ide", "claude", "--source", str(PHASE2_SRC), "--dry-run", home=home)
    assert proc.returncode == 0
    assert not (home / ".cohort" / "compiled").exists()
    assert "staged" in proc.stdout


def test_targets_filter_skips_non_claude_artifact():
    renderer = ClaudeRenderer()
    codex_only = load_ir("agents/codex-only.md")
    assert renderer.matches(codex_only) is False
    result = compile_ide(PHASE2_SRC, "claude")
    assert "codex-only" not in [s.staged_rel for s in result.staged]
    assert "codex-only" in result.skipped


# --- P2-T2: golden bytes ----------------------------------------------------

GOLDEN_FILES = [
    "agents/security-engineer.md",
    "agents/chief-of-staff.md",
    "skills/weekly-report/SKILL.md",
    "commands/snapshot.md",
]


@pytest.mark.parametrize("rel", GOLDEN_FILES)
def test_rendered_matches_golden_bytes(rel, home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "claude"))
    produced = (paths.compiled_ide("claude") / rel).read_bytes()
    assert produced == (GOLDEN / rel).read_bytes(), rel


# --- P2-T2: advisory enforcement (R7) --------------------------------------


def test_advisory_agent_strips_mutating_tools():
    ir = load_ir("agents/security-engineer.md")  # requests write, bash
    tools = claude_tools(ir)
    assert "Write" not in tools and "Bash" not in tools
    assert tools == ["Read", "Grep"]


def test_generalist_has_no_execute_capability():
    ir = load_ir("agents/chief-of-staff.md")
    tools = claude_tools(ir)
    assert set(tools).isdisjoint({"Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"})


def test_body_header_preserves_display_name_department_topology():
    ir = load_ir("agents/chief-of-staff.md")
    text = render_agent(ir).content.decode("utf-8")
    assert "ChiefOfStaff" in text
    assert "Orchestration" in text
    assert "generalist" in text


# --- P2-T2: op emission -----------------------------------------------------


def test_emitted_ops_are_link_by_default_tagged_claude(home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "claude"))
    ops = scan_staging_ops(paths, "claude", "link")
    link_ops = [o for o in ops if o.op == "link"]
    assert link_ops
    assert all(o.ide == "claude" for o in ops)
    dests = [o.dest for o in link_ops]
    assert str(home / ".claude" / "agents" / "security-engineer.md") in dests


def test_emitted_ops_are_copy_under_copy_mode(home):
    paths = CohortPaths(home)
    write_staging(paths, compile_ide(PHASE2_SRC, "claude"))
    ops = scan_staging_ops(paths, "claude", "copy")
    assert any(o.op == "copy" for o in ops)
    assert not any(o.op == "link" for o in ops)


# --- P2-T2: integration via the real CLI -----------------------------------


def test_recompile_install_then_idempotent_then_uninstall(home):
    # a pre-existing, unrelated user file in ~/.claude must survive
    (home / ".claude" / "agents").mkdir(parents=True)
    user_file = home / ".claude" / "agents" / "my-own-agent.md"
    user_file.write_text("user owned\n", encoding="utf-8")

    first = run_cli("recompile", "--ide", "claude", "--source", str(PHASE2_SRC), home=home)
    assert first.returncode == 0, first.stderr
    placed = home / ".claude" / "agents" / "security-engineer.md"
    if os.name == "nt":
        assert not placed.is_symlink()  # Windows defaults to copy-mode
    else:
        assert placed.is_symlink()
    assert placed.read_bytes() == (GOLDEN / "agents" / "security-engineer.md").read_bytes()

    second = run_cli("recompile", "--ide", "claude", "--source", str(PHASE2_SRC), home=home)
    assert "applied: 0" in second.stdout  # idempotent, no churn

    un = run_cli("uninstall", "--ide", "claude", home=home)
    assert un.returncode == 0
    assert not placed.exists()  # ours removed
    assert user_file.read_text() == "user owned\n"  # unrelated content intact


def test_compile_json_lists_staged(home):
    proc = run_cli("compile", "--ide", "claude", "--source", str(PHASE2_SRC), "--json", home=home)
    data = json.loads(proc.stdout)
    assert data[0]["action"] == "compile"
    assert "agents/security-engineer.md" in data[0]["staged"]


def test_compile_logs_carry_key_set(home):
    proc = run_cli("compile", "--ide", "claude", "--source", str(PHASE2_SRC), home=home)
    log_lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("component=")]
    assert log_lines
    required = ["component", "action", "scope", "ide", "artifact", "status", "duration_ms"]
    for line in log_lines:
        keys = {p.split("=", 1)[0] for p in line.split(" ")}
        assert all(f in keys for f in required), line

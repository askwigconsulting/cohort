"""P0-T3 ``cohort validate`` CLI: exit codes, duplicates, --json, logs.

Behavioral (REVIEW GATE) + integration tests driving the real entry point via a
subprocess so exit codes and stdout/stderr separation are exercised faithfully.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import INVALID, VALID
from test_kind_schema import EXPECTED_INVALID_CODES

SKILL = """---
name: {name}
kind: skill
scope: {scope}
description: a valid skill
targets: [all]
---
body
"""


def run_validate(
    *args: str, cwd: Path | None = None, pre: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    """Invoke ``cohort [pre...] validate [args...]`` as a subprocess.

    ``pre`` carries global flags (e.g. ``--dry-run``) that must precede the
    subcommand name.
    """
    cmd = [sys.executable, "-m", "cohort", *pre, "validate", *args]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def write_skill(path: Path, name: str, scope: str = "global") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL.format(name=name, scope=scope), encoding="utf-8")


# --- exit codes & summary ---------------------------------------------------


def test_all_valid_tree_exits_0(tmp_path):
    write_skill(tmp_path / "a.md", "a")
    write_skill(tmp_path / "b.md", "b")
    proc = run_validate(str(tmp_path))
    assert proc.returncode == 0
    assert "2 valid, 0 invalid" in proc.stdout


def test_tree_with_one_invalid_exits_1_and_lists_fail_with_code(tmp_path):
    write_skill(tmp_path / "ok.md", "ok")
    shutil.copy(INVALID / "non-advisory-agent.md", tmp_path / "non-advisory-agent.md")
    proc = run_validate(str(tmp_path))
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout
    assert "non-advisory-agent.md" in proc.stdout
    assert "E060_SAFETY_INVARIANT" in proc.stdout


def test_empty_tree_exits_0(tmp_path):
    proc = run_validate(str(tmp_path))
    assert proc.returncode == 0
    assert "0 valid, 0 invalid" in proc.stdout


def test_nonexistent_path_exits_2_without_traceback(tmp_path):
    proc = run_validate(str(tmp_path / "does-not-exist"))
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "error:" in proc.stderr


def test_no_path_defaults_to_canonical(tmp_path):
    write_skill(tmp_path / "canonical" / "a.md", "a")
    proc = run_validate(cwd=tmp_path)
    assert proc.returncode == 0
    assert "1 valid, 0 invalid" in proc.stdout


def test_dry_run_is_a_noop_not_an_error(tmp_path):
    write_skill(tmp_path / "a.md", "a")
    proc = run_validate(str(tmp_path), pre=("--dry-run",))
    assert proc.returncode == 0
    assert "1 valid, 0 invalid" in proc.stdout


# --- duplicates (E080) ------------------------------------------------------


def test_same_kind_name_scope_is_duplicate_on_second(tmp_path):
    write_skill(tmp_path / "dir_a" / "x.md", "x", scope="global")
    write_skill(tmp_path / "dir_b" / "x.md", "x", scope="global")
    proc = run_validate(str(tmp_path), "--json")
    data = json.loads(proc.stdout)
    statuses = {Path(a["path"]).parent.name: a for a in data["artifacts"]}
    dup_codes = [
        e["code"] for a in data["artifacts"] for e in a["errors"] if e["code"] == "E080_DUPLICATE"
    ]
    assert dup_codes == ["E080_DUPLICATE"], data  # exactly one duplicate, on the second
    assert proc.returncode == 1


def test_same_name_different_scope_is_not_duplicate(tmp_path):
    write_skill(tmp_path / "dir_a" / "x.md", "x", scope="global")
    write_skill(tmp_path / "dir_b" / "x.md", "x", scope="project")
    proc = run_validate(str(tmp_path), "--json")
    data = json.loads(proc.stdout)
    assert all(
        e["code"] != "E080_DUPLICATE" for a in data["artifacts"] for e in a["errors"]
    ), data
    assert proc.returncode == 0


# --- --json shape -----------------------------------------------------------


def test_json_output_matches_spec_shape(tmp_path):
    write_skill(tmp_path / "ok.md", "ok")
    shutil.copy(INVALID / "non-advisory-agent.md", tmp_path / "non-advisory-agent.md")
    proc = run_validate(str(tmp_path), "--json")
    data = json.loads(proc.stdout)
    assert set(data) == {"valid", "artifacts", "summary"}
    assert data["valid"] is False
    assert set(data["summary"]) == {"total", "valid", "invalid"}
    assert data["summary"] == {"total": 2, "valid": 1, "invalid": 1}
    for art in data["artifacts"]:
        assert set(art) >= {"path", "kind", "name", "scope", "status", "errors"}
        for err in art["errors"]:
            assert set(err) >= {"code", "field", "message"}


# --- structured logs --------------------------------------------------------


def test_log_lines_contain_required_fields(tmp_path):
    write_skill(tmp_path / "a.md", "a")
    proc = run_validate(str(tmp_path))
    log_lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("component=")]
    assert log_lines
    required = ["component", "action", "scope", "ide", "artifact", "status", "duration_ms"]
    for line in log_lines:
        keys = {pair.split("=", 1)[0] for pair in line.split(" ")}
        assert all(field in keys for field in required), line


# --- end-to-end mixed tree --------------------------------------------------


def test_end_to_end_mixed_tree(tmp_path):
    for p in sorted(VALID.rglob("*.md")):
        shutil.copy(p, tmp_path / p.name)
    for p in sorted(INVALID.glob("*.md")):
        shutil.copy(p, tmp_path / p.name)

    proc = run_validate(str(tmp_path), "--json")
    assert proc.returncode == 1
    data = json.loads(proc.stdout)

    produced = {e["code"] for a in data["artifacts"] for e in a["errors"]}
    expected = set(EXPECTED_INVALID_CODES.values())
    assert expected <= produced, (expected - produced, data)

    # Human output: one OK/FAIL line per file.
    human = run_validate(str(tmp_path))
    file_lines = [
        ln for ln in human.stdout.splitlines() if ln.startswith("OK ") or ln.startswith("FAIL ")
    ]
    total_files = len(list(tmp_path.glob("*.md")))
    assert len(file_lines) == total_files

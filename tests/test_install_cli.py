"""P1-T2: install / uninstall commands & IDE selection.

Behavioral + integration tests drive the real CLI via subprocess on a temp
``$HOME``; unit tests cover selection/source helpers directly.

NOTE: in Phase 1 the per-IDE op set is empty (no adapter), so the slice-uninstall
tests below exercise command/manifest *wiring* over empty IDE slices — the
substantive ide-filter coverage lives in test_executor.py's hand-built plan.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.install import (
    CancelledSelection,
    UsageError,
    merge_ides,
    parse_ide,
    resolve_selection,
)
from cohort.source import SourceUnresolved, resolve_source

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def read_manifest(home: Path) -> dict:
    return json.loads((home / ".cohort" / "state" / "manifest.json").read_text())


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


# --- behavioral: install on clean ------------------------------------------


def test_install_claude_on_clean(home):
    proc = run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 0, proc.stderr
    m = read_manifest(home)
    assert m["ides"] == ["claude"]
    dests = [o["dest"] for o in m["ops"]]
    assert str(home / ".cohort") in dests
    assert str(home / ".cohort" / "state") in dests
    assert str(home / ".cohort" / "canonical") in dests
    # per-IDE op set is empty: every recorded op is global
    assert all(o["ide"] == "global" for o in m["ops"])
    if os.name == "nt":
        assert (home / ".cohort" / "canonical").is_dir()  # copy-mode default on Windows
        assert not (home / ".cohort" / "canonical").is_symlink()
    else:
        assert (home / ".cohort" / "canonical").is_symlink()


def test_install_all_records_three(home):
    proc = run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 0
    assert read_manifest(home)["ides"] == ["claude", "codex", "cursor"]


def test_install_unknown_ide_is_usage_error(home):
    proc = run_cli("install", "--ide", "vscode", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 2
    assert "unknown ide" in proc.stderr


def test_install_unresolvable_source_is_usage_error(home, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = run_cli("install", "--ide", "claude", "--source", str(empty), home=home)
    assert proc.returncode == 2


def test_install_no_ide_non_tty_is_usage_error(home):
    # subprocess stdin is not a TTY → must refuse, not surprise-install
    proc = run_cli("install", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 2
    assert "ide" in proc.stderr.lower()


# --- behavioral: idempotency, additive, mixed-mode -------------------------


def test_reinstall_is_idempotent_noop(home):
    run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 0
    assert "applied: 0" in proc.stdout


def test_additive_install_merges_ides(home):
    run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("install", "--ide", "cursor", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 0
    assert read_manifest(home)["ides"] == ["claude", "cursor"]


def test_additive_copy_does_not_reflip_shared_canonical(home):
    # First install as copy → canonical is a real dir; adding an IDE without
    # --copy keeps it a copy (shared mode fixed by first install; S2).
    run_cli("install", "--ide", "claude", "--copy", "--source", str(REPO_ROOT), home=home)
    assert not (home / ".cohort" / "canonical").is_symlink()
    proc = run_cli("install", "--ide", "cursor", "--source", str(REPO_ROOT), home=home)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".cohort" / "canonical").is_dir()
    assert not (home / ".cohort" / "canonical").is_symlink()
    m = read_manifest(home)
    canon = next(o for o in m["ops"] if Path(o["dest"]).name == "canonical")  # sep-agnostic
    assert canon["op"] == "copy"  # per-op type, not manifest mode, governs


# --- behavioral: uninstall slice / whole -----------------------------------


def test_slice_uninstall_keeps_home_and_other_ides(home):
    run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("uninstall", "--ide", "claude", home=home)
    assert proc.returncode == 0
    m = read_manifest(home)
    assert "claude" not in m["ides"]
    assert (home / ".cohort").exists()  # shared home survives (S4)


def test_slice_uninstall_unrecorded_ide_is_noop(home):
    run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("uninstall", "--ide", "codex", home=home)
    assert proc.returncode == 0
    assert read_manifest(home)["ides"] == ["claude"]


def test_bare_uninstall_removes_everything(home):
    run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("uninstall", home=home)
    assert proc.returncode == 0
    assert not (home / ".cohort").exists()


def test_uninstall_nothing_installed(home):
    proc = run_cli("uninstall", home=home)
    assert proc.returncode == 0
    assert "nothing installed" in proc.stdout


# --- behavioral: dry-run & json --------------------------------------------


def test_install_dry_run_changes_nothing(home):
    proc = run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), "--dry-run", home=home)
    assert proc.returncode == 0
    assert not (home / ".cohort").exists()


def test_uninstall_dry_run_changes_nothing(home):
    run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("uninstall", "--dry-run", home=home)
    assert proc.returncode == 0
    assert (home / ".cohort").exists()  # still there


def test_install_json_shape(home):
    proc = run_cli(
        "install", "--ide", "claude", "--source", str(REPO_ROOT), "--json", home=home
    )
    data = json.loads(proc.stdout)
    assert data["action"] == "install"
    assert data["mode"] == ("copy" if os.name == "nt" else "link")  # copy-mode default on Windows
    assert data["ides"] == ["claude"]
    assert set(data["summary"]) == {"applied", "skipped", "backed_up"}


def test_install_dry_run_json_has_null_install_id(home):
    proc = run_cli(
        "install", "--ide", "claude", "--source", str(REPO_ROOT), "--dry-run", "--json", home=home
    )
    data = json.loads(proc.stdout)
    assert data["install_id"] is None


def test_uninstall_json_shape(home):
    run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home)
    proc = run_cli("uninstall", "--json", home=home)
    data = json.loads(proc.stdout)
    assert data["action"] == "uninstall"
    assert set(data["summary"]) == {"removed", "restored", "dirs_removed"}
    assert "backed_up" not in data["summary"]


def test_cli_output_survives_legacy_cp1252_console(home):
    """Emulate a legacy Windows console (cp1252 stdio, UTF-8 mode OFF): the plan
    output prints '→', which without _force_utf8_io raises UnicodeEncodeError. This
    proves the fix directly — independent of the PYTHONUTF8=1 the rest of the suite
    runs under (which would otherwise mask the fix's absence)."""
    proc = run_cli(
        "recompile", "--ide", "claude", "--source", str(REPO_ROOT), "--dry-run",
        home=home, env_extra={"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "applied:" in proc.stdout  # human output completed past the '→' op lines


# --- integration: round-trip + foreign-file --------------------------------


def test_end_to_end_round_trip(home):
    assert run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home).returncode == 0
    second = run_cli("install", "--ide", "all", "--source", str(REPO_ROOT), home=home)
    assert "applied: 0" in second.stdout  # idempotent no-op
    assert run_cli("uninstall", home=home).returncode == 0
    assert not (home / ".cohort").exists()


def test_foreign_file_blocks_then_force_then_restore(home):
    (home / ".cohort" / "state").mkdir(parents=True)
    foreign = home / ".cohort" / "canonical"
    foreign.write_text("MINE\n", encoding="utf-8")

    blocked = run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    assert blocked.returncode == 1
    assert foreign.read_text() == "MINE\n"  # untouched

    forced = run_cli(
        "install", "--ide", "claude", "--source", str(REPO_ROOT), "--force", home=home
    )
    assert forced.returncode == 0
    if os.name == "nt":
        assert (home / ".cohort" / "canonical").is_dir()  # copy-mode default on Windows
        assert not (home / ".cohort" / "canonical").is_symlink()
    else:
        assert (home / ".cohort" / "canonical").is_symlink()

    run_cli("uninstall", home=home)
    assert foreign.read_text() == "MINE\n"  # restored


def test_log_lines_carry_required_fields(home):
    proc = run_cli("install", "--ide", "claude", "--source", str(REPO_ROOT), home=home)
    log_lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("component=")]
    assert log_lines
    required = ["component", "action", "scope", "ide", "artifact", "status", "duration_ms"]
    for line in log_lines:
        keys = {p.split("=", 1)[0] for p in line.split(" ")}
        assert all(f in keys for f in required), line


# --- unit: platform-aware placement mode -----------------------------------


def test_resolve_mode_defaults_to_copy_on_windows(monkeypatch):
    import cohort.install_model as im

    monkeypatch.setattr(im.os, "name", "nt")
    assert im.resolve_mode(False) == "copy"  # Windows → copy (no symlink privilege needed)
    assert im.resolve_mode(True) == "copy"
    monkeypatch.setattr(im.os, "name", "posix")
    assert im.resolve_mode(False) == "link"  # POSIX → symlink by default
    assert im.resolve_mode(True) == "copy"  # --copy still forces copy everywhere


# --- unit: selection & source ----------------------------------------------


def test_parse_ide_expands_all_and_dedups():
    assert parse_ide("all") == ["claude", "codex", "cursor"]
    assert parse_ide("claude,claude,cursor") == ["claude", "cursor"]
    assert parse_ide("all,claude") == ["claude", "codex", "cursor"]


def test_parse_ide_rejects_unknown():
    with pytest.raises(UsageError):
        parse_ide("vscode")


def test_merge_ides_is_additive_first_seen():
    assert merge_ides(["claude"], ["cursor", "claude"]) == ["claude", "cursor"]


def test_resolve_source_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    (explicit / "canonical").mkdir(parents=True)
    env_src = tmp_path / "env"
    (env_src / "canonical").mkdir(parents=True)
    monkeypatch.setenv("COHORT_SOURCE", str(env_src))
    # explicit wins over env
    assert resolve_source(str(explicit)) == explicit.resolve()
    # env wins over inference
    assert resolve_source(None) == env_src.resolve()


def test_resolve_source_unresolvable(tmp_path, monkeypatch):
    monkeypatch.delenv("COHORT_SOURCE", raising=False)
    bad = tmp_path / "nope"
    bad.mkdir()
    with pytest.raises(SourceUnresolved):
        resolve_source(str(bad))


def test_resolve_selection_tty_picker(monkeypatch):
    monkeypatch.setattr("cohort.install._isatty", lambda: True)
    monkeypatch.setattr("cohort.install.prompt_ide_selection", lambda: ["claude"])
    assert resolve_selection(None) == ["claude"]


def test_resolve_selection_confirmed_empty_is_usage_error(monkeypatch):
    monkeypatch.setattr("cohort.install._isatty", lambda: True)
    monkeypatch.setattr("cohort.install.prompt_ide_selection", lambda: [])
    with pytest.raises(UsageError):
        resolve_selection(None)


def test_resolve_selection_cancelled(monkeypatch):
    monkeypatch.setattr("cohort.install._isatty", lambda: True)
    monkeypatch.setattr("cohort.install.prompt_ide_selection", lambda: None)
    with pytest.raises(CancelledSelection):
        resolve_selection(None)


def test_resolve_selection_non_tty_is_usage_error(monkeypatch):
    monkeypatch.setattr("cohort.install._isatty", lambda: False)
    with pytest.raises(UsageError):
        resolve_selection(None)


def test_picker_protocol(monkeypatch):
    import io

    from cohort.install import prompt_ide_selection

    def picker(text):
        return prompt_ide_selection(stdin=io.StringIO(text), stdout=io.StringIO())

    assert picker("1 3\n") == ["claude", "cursor"]
    assert picker("\n") == []  # empty line = confirm-empty
    assert picker("q\n") is None  # cancel
    assert picker("") is None  # EOF = cancel

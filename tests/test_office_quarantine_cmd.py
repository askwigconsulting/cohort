"""Behaviour tests for the office-layer quarantine CLI (``cohort office review`` /
``cohort office approve``), the ``AmbiguousApprovalError`` handling added to
``my-office approve``, and the ``record_office_delta`` wiring into ``do_update``.

The office store lives under ``~/.cohort/state``; every test points ``$HOME`` at a
temporary directory so nothing touches the real install.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cohort import cli as cli_module
from cohort.cli import app
from cohort import quarantine
from cohort.update import UpdateResult, do_update

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]

_HASH_A = "a" * 64
_HASH_B = "b" * 64


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway $HOME whose ~/.cohort/state exists (so the stores are writable)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".cohort" / "state"
    state.mkdir(parents=True)
    return tmp_path


def _state(home: Path) -> Path:
    return home / ".cohort" / "state"


def _seed(state: Path, filename: str, records: list[dict]) -> None:
    (state / filename).write_text(json.dumps({"pending": records}), encoding="utf-8")


def _rec(name: str, content_hash: str, kind: str = "memory") -> dict:
    return {"kind": kind, "name": name, "content_hash": content_hash, "first_seen": "2026-01-01T00:00:00Z"}


# --- cohort office review ---------------------------------------------------


def test_office_review_reports_nothing_pending(home: Path):
    result = runner.invoke(app, ["office", "review"])
    assert result.exit_code == 0, result.output
    assert "nothing pending" in result.output


def test_office_review_lists_pending_office_artifacts(home: Path):
    _seed(_state(home), "office_quarantine.json", [_rec("foo", _HASH_A)])
    result = runner.invoke(app, ["office", "review"])
    assert result.exit_code == 0, result.output
    assert "1 office artifact(s) awaiting approval" in result.output
    assert "memory foo" in result.output


def test_office_review_json_lists_pending(home: Path):
    _seed(_state(home), "office_quarantine.json", [_rec("foo", _HASH_A)])
    result = runner.invoke(app, ["office", "review", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pending"] == [{"kind": "memory", "name": "foo", "content_hash": _HASH_A}]


def test_office_review_fails_closed_on_corrupt_state(home: Path):
    (_state(home) / "office_quarantine.json").write_text("{ not json", encoding="utf-8")
    result = runner.invoke(app, ["office", "review"])
    assert result.exit_code == 1
    assert "unreadable" in result.output


# --- cohort office approve --------------------------------------------------


def test_office_approve_clears_a_reviewed_artifact(home: Path):
    _seed(_state(home), "office_quarantine.json", [_rec("foo", _HASH_A)])
    result = runner.invoke(app, ["office", "approve", "foo"])
    assert result.exit_code == 0, result.output
    assert "cleared foo" in result.output
    assert quarantine.office_pending_keys(_state(home)) == set()


def test_office_approve_refuses_an_ambiguous_bare_name(home: Path):
    _seed(_state(home), "office_quarantine.json", [_rec("foo", _HASH_A), _rec("foo", _HASH_B)])
    result = runner.invoke(app, ["office", "approve", "foo"])
    assert result.exit_code == 1
    assert "refusing to guess" in result.output
    assert "hash-prefix" in result.output
    # Nothing cleared — both records remain withheld.
    assert len(quarantine.office_pending_keys(_state(home))) == 2


def test_office_approve_hash_prefix_disambiguates(home: Path):
    _seed(_state(home), "office_quarantine.json", [_rec("foo", _HASH_A), _rec("foo", _HASH_B)])
    result = runner.invoke(app, ["office", "approve", "foo@aaaa"])
    assert result.exit_code == 0, result.output
    remaining = quarantine.office_pending_keys(_state(home))
    assert remaining == {("memory", "foo", _HASH_B)}  # only the reviewed one cleared


def test_office_approve_requires_a_name_or_all(home: Path):
    result = runner.invoke(app, ["office", "approve"])
    assert result.exit_code == 1
    assert "give an artifact name" in result.output


# --- my-office approve: AmbiguousApprovalError (Task 4) ---------------------


def test_my_office_approve_refuses_an_ambiguous_bare_name_cleanly(home: Path):
    # Two pulls left two records under one name; approving the bare name would guess
    # which bytes were reviewed. It must exit non-zero cleanly, not traceback.
    _seed(_state(home), "quarantine.json", [_rec("foo", _HASH_A), _rec("foo", _HASH_B)])
    result = runner.invoke(app, ["my-office", "approve", "foo"])
    assert result.exit_code == 1
    assert "refusing to guess" in result.output
    assert "Traceback" not in result.output


def test_my_office_approve_hash_prefix_disambiguates(home: Path):
    _seed(_state(home), "quarantine.json", [_rec("foo", _HASH_A), _rec("foo", _HASH_B)])
    result = runner.invoke(app, ["my-office", "approve", "foo@bbbb"])
    assert result.exit_code == 0, result.output
    assert quarantine.pending_keys(_state(home)) == {("memory", "foo", _HASH_A)}


# --- Task 6: update staleness detail is surfaced to the human ---------------


def test_print_update_human_surfaces_the_staleness_detail(capsys: pytest.CaptureFixture[str]):
    result = UpdateResult(
        status="updated", upstream="origin/main", behind=1, current="aaa",
        target="bbb", recompiled_ides=["claude"],
        detail="This update changed Cohort's own compiler/renderer.",
    )
    cli_module._print_update_human(result)
    captured = capsys.readouterr()
    assert "compiler/renderer" in (captured.out + captured.err)


def test_print_update_human_no_detail_prints_no_warning(capsys: pytest.CaptureFixture[str]):
    result = UpdateResult(
        status="updated", upstream="origin/main", behind=1, current="aaa",
        target="bbb", recompiled_ides=["claude"], detail="",
    )
    cli_module._print_update_human(result)
    captured = capsys.readouterr()
    assert "warning:" not in (captured.out + captured.err)


# --- Task 5b: record_office_delta wired into do_update ----------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {rel}")


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@e.st")
    _git(up, "config", "user.name", "T")
    (up / "canonical").mkdir()
    _commit(up, "canonical/x.md", "x\n")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(up), str(src))
    _git(src, "config", "user.email", "t@e.st")
    _git(src, "config", "user.name", "T")
    return up, src


_GATED_MEMORY = (
    "---\n"
    "name: pulled_mem\n"
    "kind: memory\n"
    "scope: global\n"
    "description: a memory an update pull introduced\n"
    "targets: [claude]\n"
    "priority: normal\n"
    "---\n"
    "body\n"
)


def _no_pip(args: list) -> int:
    raise AssertionError(f"pip must not run here: {args}")


def test_do_update_records_office_delta_quarantining_a_pulled_gated_artifact(tmp_path: Path):
    up, src = _make_upstream_and_clone(tmp_path)
    home = tmp_path / "home"
    state = home / ".cohort" / "state"
    state.mkdir(parents=True)

    # Establish the office baseline as the shipped set (trusts current, quarantines
    # nothing) — the state a prior first install would have left.
    quarantine.record_office_delta(state, src)
    assert quarantine.office_pending_keys(state) == set()

    # Upstream now ships a NEW gated memory; the update pull must withhold it via the
    # record_office_delta call wired into do_update.
    _commit(up, "canonical/memories/pulled_mem.md", _GATED_MEMORY)
    result = do_update(src, home, pip_run=_no_pip)

    assert result.status == "updated"
    pending = {(a.kind, a.name) for a in quarantine.load_office_pending(state)}
    assert ("memory", "pulled_mem") in pending


def test_do_update_office_delta_is_a_noop_without_state_dir(tmp_path: Path):
    # No ~/.cohort/state (nothing installed) → record_office_delta is a safe no-op and
    # the update still succeeds.
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    result = do_update(src, tmp_path / "home", pip_run=_no_pip)
    assert result.status == "updated"

"""The global ``--dry-run`` is a promise, and honest exit codes are the other half (#267, #270).

``cohort --dry-run <command>`` claims to "print the file operations a command would perform
without making changes". Audit r4 (H3) found ten commands that ignored it — including the
ones with irreversible or outward effects: ``approve`` really released the quarantine,
``engine propose`` really egressed and created a worktree. A preview flag that is a placebo
on exactly the dangerous commands is worse than no flag.

Two families of test here:

* **Introspection** — walk every command registered on the Typer app (subgroups included)
  and require that each either reads the global flag or is on the explicit exemption list
  below, with a reason. A new command cannot silently join the placebo set.
* **Behaviour** — drive each mutating command under ``--dry-run`` with a filesystem snapshot
  (hash of HOME and the repo before/after) while ``subprocess`` and ``socket`` are patched to
  raise. A recording fake alone would prove only that *the fake* was not called; the snapshot
  and the raising patches prove nothing was written, launched or sent by any path.

The second half pins the success signals (#270): ``status`` exits 1 when a ``!`` diagnostic
fired and carries ``ok`` in ``--json``; ``my-office sync`` names a real remedy and exits
non-zero when the recompile failed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import platform
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import typer
from typer.testing import CliRunner

from cohort import autonomy, quarantine
from cohort.cli import app

runner = CliRunner()
COHORT_SRC = Path(__file__).resolve().parents[1]

_HASH_A = "a" * 64
_HASH_B = "b" * 64


# --- fixtures ---------------------------------------------------------------


def tree_hash(root: Path) -> str:
    """Content hash of a tree — names, file bytes, link targets. Order-stable."""
    if not root.exists():
        return "MISSING"
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
        elif p.is_symlink():
            h.update(os.readlink(p).encode())
    return h.hexdigest()


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME (both HOME and USERPROFILE, so ``Path.home()`` agrees on Windows)."""
    h = tmp_path / "home"
    (h / ".cohort" / "state").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("COHORT_SOURCE", raising=False)
    return h


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    shutil.copytree(COHORT_SRC / "adapters", src / "adapters")
    return src


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed git repo as the cwd — the repository context the engine gates need."""
    r = make_git_repo(tmp_path / "repo")
    monkeypatch.chdir(r)
    return r


def _forbid(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("dry-run launched a subprocess or opened a socket")


@pytest.fixture()
def sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """After this fixture runs, any subprocess launch or socket connection raises.

    Applied *after* the fixtures that legitimately shell out (git init, cohort init) so the
    command under test is the only thing that could trip it.
    """
    # Windows before Python 3.12 gets the OS version by spawning a subprocess the first
    # time platform.uname() runs; the result is cached, so warm it before arming the
    # trap — `report`'s environment block is legitimate output, not egress.
    platform.system()
    platform.release()
    monkeypatch.setattr(subprocess, "run", _forbid)
    monkeypatch.setattr(subprocess, "Popen", _forbid)
    monkeypatch.setattr(subprocess, "check_output", _forbid)
    monkeypatch.setattr(subprocess, "check_call", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket.socket, "connect", _forbid)


@contextmanager
def unchanged(*roots: Path) -> Iterator[None]:
    before = [tree_hash(r) for r in roots]
    yield
    after = [tree_hash(r) for r in roots]
    assert after == before, "a dry-run changed the filesystem"


def _dry(*args: str) -> object:
    return runner.invoke(app, ["--dry-run", *args])


# --- introspection: every registered command ---------------------------------

# Commands exempt from reading the global flag, each with the reason it is exempt.
# A command not listed here MUST declare ``ctx`` and read ``dry_run``.
DRY_RUN_EXEMPT: dict[tuple[str, ...], str] = {
    ("validate",): "read-only: validates canonical, writes nothing",
    ("lint",): "read-only: lints canonical, writes nothing",
    ("projects",): "read-only: lists the project registry",
    ("status",): "read-only aggregate (test_status_never_writes pins it)",
    ("office", "review"): "read-only: lists the office quarantine's pending keys",
    ("my-office", "review"): (
        "lists the quarantine; its reconcile prunes only records whose bytes are already "
        "gone (state repair, activates nothing) — W1 owns the hunk for --reset"
    ),
    ("dashboard",): (
        "documented exception (README): a server whose mutations are separately confirmed "
        "POST actions — W6 owns the hunk"
    ),
    ("staleness-check",): "read-only hook target: prints a staleness advisory",
    ("compact-recall",): "read-only hook target: prints the memory-commit instruction",
    ("autonomy-recall",): "read-only hook target: prints the supervision level",
}


def _registered_commands(
    typer_app: typer.Typer, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    for info in typer_app.registered_commands:
        callback = info.callback
        assert callback is not None
        name = info.name or callback.__name__.replace("_", "-")
        yield (*prefix, name), callback
    for group in typer_app.registered_groups:
        assert group.typer_instance is not None and group.name is not None
        yield from _registered_commands(group.typer_instance, (*prefix, group.name))


def test_every_registered_command_reads_the_global_dry_run_or_is_exempt_with_a_reason():
    commands = dict(_registered_commands(app))
    assert len(commands) > 40  # the walk really reached the app, subgroups included
    offenders = []
    for path, callback in commands.items():
        if path in DRY_RUN_EXEMPT:
            continue
        declares_ctx = "ctx" in inspect.signature(callback).parameters
        reads_flag = "dry_run" in inspect.getsource(callback)
        if not (declares_ctx and reads_flag):
            offenders.append(" ".join(path))
    assert offenders == [], f"commands that silently ignore --dry-run: {offenders}"


def test_the_exemption_list_names_only_real_commands():
    registered = {path for path, _ in _registered_commands(app)}
    stale = set(DRY_RUN_EXEMPT) - registered
    assert stale == set(), f"exempt entries for commands that no longer exist: {stale}"


# --- behaviour: try --place ---------------------------------------------------


def test_try_place_under_dry_run_prints_the_plan_and_places_nothing(
    home: Path, source: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
):
    init = runner.invoke(app, ["init", "--source", str(source)])
    assert init.exit_code == 0, init.output
    monkeypatch.setattr(subprocess, "run", _forbid)
    with unchanged(home, repo):
        result = _dry("try", "counsel", "--place", "--source", str(source))
    assert result.exit_code == 0, result.output
    assert "(dry-run)" in result.output
    assert "would sandbox" in result.output
    assert not (repo / ".claude" / "agents" / "counsel.md").exists()
    assert not (repo / ".cohort" / "canonical" / "agents" / "counsel.md").exists()


def test_try_place_dry_run_json_reports_the_would_be_path(
    home: Path, source: Path, repo: Path
):
    init = runner.invoke(app, ["init", "--source", str(source)])
    assert init.exit_code == 0, init.output
    result = _dry("try", "counsel", "--place", "--source", str(source), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["would_place"].endswith("counsel.md")
    assert "placed" not in report


# --- behaviour: approve -------------------------------------------------------


def _seed(home: Path, filename: str, records: list[dict]) -> None:
    (home / ".cohort" / "state" / filename).write_text(
        json.dumps({"pending": records}), encoding="utf-8"
    )


def _rec(name: str, content_hash: str) -> dict:
    return {"kind": "memory", "name": name, "content_hash": content_hash,
            "first_seen": "2026-01-01T00:00:00Z"}


def test_my_office_approve_all_under_dry_run_releases_nothing(home: Path, sealed: None):
    _seed(home, "quarantine.json", [_rec("foo", _HASH_A), _rec("bar", _HASH_B)])
    with unchanged(home):
        result = _dry("my-office", "approve", "--all")
    assert result.exit_code == 0, result.output
    assert "(dry-run)" in result.output
    assert "would clear bar, foo" in result.output
    assert len(quarantine.pending_keys(home / ".cohort" / "state")) == 2


def test_my_office_approve_dry_run_json_carries_the_plan(home: Path, sealed: None):
    _seed(home, "quarantine.json", [_rec("foo", _HASH_A)])
    result = _dry("my-office", "approve", "foo", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["would_approve"] == ["foo"]
    assert len(quarantine.pending_keys(home / ".cohort" / "state")) == 1


def test_my_office_approve_dry_run_still_refuses_an_ambiguous_name(home: Path, sealed: None):
    """A preview that guessed where the real command refuses would mispredict the outcome."""
    _seed(home, "quarantine.json", [_rec("foo", _HASH_A), _rec("foo", _HASH_B)])
    with unchanged(home):
        result = _dry("my-office", "approve", "foo")
    assert result.exit_code == 1
    assert "refusing to guess" in result.output


def test_office_approve_under_dry_run_releases_nothing(home: Path, sealed: None):
    _seed(home, "office_quarantine.json", [_rec("foo", _HASH_A)])
    with unchanged(home):
        result = _dry("office", "approve", "foo")
    assert result.exit_code == 0, result.output
    assert "(dry-run)" in result.output
    assert "would clear foo" in result.output
    assert quarantine.office_pending_keys(home / ".cohort" / "state") == {
        ("memory", "foo", _HASH_A)
    }


# --- behaviour: report --------------------------------------------------------


def test_report_under_dry_run_prints_the_preview_and_never_calls_gh(
    home: Path, tmp_path: Path, sealed: None
):
    body = tmp_path / "body.md"
    body.write_text("Steps:\n1. run it\n2. wait\n", encoding="utf-8")
    with unchanged(home):
        result = _dry("report", "--title", "[engines] consult times out", "--body-file", str(body))
    assert result.exit_code == 0, result.output
    assert "[engines] consult times out" in result.output
    assert "Steps:" in result.output
    assert "(dry-run)" in result.output and "not filed" in result.output


def test_report_dry_run_json_names_the_target_without_filing(
    home: Path, tmp_path: Path, sealed: None
):
    body = tmp_path / "body.md"
    body.write_text("something broke\n", encoding="utf-8")
    result = _dry("report", "--title", "t", "--body-file", str(body), "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["repo"] and "url" not in report


def test_report_dry_run_still_refuses_a_secret(home: Path, tmp_path: Path, sealed: None):
    body = tmp_path / "body.md"
    body.write_text('AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"\n',
                    encoding="utf-8")
    result = _dry("report", "--title", "t", "--body-file", str(body))
    assert result.exit_code == 1
    assert "Nothing was filed" in result.output


# --- behaviour: engine --------------------------------------------------------


@pytest.fixture(autouse=True)
def _grok_cli_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the API-direct channel so the engine previews are host-independent."""
    monkeypatch.setattr("cohort.engines.cli_doer._grok_cli_available", lambda: False)


def _task_file(tmp_path: Path, text: str = "make the tests faster") -> Path:
    f = tmp_path / "task.txt"
    f.write_text(text, encoding="utf-8")
    return f


def test_engine_consult_under_dry_run_prints_gates_limits_and_egress_and_sends_nothing(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    prompt = _task_file(tmp_path, "explain this")
    with unchanged(home, repo):
        result = _dry("engine", "consult", "grok", "--prompt-file", str(prompt), "--tier", "cheap")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "(dry-run)" in out and "nothing sent" in out
    assert "secret scan" in out and "egress opt-out" in out
    assert "grok-4.3" in out  # the resolved model is part of the prospective egress
    assert "12 bytes" in out  # the payload that would leave the machine
    assert "--max-tokens 4096" in out
    assert str(prompt) in out


def test_engine_consult_dry_run_reports_a_gate_refusal_honestly(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    prompt = _task_file(tmp_path, 'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"')
    result = _dry("engine", "consult", "grok", "--prompt-file", str(prompt))
    assert result.exit_code == 1
    assert "Nothing was sent" in result.output


def test_engine_consult_dry_run_honours_the_egress_opt_out(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    (repo / ".cohort").mkdir()
    (repo / ".cohort" / "project_context.md").write_text(
        "## Egress\n\ncohort:egress=deny\n", encoding="utf-8"
    )
    prompt = _task_file(tmp_path, "hi")
    result = _dry("engine", "consult", "grok", "--prompt-file", str(prompt))
    assert result.exit_code == 1
    assert "opted out" in result.output.lower() or "egress" in result.output.lower()


def test_engine_review_under_dry_run_names_the_unenumerable_later_payloads(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    with unchanged(home, repo):
        result = _dry("engine", "review", "grok", "--task-file", str(task), "--max-iterations", "7")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "cannot be enumerated" in out
    assert "--max-iterations 7" in out
    assert "engine-transcripts" in out
    assert not (repo / ".cohort" / "engine-transcripts").exists()


def test_engine_propose_under_dry_run_creates_no_worktree(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    with unchanged(home, repo):
        result = _dry(
            "engine", "propose", "grok", "--task-file", str(task),
            "--footprint", "cli/cohort/cli.py", "--footprint", "tests/",
        )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "footprint: cli/cohort/cli.py, tests/" in out
    assert "worktree" in out and "no worktree" in out
    assert "cannot be enumerated" not in out  # one-shot: the whole payload is the prompt


def test_engine_propose_agentic_under_dry_run_says_reads_are_model_selected(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    with unchanged(home, repo):
        result = _dry(
            "engine", "propose", "grok", "--agentic", "--task-file", str(task),
            "--footprint", "cli/",
        )
    assert result.exit_code == 0, result.output
    assert "cannot be enumerated" in result.output


def test_engine_propose_dry_run_still_requires_a_footprint(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    result = _dry("engine", "propose", "grok", "--task-file", str(task))
    assert result.exit_code == 2
    assert "--footprint is required" in result.output


def test_engine_work_under_dry_run_prints_the_wire_cap_and_launches_nothing(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    with unchanged(home, repo):
        result = _dry("engine", "work", "gpt", "--task-file", str(task), "--footprint", "src/")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "wire cap" in out and "50000000" in out
    assert "tracked file" in out  # the CLI reads the worktree's committed files
    assert "cannot be enumerated" in out
    assert "footprint" in out and "src/" in out


def test_engine_work_dry_run_rejects_an_engine_with_no_doer(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    result = _dry("engine", "work", "claude", "--task-file", str(task))
    assert result.exit_code == 2
    assert "no CLI doer" in result.output


def test_engine_ratchet_under_dry_run_never_runs_the_evaluator(
    home: Path, repo: Path, tmp_path: Path, sealed: None
):
    task = _task_file(tmp_path)
    with unchanged(home, repo):
        result = _dry(
            "engine", "ratchet", "gpt", "--task-file", str(task),
            "--evaluator", "pytest -q 2>&1 | tail -1", "--budget", "3",
        )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "pytest -q 2>&1 | tail -1" in out and "not run" in out
    assert "budget: 3" in out
    assert "cannot be enumerated" in out


# --- behaviour: reference / autonomy / relink / gc / hook targets ---------------


def test_reference_refuses_the_global_dry_run_with_exit_2(home: Path, source: Path, sealed: None):
    with unchanged(home, source):
        result = _dry("reference", "--source", str(source))
    assert result.exit_code == 2
    assert "does not support --dry-run" in result.output
    assert not (source / "docs").exists()


def test_autonomy_under_dry_run_validates_and_writes_no_state(home: Path, sealed: None):
    with unchanged(home):
        result = _dry("autonomy", "autopilot")
    assert result.exit_code == 0, result.output
    assert "would set autonomy to autopilot" in result.output
    assert autonomy.read_autonomy_level(home) == autonomy.DEFAULT_LEVEL


def test_autonomy_under_dry_run_still_rejects_an_unknown_level(home: Path, sealed: None):
    result = _dry("autonomy", "yolo")
    assert result.exit_code == 2
    assert "unknown autonomy level" in result.output


def test_autonomy_show_ignores_dry_run_because_it_only_reads(home: Path, sealed: None):
    result = _dry("autonomy")
    assert result.exit_code == 0, result.output
    assert f"autonomy: {autonomy.DEFAULT_LEVEL}" in result.output


def test_relink_refuses_the_global_dry_run_with_exit_2(home: Path, source: Path, sealed: None):
    with unchanged(home):
        result = _dry("relink", "--source", str(source))
    assert result.exit_code == 2
    assert "does not support --dry-run" in result.output


def test_gc_apply_under_dry_run_deletes_nothing(home: Path, repo: Path):
    """gc's scan legitimately asks git which worktrees are live (read-only), so this one is
    not sealed against subprocess — the snapshot is the proof that --apply was disarmed."""
    with unchanged(home, repo):
        result = _dry("gc", "--apply", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["applied"] is False


@pytest.mark.parametrize(
    "argv",
    [
        ["working-note", "remember this"],
        ["working-capture"],
        ["session-capture"],
        ["session-recall"],
        ["update-check"],
    ],
)
def test_state_writing_hook_targets_refuse_the_global_dry_run(
    home: Path, repo: Path, sealed: None, argv: list[str]
):
    with unchanged(home, repo):
        result = _dry(*argv)
    assert result.exit_code == 2, result.output
    assert "does not support --dry-run" in result.output


def test_add_skill_honours_the_global_dry_run(home: Path, source: Path, sealed: None):
    with unchanged(home, source):
        result = _dry(
            "add-skill", "tidy-up", "--description", "Tidies.", "--source", str(source), "--json",
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is True


def test_my_office_sync_honours_the_global_dry_run(home: Path, sealed: None):
    with unchanged(home):
        result = _dry("my-office", "sync", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is True


# --- status exit code (#270) ---------------------------------------------------


@pytest.fixture()
def installed(home: Path, source: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A healthy global install of the claude adapter, in-process."""
    monkeypatch.setenv("COHORT_ADAPTERS_DIR", str(source / "adapters"))
    result = runner.invoke(app, ["recompile", "--ide", "claude", "--source", str(source)])
    assert result.exit_code == 0, result.output
    return home


def test_status_exits_0_on_a_healthy_install(installed: Path, tmp_path: Path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    human = runner.invoke(app, ["status"])
    assert human.exit_code == 0, human.output
    as_json = runner.invoke(app, ["status", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["ok"] is True


def test_status_exits_1_when_a_project_specialist_shadows_a_global_agent(
    installed: Path, source: Path, repo: Path
):
    """The shadowing ``!`` goes to stdout, not stderr — it must still flip the exit code."""
    assert runner.invoke(app, ["init", "--source", str(source)]).exit_code == 0
    added = runner.invoke(app, [
        "add-specialist", "--name", "counsel", "--display-name", "Counsel",
        "--department", "Legal", "--description", "x.",
    ])
    assert added.exit_code == 0, added.output
    human = runner.invoke(app, ["status"])
    assert human.exit_code == 1, human.output
    assert "! counsel shadows a global agent" in human.output
    as_json = runner.invoke(app, ["status", "--json"])
    assert as_json.exit_code == 1
    report = json.loads(as_json.stdout)  # still parseable — scripts read it on failure too
    assert report["ok"] is False
    assert report["project"]["shadowed"] == ["counsel"]


def test_status_exits_1_when_the_project_wiring_is_missing(installed: Path, source: Path, repo: Path):
    assert runner.invoke(app, ["init", "--source", str(source)]).exit_code == 0
    (repo / ".claude" / "CLAUDE.md").write_text("# mine only\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["project"]["wiring"]["state"] == "missing"


def test_status_exits_1_on_an_unmanaged_claude_file(installed: Path):
    loose = installed / ".claude" / "agents" / "perf-auditor.md"
    loose.write_text("---\nname: perf-auditor\n---\nbody\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


# --- my-office sync remedy (#270) ---------------------------------------------


def _sync_report(recompile_failed: str | None) -> dict:
    return {"action": "my-sync", "dry_run": False, "remote": "git@example:me/my.git",
            "pulled": True, "pushed": True, "recompiled": [],
            "recompile_failed": recompile_failed, "quarantined": []}


def test_my_office_sync_exits_1_and_names_a_real_remedy_when_recompile_failed(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "cohort.myoffice.do_my_sync", lambda *_a, **_k: _sync_report("OSError: disk full")
    )
    result = runner.invoke(app, ["my-office", "sync"])
    assert result.exit_code == 1, result.output
    assert "disk full" in result.output
    assert "`cohort recompile`" in result.output
    assert "update --recompile" not in result.output


def test_my_office_sync_exits_1_in_json_mode_too_when_recompile_failed(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "cohort.myoffice.do_my_sync", lambda *_a, **_k: _sync_report("OSError: disk full")
    )
    result = runner.invoke(app, ["my-office", "sync", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["recompile_failed"] == "OSError: disk full"


def test_my_office_sync_exits_0_when_the_recompile_succeeded(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("cohort.myoffice.do_my_sync", lambda *_a, **_k: _sync_report(None))
    result = runner.invoke(app, ["my-office", "sync"])
    assert result.exit_code == 0, result.output

"""Tests for the codex CLI consult transport (cohort.engines.codex_cli).

``codex`` is never run: every test either fakes the launch seam
(``cli_doer._launch_vendor_cli``) or drives a real, harmless subprocess through it to
prove the seam delivers the prompt on stdin. Tests assert on behaviour — the argv shape
that pins the read-only sandbox, the scrubbed environment, the error types — never on
how the module is written.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cohort.engines import cli_doer, codex_cli


@pytest.fixture
def _codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/fake/bin/codex")


class _Launch:
    """Records the one launch a consult makes and returns a canned outcome."""

    def __init__(self, outcome: subprocess.CompletedProcess | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def __call__(self, cmd, *, timeout, env, stdin_text=None):
        self.calls.append(
            {"cmd": list(cmd), "timeout": timeout, "env": dict(env), "stdin_text": stdin_text}
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _ok(stdout: str = "a second opinion\n") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["codex"], 0, stdout, "")


# --- invocation shape -------------------------------------------------------------


def test_consult_pins_the_read_only_sandbox_and_feeds_the_prompt_on_stdin(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    reply = codex_cli.consult("what is wrong with this plan?")

    assert reply == "a second opinion\n"
    (call,) = launch.calls
    cmd = call["cmd"]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "workspace-write" not in cmd and not any("danger" in c for c in cmd)
    # The prompt travels on stdin (never argv: no process-list leak, no ARG_MAX cap) and
    # the CLI is told so with "-", so it can never wait on an inherited stdin.
    assert cmd[-1] == "-"
    assert call["stdin_text"] == "what is wrong with this plan?"
    assert "what is wrong" not in " ".join(cmd)


def test_consult_starts_codex_in_an_empty_scratch_directory_that_is_removed_after(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    seen: dict[str, object] = {}

    def launch(cmd, *, timeout, env, stdin_text=None):
        root = Path(cmd[cmd.index("-C") + 1])
        seen["root"] = root
        seen["existed"] = root.is_dir()
        seen["entries"] = sorted(p.name for p in root.iterdir())
        return _ok()

    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)
    codex_cli.consult("q")

    root = seen["root"]
    assert isinstance(root, Path)
    assert seen["existed"] is True and seen["entries"] == []
    assert not root.exists()  # nothing lingers once the consult returns
    assert root != Path.cwd()  # never this repo's tree


def test_consult_uses_the_scrubbed_environment_with_the_real_home(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_hostsecret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hostsecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    codex_cli.consult("q")

    env = launch.calls[0]["env"]
    assert isinstance(env, dict)
    assert "GITHUB_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["OPENAI_API_KEY"] == "sk-test-key"  # the CLI's own key rides through
    assert env["HOME"] == str(Path.home())  # so a saved `codex login` (~/.codex) resolves
    assert "PATH" in env


def test_consult_passes_an_explicit_model_and_a_generous_default_timeout(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    codex_cli.consult("q", model="gpt-5.6-sol")
    codex_cli.consult("q")

    pinned, default = launch.calls
    assert pinned["cmd"][pinned["cmd"].index("-m") + 1] == "gpt-5.6-sol"
    assert "-m" not in default["cmd"]  # the CLI's default flagship, never a downgrade
    assert default["timeout"] == codex_cli.CONSULT_TIMEOUT_SECONDS
    assert codex_cli.CONSULT_TIMEOUT_SECONDS >= 300  # a flagship consult takes minutes


# --- failure modes ----------------------------------------------------------------


def test_consult_refuses_before_any_launch_when_the_cli_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: None)
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    with pytest.raises(codex_cli.CodexUnavailableError) as excinfo:
        codex_cli.consult("q")

    assert launch.calls == []
    assert "codex login" in str(excinfo.value)
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_consult_reports_a_non_zero_exit_with_the_stderr_tail(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    failed = subprocess.CompletedProcess(["codex"], 1, "", "x" * 5000 + "\nnot logged in\n")
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", _Launch(failed))

    with pytest.raises(codex_cli.CodexFailedError) as excinfo:
        codex_cli.consult("q")

    message = str(excinfo.value)
    assert "exited 1" in message and "not logged in" in message
    assert len(message) < 3000  # the tail, not the whole stream


def test_consult_reports_a_timeout_as_a_failure_naming_the_budget(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    expired = subprocess.TimeoutExpired(["codex"], 12.0, output="partial", stderr="")
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", _Launch(expired))

    with pytest.raises(codex_cli.CodexFailedError) as excinfo:
        codex_cli.consult("q", timeout=12.0)

    assert "12" in str(excinfo.value) and "--timeout" in str(excinfo.value)


def test_consult_refuses_an_empty_prompt_before_any_launch(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    with pytest.raises(codex_cli.ConsultError):
        codex_cli.consult("   \n")
    assert launch.calls == []


# --- model resolution: the CLI's default flagship, unless the user pins one ----------


def test_resolve_model_defaults_to_the_cli_flagship_and_honours_an_explicit_model() -> None:
    assert codex_cli.resolve_model(None, None) is None
    assert codex_cli.resolve_model("flagship", None) is None
    assert codex_cli.resolve_model(None, "gpt-5.6-sol") == "gpt-5.6-sol"


def test_resolve_model_rejects_other_tiers_and_tier_plus_model() -> None:
    with pytest.raises(codex_cli.ConsultError, match="--model"):
        codex_cli.resolve_model("cheap", None)  # never a silent downgrade
    with pytest.raises(codex_cli.ConsultError, match="mutually exclusive"):
        codex_cli.resolve_model("flagship", "gpt-5.6-sol")


# --- the launch seam really delivers stdin_text and still closes stdin ------------


def test_launch_vendor_cli_delivers_stdin_text_to_the_child() -> None:
    script = "import sys; sys.stdout.write('got:' + sys.stdin.read())"
    proc = cli_doer._launch_vendor_cli(
        [sys.executable, "-c", script],
        timeout=30.0,
        env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        stdin_text="the prompt\n",
    )
    assert proc.returncode == 0
    assert proc.stdout == "got:the prompt\n"


def test_launch_vendor_cli_without_stdin_text_still_closes_stdin() -> None:
    script = "import sys; sys.stdout.write(repr(sys.stdin.read()))"
    proc = cli_doer._launch_vendor_cli(
        [sys.executable, "-c", script],
        timeout=30.0,
        env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
    )
    assert proc.returncode == 0
    assert proc.stdout == "''"  # DEVNULL: EOF at once, never an inherited terminal

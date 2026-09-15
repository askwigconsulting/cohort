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


from test_ratchet import _require_bwrap_or_skip


@pytest.fixture
def _codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_cli.shutil, "which", lambda name: "/fake/bin/codex" if name == "codex" else None
    )
    # Argv-shape tests look at the codex invocation itself; the jail is exercised by
    # its own tests below.
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: None)


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


def test_consult_reports_a_non_zero_exit_without_echoing_the_prompt(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    # codex echoes the prompt to stderr under a "user" heading; a failure must quote the
    # diagnostic and never the prompt (it may hold what the secret scan did not catch).
    prompt = "review this plan\ninternal hostname db-7.corp.example\n"
    stderr = (
        "OpenAI Codex v0.144.4\n--------\nuser\nreview this plan\n"
        "internal hostname db-7.corp.example\n" + "x" * 5000 + "\nERROR: not logged in\n"
    )
    failed = subprocess.CompletedProcess(["codex"], 1, "", stderr)
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", _Launch(failed))

    with pytest.raises(codex_cli.CodexFailedError) as excinfo:
        codex_cli.consult(prompt)

    message = str(excinfo.value)
    assert "exited 1" in message and "not logged in" in message
    assert "db-7.corp.example" not in message and "review this plan" not in message
    assert "x" * 201 not in message  # bounded lines, never a raw tail
    assert "codex login status" in message


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


# --- the jail: confined to the prompt wherever bubblewrap is installed --------------


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in HOME with a codex auth dir and a secret that must stay invisible."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"login": "saved"}', encoding="utf-8")
    (home / "secret.txt").write_text("ssh-private-key-material", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def test_consult_runs_inside_the_bubblewrap_jail_when_bwrap_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        codex_cli.shutil, "which", lambda name: "/fake/bin/codex" if name == "codex" else None
    )
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    codex_cli.consult("q")

    cmd = launch.calls[0]["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-all", "--share-net", "--die-with-parent", "--new-session"):
        assert flag in cmd
    inner = cmd[cmd.index("--") + 1:]
    assert inner[:2] == ["codex", "exec"] and inner[-1] == "-"
    scratch = Path(inner[inner.index("-C") + 1])
    sandbox_home = Path(cmd[cmd.index("HOME") + 1])
    assert cmd[cmd.index("--chdir") + 1] == str(scratch)
    assert cmd[cmd.index("CODEX_HOME") + 1] == str(sandbox_home / ".codex")
    # The only writable binds are the scratch root and codex's own state dir (codex will
    # not start on a read-only one); the only thing from the real home is that state
    # dir, mounted into the ephemeral HOME.
    binds = [(cmd[i], cmd[i + 1], cmd[i + 2]) for i, tok in enumerate(cmd) if tok in ("--bind", "--ro-bind")]
    writable = sorted(src for kind, src, _dst in binds if kind == "--bind")
    assert writable == sorted([str(scratch), str(home / ".codex")])
    from_home = [(src, dst) for _kind, src, dst in binds if Path(src).is_relative_to(home)]
    assert from_home == [(str(home / ".codex"), str(sandbox_home / ".codex"))]


def test_consult_honours_codex_home_override_inside_the_jail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_home(tmp_path, monkeypatch)
    override = tmp_path / "elsewhere" / "codex-cfg"
    override.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(override))
    monkeypatch.setattr(
        codex_cli.shutil, "which", lambda name: "/fake/bin/codex" if name == "codex" else None
    )
    monkeypatch.setattr(cli_doer, "_bwrap", lambda: "/usr/bin/bwrap")
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    codex_cli.consult("q")

    cmd = launch.calls[0]["cmd"]
    assert isinstance(cmd, list)
    sandbox_home = Path(cmd[cmd.index("HOME") + 1])
    triples = [tuple(cmd[i:i + 3]) for i, tok in enumerate(cmd) if tok == "--bind"]
    assert ("--bind", str(override), str(sandbox_home / ".codex")) in triples
    assert not any(src == str(tmp_path / "home" / ".codex") for _k, src, _d in triples)


def test_consult_runs_unjailed_when_bwrap_is_absent(
    monkeypatch: pytest.MonkeyPatch, _codex_on_path: None
) -> None:
    launch = _Launch(_ok())
    monkeypatch.setattr(cli_doer, "_launch_vendor_cli", launch)

    codex_cli.consult("q")

    assert launch.calls[0]["cmd"][0] == "codex"
    assert codex_cli.jailed() is False


def test_live_jail_hides_the_repo_and_the_real_home_but_shows_codex_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in ``codex`` runs under the real jail and reports what it can reach: the
    repository file and the real HOME's secret must be unreadable, codex's auth dir
    readable, and the scratch root writable. Skips where bwrap cannot sandbox, but
    fails under ``COHORT_REQUIRE_BWRAP=1`` (CI installs bubblewrap for this)."""
    _require_bwrap_or_skip()
    home = _fake_home(tmp_path, monkeypatch)
    repo_file = tmp_path / "repo" / "secrets.env"
    repo_file.parent.mkdir()
    repo_file.write_text("DB_HOST=db-7.corp.example", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in = bin_dir / "codex"
    stand_in.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"  # the prompt arrives on stdin, as with the real CLI
        'echo "cwd=$(pwd)"\n'
        f'if cat "{repo_file}" >/dev/null 2>&1; then echo repo=readable; else echo repo=blocked; fi\n'
        f'if cat "{home / "secret.txt"}" >/dev/null 2>&1; then echo home=readable; else echo home=blocked; fi\n'
        'if cat "$CODEX_HOME/auth.json" >/dev/null 2>&1; then echo auth=readable; else echo auth=blocked; fi\n'
        'if cat "$HOME/.codex/auth.json" >/dev/null 2>&1; then echo homeauth=readable; else echo homeauth=blocked; fi\n'
        "if touch ./note >/dev/null 2>&1; then echo write=ok; else echo write=blocked; fi\n"
        'if touch "$CODEX_HOME/note" >/dev/null 2>&1; then echo authwrite=ok; else echo authwrite=blocked; fi\n',
        encoding="utf-8",
    )
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    reply = codex_cli.consult("what do you see?", timeout=60.0)

    facts = dict(line.split("=", 1) for line in reply.splitlines() if "=" in line)
    assert facts["repo"] == "blocked", reply
    assert facts["home"] == "blocked", reply
    assert facts["auth"] == "readable", reply
    assert facts["homeauth"] == "readable", reply  # HOME inside the jail is the ephemeral one
    assert facts["write"] == "ok", reply  # the scratch root is the working directory
    assert facts["authwrite"] == "ok", reply  # codex's own state dir: it must write there
    assert "cohort-consult-" in facts["cwd"], reply

"""#122: the remote-transport allowlist (bans ext::/fd::) is hoisted into the
shared GIT_ENV, so every git caller — update, my-office sync — refuses a
code-executing transport and none can drift by forgetting the -c flags.

The ext:: transport runs its string AS a command; the tests use an ext:: URL
whose command would create a marker file. If the transport is blocked, the
command never runs (no marker) — that absence is the real proof, since ls-remote
returns non-zero either way (a bare `touch` doesn't speak the git protocol)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cohort import myoffice, update
from cohort.gitutil import GIT_ENV


def _git_config() -> dict[str, str]:
    return {
        GIT_ENV[f"GIT_CONFIG_KEY_{i}"]: GIT_ENV[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(GIT_ENV["GIT_CONFIG_COUNT"]))
    }


def _ext_url(marker: Path) -> str:
    return f"ext::touch {marker}"  # ext runs `touch <marker>` as the "transport"


def test_git_env_default_denies_and_allows_only_safe_schemes():
    cfg = _git_config()
    assert cfg["protocol.allow"] == "never"  # default-deny
    for scheme in ("file", "ssh", "https", "http"):
        assert cfg[f"protocol.{scheme}.allow"] == "always"
    assert cfg["credential.helper"] == ""  # no stored helper prompts/leaks


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )


def test_ext_transport_command_never_runs_under_shared_env(tmp_path):
    marker = tmp_path / "pwned"
    r = _run_git(tmp_path, "ls-remote", _ext_url(marker))
    assert r.returncode != 0
    assert not marker.exists()  # the ext:: command was refused before it could run


def test_file_transport_still_works(tmp_path):
    # a legitimate local (file) remote must remain reachable — we didn't over-ban.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    r = _run_git(tmp_path, "ls-remote", str(remote))
    assert r.returncode == 0  # empty, but reachable


def _init(repo: Path) -> Path:
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_update_git_wrapper_refuses_ext(tmp_path):
    # update.py fetches a config-derived upstream — its wrapper inherits the ban.
    marker = tmp_path / "pwned"
    rc, _ = update._git(_init(tmp_path / "src"), "ls-remote", _ext_url(marker))
    assert rc != 0 and not marker.exists()


def test_myoffice_git_wrapper_refuses_ext(tmp_path):
    marker = tmp_path / "pwned"
    rc, _ = myoffice._git(_init(tmp_path / "my"), "ls-remote", _ext_url(marker))
    assert rc != 0 and not marker.exists()

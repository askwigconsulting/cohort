"""P1-T3: bootstrap.sh & Makefile (clone-and-go).

Behavioral tests use a *fake* venv (fake python + fake cohort) so they are
hermetic and fast — they assert the bootstrap's control flow and the exact
forwarded command, not a real pip install. One integration test drives the real
venv to prove end-to-end forwarding and exit-code propagation.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "installer" / "bootstrap.sh"
MAKEFILE = REPO_ROOT / "installer" / "Makefile"
REAL_VENV = REPO_ROOT / ".venv"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


@pytest.fixture
def fake_venv(tmp_path):
    """A venv dir with a fake python and fake cohort that record their calls."""
    venv = tmp_path / "venv"
    binp = venv / "bin"
    binp.mkdir(parents=True)
    pip_trace = tmp_path / "pip_trace.txt"
    cohort_trace = tmp_path / "cohort_trace.txt"

    _write_exec(
        binp / "python",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  [ "${FAKE_IMPORTABLE:-1}" = "1" ] && exit 0 || exit 1\n'
        "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        '  echo "$@" >> "$PIP_TRACE"; exit 0\n'
        "fi\n"
        "exit 0\n",
    )
    _write_exec(
        binp / "cohort",
        '#!/bin/sh\necho "$@" >> "$COHORT_TRACE"\nexit "${FAKE_COHORT_RC:-0}"\n',
    )
    return {
        "venv": venv,
        "cohort_bin": binp / "cohort",
        "pip_trace": pip_trace,
        "cohort_trace": cohort_trace,
    }


def run_bootstrap(*args, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["sh", str(BOOTSTRAP), *args], capture_output=True, text=True, env=env
    )


# --- behavioral -------------------------------------------------------------


def test_bootstrap_forwards_dry_run_and_skips_pip_when_importable(fake_venv):
    env = {
        "COHORT_SOURCE": str(REPO_ROOT),
        "COHORT_VENV": str(fake_venv["venv"]),
        "COHORT_BIN": str(fake_venv["cohort_bin"]),
        "PIP_TRACE": str(fake_venv["pip_trace"]),
        "COHORT_TRACE": str(fake_venv["cohort_trace"]),
        "FAKE_IMPORTABLE": "1",
    }
    proc = run_bootstrap("--ide", "claude", "--dry-run", env_extra=env)
    assert proc.returncode == 0, proc.stderr
    trace = fake_venv["cohort_trace"].read_text()
    assert f"recompile --source {REPO_ROOT} --ide claude --dry-run" in trace
    # importable → no pip install churn (N5)
    assert not fake_venv["pip_trace"].exists()


def test_bootstrap_installs_when_not_importable(fake_venv):
    env = {
        "COHORT_SOURCE": str(REPO_ROOT),
        "COHORT_VENV": str(fake_venv["venv"]),
        "COHORT_BIN": str(fake_venv["cohort_bin"]),
        "PIP_TRACE": str(fake_venv["pip_trace"]),
        "COHORT_TRACE": str(fake_venv["cohort_trace"]),
        "FAKE_IMPORTABLE": "0",
    }
    proc = run_bootstrap("--ide", "claude", env_extra=env)
    assert proc.returncode == 0, proc.stderr
    assert fake_venv["pip_trace"].exists()
    assert "pip install -e" in fake_venv["pip_trace"].read_text()


def test_bootstrap_propagates_cli_exit_code(fake_venv):
    env = {
        "COHORT_SOURCE": str(REPO_ROOT),
        "COHORT_VENV": str(fake_venv["venv"]),
        "COHORT_BIN": str(fake_venv["cohort_bin"]),
        "PIP_TRACE": str(fake_venv["pip_trace"]),
        "COHORT_TRACE": str(fake_venv["cohort_trace"]),
        "FAKE_IMPORTABLE": "1",
        "FAKE_COHORT_RC": "2",
    }
    proc = run_bootstrap(env_extra=env)
    assert proc.returncode == 2


# --- Makefile ---------------------------------------------------------------


@pytest.fixture
def fake_cohort(tmp_path):
    trace = tmp_path / "make_trace.txt"
    binf = tmp_path / "fake_cohort"
    _write_exec(binf, f'#!/bin/sh\necho "$@" >> "{trace}"\nexit 0\n')
    return {"bin": binf, "trace": trace}


def test_make_install_forwards_ide(fake_cohort):
    proc = subprocess.run(
        ["make", "-C", str(REPO_ROOT / "installer"), "install",
         "IDE=claude,cursor", f"COHORT={fake_cohort['bin']}"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "recompile --ide claude,cursor" in fake_cohort["trace"].read_text()


def test_make_uninstall_invokes_cli(fake_cohort):
    proc = subprocess.run(
        ["make", "-C", str(REPO_ROOT / "installer"), "uninstall",
         f"COHORT={fake_cohort['bin']}"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "uninstall" in fake_cohort["trace"].read_text()


# --- script hygiene ---------------------------------------------------------


def test_bootstrap_is_posix_sh_clean():
    proc = subprocess.run(["sh", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed (N4)")
def test_bootstrap_shellcheck_clean():
    proc = subprocess.run(["shellcheck", str(BOOTSTRAP)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


# --- integration: real venv -------------------------------------------------


@pytest.mark.skipif(not REAL_VENV.exists(), reason="real .venv not present")
def test_bootstrap_real_venv_round_trip(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"COHORT_VENV": str(REAL_VENV), "HOME": str(home)}
    # dry-run via the real cohort: forwards and changes nothing
    dry = run_bootstrap(
        "--ide", "all", "--dry-run", env_extra={**env, "COHORT_SOURCE": str(REPO_ROOT)}
    )
    assert dry.returncode == 0, dry.stderr
    assert not (home / ".cohort").exists()
    # no --ide in a non-TTY → propagate exit 2
    bad = run_bootstrap(env_extra={**env, "COHORT_SOURCE": str(REPO_ROOT)})
    assert bad.returncode == 2

"""#30: opt-in signed-commit verification for `cohort update`.

With ``[update] require_signed = true`` in the global cohort.toml, `cohort update`
refuses an upstream tip whose commit isn't verifiably signed — the residual risk
once transport and local config are trusted is a *compromised upstream* whose
malicious commit is still a valid fast-forward. Default stays off (clone-and-go).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cohort.install_model import CohortPaths
from cohort.update import _commit_is_signed, _require_signed, do_update


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _commit(repo: Path, name: str, body: str, *, sign: bool = False) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", *(["-S"] if sign else []), "-qm", f"add {name}")


def _make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@e.st")
    _git(up, "config", "user.name", "T")
    # Pin both repos to unsigned so a contributor's global commit.gpgsign=true
    # can't auto-sign these commits and flip the "unsigned refuses" assertions.
    # (The SSH test re-enables signing on `up` explicitly.)
    _git(up, "config", "commit.gpgsign", "false")
    (up / "canonical").mkdir()
    _commit(up, "canonical/x.md", "x\n")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(up), str(src))
    _git(src, "config", "user.email", "t@e.st")
    _git(src, "config", "user.name", "T")
    _git(src, "config", "commit.gpgsign", "false")
    return up, src


def _no_pip(args: list) -> int:
    raise AssertionError(f"pip must not run here: {args}")


def _write_config(home: Path, text: str) -> None:
    cfg = CohortPaths(home).cohort_home / "cohort.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# === config reader ===========================================================


def test_require_signed_defaults_off(tmp_path):
    assert _require_signed(tmp_path / "home") is False  # no config at all


def test_require_signed_reads_the_flag(tmp_path):
    home = tmp_path / "home"
    _write_config(home, "[update]\nrequire_signed = true\n")
    assert _require_signed(home) is True
    _write_config(home, "[update]\nrequire_signed = false\n")
    assert _require_signed(home) is False
    _write_config(home, "[update]\nupstream_remote = 'origin'\n")  # key absent
    assert _require_signed(home) is False


def test_require_signed_honors_a_mistyped_string_true(tmp_path):
    # A security opt-in typed as a string must fail safe (on), not silently off.
    home = tmp_path / "home"
    _write_config(home, '[update]\nrequire_signed = "true"\n')
    assert _require_signed(home) is True


def test_require_signed_ignores_the_flag_under_other_tables(tmp_path):
    home = tmp_path / "home"
    _write_config(home, "[other]\nrequire_signed = true\n\n[update]\nupstream_branch = 'main'\n")
    assert _require_signed(home) is False


def test_require_signed_fails_closed_on_an_unreadable_config(tmp_path, monkeypatch):
    # A present-but-unreadable cohort.toml must refuse unsigned updates, not
    # silently disable the gate (e.g. the Python-3.10 tomllib gap this avoids).
    home = tmp_path / "home"
    _write_config(home, "[update]\nrequire_signed = true\n")
    monkeypatch.setattr(
        "cohort.update._config_text",
        lambda _h: (_ for _ in ()).throw(OSError("permission denied")),
    )
    assert _require_signed(home) is True


# === the gate in do_update ===================================================


def test_update_refuses_unsigned_upstream_when_required(tmp_path):
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")  # an ordinary (unsigned) upstream commit
    home = tmp_path / "home"
    _write_config(home, "[update]\nrequire_signed = true\n")
    before = _head(src)

    res = do_update(src, home, pip_run=_no_pip)

    assert res.status == "unsigned"
    assert res.ok is False  # exit 1
    assert "require_signed" in res.detail
    assert _head(src) == before  # refused before the fast-forward — nothing pulled


def test_update_dry_run_also_refuses_unsigned(tmp_path):
    # A preview must not imply an apply that would then be refused.
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    home = tmp_path / "home"
    _write_config(home, "[update]\nrequire_signed = true\n")

    res = do_update(src, home, dry_run=True, pip_run=_no_pip)
    assert res.status == "unsigned"


def test_update_default_off_pulls_unsigned_upstream(tmp_path):
    # The common clone-and-go flow is unchanged when the flag is absent.
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    res = do_update(src, tmp_path / "home", pip_run=_no_pip)  # no config
    assert res.status == "updated"
    assert _head(src) == _head(up)


def test_update_proceeds_when_signature_verifies(tmp_path, monkeypatch):
    # Gate wiring: require_signed=true + a verifiable tip → the update proceeds.
    # (verify-commit itself is exercised for real in the SSH test below.)
    up, src = _make_upstream_and_clone(tmp_path)
    _commit(up, "a.txt", "1\n")
    home = tmp_path / "home"
    _write_config(home, "[update]\nrequire_signed = true\n")
    monkeypatch.setattr("cohort.update._commit_is_signed", lambda *a, **k: True)

    res = do_update(src, home, pip_run=_no_pip)
    assert res.status == "updated"
    assert _head(src) == _head(up)


def test_commit_is_signed_fails_closed_on_unresolvable_sha(tmp_path):
    # Host-independent lock on the fail-closed contract (no ssh-keygen needed).
    _, src = _make_upstream_and_clone(tmp_path)
    assert _commit_is_signed(src, "0" * 40) is False  # no such object
    assert _commit_is_signed(src, "") is False        # empty → never verifies


# === real signature verification (skipped where ssh signing is unavailable) ==


def _ssh_signing_ready(tmp_path: Path) -> tuple[Path, Path] | None:
    """Generate an ed25519 key + allowed-signers file and confirm this host can
    sign and verify a commit with them; return (key, allowed_signers) or None."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "sign_key"
    kg = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "signer@e.st", "-f", str(key)],
        capture_output=True, text=True,
    )
    if kg.returncode != 0:
        return None
    pub = (tmp_path / "sign_key.pub").read_text(encoding="utf-8").strip()
    allowed = tmp_path / "allowed_signers"
    # principal (the committer email) + the public key line
    allowed.write_text(f"signer@e.st {pub}\n", encoding="utf-8")
    probe = tmp_path / "probe"
    probe.mkdir()
    _git(probe, "init", "-q", "-b", "main")
    for k, v in {
        "user.email": "signer@e.st", "user.name": "Signer",
        "gpg.format": "ssh", "user.signingkey": str(key),
        "gpg.ssh.allowedSignersFile": str(allowed), "commit.gpgsign": "true",
    }.items():
        _git(probe, "config", k, v)
    _commit(probe, "f.txt", "1\n", sign=True)
    ok = _git(probe, "verify-commit", "HEAD").returncode == 0
    return (key, allowed) if ok else None


def test_upstream_is_signed_verifies_a_real_ssh_signature(tmp_path):
    ready = _ssh_signing_ready(tmp_path / "probe-setup")
    if ready is None:
        pytest.skip("ssh commit signing/verification unavailable on this host")
    key, allowed = ready

    up, src = _make_upstream_and_clone(tmp_path)
    for k, v in {
        "user.email": "signer@e.st", "gpg.format": "ssh",
        "user.signingkey": str(key), "commit.gpgsign": "true",
    }.items():
        _git(up, "config", k, v)
    _commit(up, "signed.txt", "s\n", sign=True)

    # The clone needs the allowed-signers file to verify the fetched tip.
    _git(src, "config", "gpg.format", "ssh")
    _git(src, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    _git(src, "fetch", "-q", "origin")

    tip = _git(src, "rev-parse", "--verify", "origin/main^{commit}").stdout.strip()
    assert _commit_is_signed(src, tip) is True

    # And an unsigned tip fails verification (fail-closed). --no-gpg-sign is
    # needed to override up's commit.gpgsign=true, which auto-signs otherwise.
    (up / "unsigned.txt").write_text("u\n", encoding="utf-8")
    _git(up, "add", "-A")
    _git(up, "commit", "--no-gpg-sign", "-qm", "unsigned")
    _git(src, "fetch", "-q", "origin")
    tip2 = _git(src, "rev-parse", "--verify", "origin/main^{commit}").stdout.strip()
    assert _commit_is_signed(src, tip2) is False

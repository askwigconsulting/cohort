"""#105: `[update] signed_by` — pin the *identity* of the upstream signer.

`require_signed` (#30) proves the tip is signed by a key git trusts, not that it
is signed by the maintainer. `signed_by = ["SHA256:…"]` is the strict tier: the
tip's signing key must match a pinned fingerprint. A non-empty pin list implies
`require_signed`. Real SSH-signature tests skip where ssh signing is unavailable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cohort.install_model import CohortPaths
from cohort.update import (
    _commit_signer_allowed,
    _signed_by,
    _signing_key_fingerprints,
    do_update,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _write_config(home: Path, text: str) -> None:
    cfg = CohortPaths(home).cohort_home / "cohort.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")


def _no_pip(args: list) -> int:
    raise AssertionError(f"pip must not run here: {args}")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# === config reader (host-independent) ========================================


def test_signed_by_defaults_empty(tmp_path):
    assert _signed_by(tmp_path / "home") == []


def test_signed_by_parses_a_single_line_array(tmp_path):
    home = tmp_path / "home"
    _write_config(home, '[update]\nsigned_by = ["SHA256:aaa", "SHA256:bbb"]\n')
    assert _signed_by(home) == ["SHA256:aaa", "SHA256:bbb"]


def test_signed_by_ignores_the_key_under_other_tables(tmp_path):
    home = tmp_path / "home"
    _write_config(home, '[other]\nsigned_by = ["SHA256:x"]\n\n[update]\nupstream_branch = "main"\n')
    assert _signed_by(home) == []


def test_signed_by_parses_a_multi_line_array(tmp_path):
    # A hand-edited multi-line array must NOT silently parse to [] — that would
    # disable the gate while the user believes they pinned a key (#108 review).
    home = tmp_path / "home"
    _write_config(
        home,
        '[update]\nsigned_by = [\n  "SHA256:aaa",  # primary\n  "SHA256:bbb",\n]\n',
    )
    assert _signed_by(home) == ["SHA256:aaa", "SHA256:bbb"]


# === fingerprint extraction & matching (host-independent) =====================


class _FakeVerify:
    """Stand in for ``git verify-commit --raw`` with canned output, so the reject
    paths are exercised on hosts without ssh/gpg commit signing."""

    def __init__(self, returncode, stdout="", stderr="", raises=False):
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises

    def __call__(self, cmd, *args, **kwargs):
        if self.raises:
            raise OSError("git not found")
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


_FP_A = "A" * 40  # the real GPG signing-key fingerprint
_FP_C = "C" * 40  # a different, pinned fingerprint


def test_signing_key_fingerprints_reads_gpg_validsig_not_the_user_id():
    # The user-id on GOODSIG is set by the key's owner; only VALIDSIG names the key.
    raw = (
        "[GNUPG:] NEWSIG\n"
        f"[GNUPG:] GOODSIG DEADBEEF12345678 Evil {_FP_C}\n"
        f"[GNUPG:] VALIDSIG {_FP_A} 2026-01-01 0 0 4 0 1 8 00 {_FP_A}\n"
    )
    keys = _signing_key_fingerprints(raw)
    assert _FP_A in keys and _FP_C not in keys


def test_signing_key_fingerprints_reads_ssh_key_token_only():
    raw = 'Good "git" signature for SHA256:planted with ED25519 key SHA256:realkey\n'
    assert _signing_key_fingerprints(raw) == {"SHA256:realkey"}


def test_commit_signer_allowed_rejects_userid_spoof(monkeypatch):
    # rc==0 (some trusted key signed it) but the pinned string appears ONLY in the
    # attacker-controlled user-id; the real key is _FP_A. Must be refused.
    raw = (
        f"[GNUPG:] GOODSIG DEADBEEF12345678 pinned-{_FP_C}\n"
        f"[GNUPG:] VALIDSIG {_FP_A} 2026-01-01 0 0 4 0 1 8 00 {_FP_A}\n"
    )
    monkeypatch.setattr("cohort.update.subprocess.run", _FakeVerify(0, stderr=raw))
    assert _commit_signer_allowed(Path("."), "deadbeef", [_FP_C]) is False
    assert _commit_signer_allowed(Path("."), "deadbeef", [_FP_A]) is True  # real key matches


def test_commit_signer_allowed_ssh_whole_token_match(monkeypatch):
    raw = 'Good "git" signature for m@e.st with ED25519 key SHA256:realkey\n'
    monkeypatch.setattr("cohort.update.subprocess.run", _FakeVerify(0, stdout=raw))
    assert _commit_signer_allowed(Path("."), "sha", ["SHA256:realkey"]) is True
    assert _commit_signer_allowed(Path("."), "sha", ["SHA256:other"]) is False


def test_commit_signer_allowed_fails_closed(monkeypatch):
    # non-zero exit, unidentifiable key, and a subprocess error all refuse.
    monkeypatch.setattr("cohort.update.subprocess.run", _FakeVerify(1, stderr="bad signature"))
    assert _commit_signer_allowed(Path("."), "sha", [_FP_A]) is False
    monkeypatch.setattr("cohort.update.subprocess.run", _FakeVerify(0, stdout="Good signature\n"))
    assert _commit_signer_allowed(Path("."), "sha", [_FP_A]) is False  # rc 0 but no key token
    monkeypatch.setattr("cohort.update.subprocess.run", _FakeVerify(0, raises=True))
    assert _commit_signer_allowed(Path("."), "sha", [_FP_A]) is False


# === signed_by implies require_signed (host-independent) ======================


def test_signed_by_refuses_an_unsigned_tip(tmp_path):
    # An unsigned upstream + any pin → verify fails → refused, no real crypto needed.
    up = tmp_path / "up"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@e.st")
    _git(up, "config", "user.name", "T")
    _git(up, "config", "commit.gpgsign", "false")
    (up / "canonical").mkdir()
    (up / "canonical" / "x.md").write_text("x\n", encoding="utf-8")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "seed")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(up), str(src))
    (up / "a.md").write_text("a\n", encoding="utf-8")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "unsigned change")

    home = tmp_path / "home"
    _write_config(home, '[update]\nsigned_by = ["SHA256:nonexistent"]\n')
    before = _head(src)
    res = do_update(src, home, pip_run=_no_pip)
    assert res.status == "unsigned"
    assert "signed_by" in res.detail
    assert _head(src) == before  # refused before the fast-forward


# === real SSH-signature pinning (skipped where unavailable) ==================


def _ssh_signing(tmp_path: Path):
    """Generate an ed25519 key + allowed-signers file, confirm this host can sign
    and verify with them, and return (key, fingerprint, allowed) or None."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "sign_key"
    if subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "maint@e.st", "-f", str(key)],
        capture_output=True, text=True,
    ).returncode != 0:
        return None
    pub = (tmp_path / "sign_key.pub").read_text(encoding="utf-8").strip()
    fp_line = subprocess.run(
        ["ssh-keygen", "-lf", str(tmp_path / "sign_key.pub")], capture_output=True, text=True
    )
    if fp_line.returncode != 0:
        return None
    fingerprint = fp_line.stdout.split()[1]  # "256 SHA256:… comment ED25519"
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"maint@e.st {pub}\n", encoding="utf-8")
    probe = tmp_path / "probe"
    probe.mkdir()
    _git(probe, "init", "-q", "-b", "main")
    for k, v in {
        "user.email": "maint@e.st", "user.name": "M", "gpg.format": "ssh",
        "user.signingkey": str(key), "gpg.ssh.allowedSignersFile": str(allowed),
        "commit.gpgsign": "true",
    }.items():
        _git(probe, "config", k, v)
    (probe / "f").write_text("1\n", encoding="utf-8")
    _git(probe, "add", "-A")
    _git(probe, "commit", "-S", "-qm", "probe")
    if _git(probe, "verify-commit", "HEAD").returncode != 0:
        return None
    return key, fingerprint, allowed


def _signed_upstream_and_clone(tmp_path, key, allowed):
    """An upstream that signs its commits with `key`, and a clone that can verify
    them (allowedSignersFile configured)."""
    up = tmp_path / "up"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    for k, v in {
        "user.email": "maint@e.st", "user.name": "M", "gpg.format": "ssh",
        "user.signingkey": str(key), "commit.gpgsign": "true",
    }.items():
        _git(up, "config", k, v)
    (up / "canonical").mkdir()
    (up / "canonical" / "x.md").write_text("x\n", encoding="utf-8")
    _git(up, "add", "-A")
    _git(up, "commit", "-S", "-qm", "seed")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(up), str(src))
    _git(src, "config", "gpg.format", "ssh")
    _git(src, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    return up, src


def test_commit_signer_allowed_matches_and_rejects(tmp_path):
    ready = _ssh_signing(tmp_path / "setup")
    if ready is None:
        pytest.skip("ssh commit signing/verification unavailable on this host")
    key, fingerprint, allowed = ready
    _up, src = _signed_upstream_and_clone(tmp_path, key, allowed)
    tip = _git(src, "rev-parse", "--verify", "origin/main^{commit}").stdout.strip()

    assert _commit_signer_allowed(src, tip, [fingerprint]) is True
    assert _commit_signer_allowed(src, tip, ["SHA256:someone-elses-key"]) is False
    assert _commit_signer_allowed(src, tip, []) is False  # no pins → never allowed


def test_update_proceeds_when_tip_signed_by_a_pinned_key(tmp_path):
    ready = _ssh_signing(tmp_path / "setup")
    if ready is None:
        pytest.skip("ssh commit signing/verification unavailable on this host")
    key, fingerprint, allowed = ready
    up, src = _signed_upstream_and_clone(tmp_path, key, allowed)
    (up / "a.md").write_text("a\n", encoding="utf-8")
    _git(up, "add", "-A")
    _git(up, "commit", "-S", "-qm", "signed change")

    home = tmp_path / "home"
    _write_config(home, f'[update]\nsigned_by = ["{fingerprint}"]\n')
    res = do_update(src, home, pip_run=_no_pip)
    assert res.status == "updated"
    assert _head(src) == _head(up)


def test_update_refuses_when_tip_signed_by_an_unpinned_key(tmp_path):
    ready = _ssh_signing(tmp_path / "setup")
    if ready is None:
        pytest.skip("ssh commit signing/verification unavailable on this host")
    key, _fingerprint, allowed = ready
    up, src = _signed_upstream_and_clone(tmp_path, key, allowed)
    (up / "a.md").write_text("a\n", encoding="utf-8")
    _git(up, "add", "-A")
    _git(up, "commit", "-S", "-qm", "signed by the wrong key")

    home = tmp_path / "home"
    # A validly-signed tip, but NOT by the pinned fingerprint → refused.
    _write_config(home, '[update]\nsigned_by = ["SHA256:a-different-maintainer-key"]\n')
    before = _head(src)
    res = do_update(src, home, pip_run=_no_pip)
    assert res.status == "unsigned"
    assert _head(src) == before

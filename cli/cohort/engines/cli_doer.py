"""External-CLI doers — dispatch a vendor's own agentic CLI as a *write* doer, confined
to an isolated git worktree.

Where :mod:`cohort.engines.patch_proposal` has the engine return a diff that Cohort
applies, a CLI doer lets the vendor's own agentic loop edit files directly — it can
iterate and run its own tests. Safety rests on OS-level confinement plus a throwaway
worktree, never on trusting the engine:

* **Codex (ChatGPT)** runs under its own sandbox — ``codex exec --sandbox
  workspace-write -C <worktree>`` — so every model-generated write and shell command is
  confined to the worktree by the OS (Landlock/seccomp on Linux, Seatbelt on macOS).
* The worktree is a **detached checkout off HEAD** — committed files only, so no
  untracked secret (a git-ignored ``.env``) is ever exposed — and it is discarded if the
  run is rejected.
* The task **egresses to the vendor**, so the repo's egress opt-out and a secret scan on
  the task text gate the dispatch *before* the CLI is invoked.
* Nothing is committed or merged here. The coordinator reviews the diff and integrates;
  the human PR-reviews. A change that lands outside a declared footprint is surfaced.

**grok-cli is wired through a Cohort-imposed sandbox.** grok-cli has no sandbox,
read-only, or approval mode of its own, so — unlike Codex, which confines itself —
Cohort runs it inside a **bubblewrap** jail rooted at the worktree: writes are confined
to the worktree by the kernel and the user's real home is not mounted. bubblewrap does
**no** host egress filtering, so the network is fully open (not restricted to the xAI
API). The secret-exfiltration surface an open network would otherwise create is closed
two other ways instead: grok-cli inherits a **scrubbed, minimal environment** — only
``PATH``, an ephemeral ``HOME``, and ``GROK_API_KEY`` (plus optional base-URL/model), and
never the host environment's other secrets — and the worktree exposes only committed
files, which are secret-scanned before dispatch (`run_grok_in_worktree`). Requires
``bwrap`` (Linux); where it is absent the doer refuses rather than run grok unconfined,
and the gated agentic patch_proposal (``cohort engine propose grok --agentic``) remains
the fallback contained write path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cohort.engines import gates
from cohort.engines import patch_proposal

# A hard wall-clock cap on an external doer run — an agentic CLI that has not finished
# in this long is stuck or looping; kill it (the worktree is cleaned up on timeout).
_DOER_TIMEOUT_SECONDS: float = 900.0

# Engine names that resolve to the Codex-sandboxed doer.
_CODEX_ENGINE_ALIASES = frozenset({"gpt", "chatgpt", "codex", "openai"})

# Engine names that resolve to the bubblewrap-sandboxed grok-cli doer.
_GROK_ENGINE_ALIASES = frozenset({"grok", "xai"})
# Bound grok-cli's agentic loop (its own default is 400 rounds — far too loose here).
_GROK_MAX_TOOL_ROUNDS = "60"

# Environment variables each vendor CLI legitimately needs, beyond PATH/HOME. Every
# other host variable is scrubbed (see :func:`_scrubbed_env`) so an untrusted vendor
# CLI cannot read a host secret (``AWS_*``, ``GITHUB_TOKEN``, another repo's tokens) out
# of an inherited environment and exfiltrate it over the open network. The keys ride the
# environment, never argv.
_GROK_ENV_PASSTHROUGH = ("GROK_API_KEY", "GROK_BASE_URL", "GROK_MODEL")
_CODEX_ENV_PASSTHROUGH = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")
# Non-secret TLS trust-store pointers (file/dir paths, not credentials) the node-based
# vendor CLIs need to reach their HTTPS API behind a corporate CA — kept for every doer
# so the scrub doesn't silently break TLS. Proxy vars (which can embed credentials) are
# deliberately NOT here; a user behind an authenticating proxy widens the allow-list.
_TLS_ENV = ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE")


def _scrubbed_env(*, home: Path, passthrough: tuple[str, ...]) -> dict[str, str]:
    """Build a minimal environment for a vendor CLI, dropping every host secret.

    The returned dict carries only what the CLI needs: ``PATH`` (so the node runtime and
    the CLI binary resolve), ``HOME`` (pointed at ``home``), a couple of harmless
    locale/terminal vars, and each name in ``passthrough`` that is actually set in the
    host environment (the CLI's own API key / config location). Everything else in the
    host environment — ``AWS_*``, ``GITHUB_TOKEN``, other repos' credentials — is
    dropped, closing the secret-exfiltration path an inherited environment opens when the
    sandbox leaves the network open. The CLI receives *only* this dict, never
    ``os.environ``.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
    }
    for name in ("LANG", "LC_ALL", "TERM", *_TLS_ENV, *passthrough):
        value = os.environ.get(name)
        if value is None:
            continue
        # A *_URL config var carrying embedded userinfo (https://user:pass@host) would
        # hand those credentials to the untrusted CLI — drop it (the CLI falls back to
        # its default endpoint), same rationale as excluding proxy vars.
        if name.endswith("URL") and "@" in urlsplit(value).netloc:
            continue
        env[name] = value
    return env


def _assert_worktree_files_have_no_secrets(worktree: Path) -> None:
    """Secret-scan the committed files the worktree will expose to the vendor CLI.

    The outbound task string is gated separately, but the vendor CLI then *reads the
    worktree's tracked files and sends them to the vendor* — so those file contents are
    egress too and must clear the same secret gate the agentic patch_proposal path
    applies per read. The worktree is a detached checkout of HEAD, so only committed
    files are present (a git-ignored ``.env`` is never checked out); this scans those
    tracked contents and refuses before dispatch on any credential-shaped hit.

    Raises:
        gates.SecretFoundError: a tracked file carries credential-shaped content. The
            message lists only non-secret finding labels, never a matched value.
    """
    tracked = [rel for rel in _git(worktree, "ls-files", "-z").split("\0") if rel]
    labels: set[str] = set()
    for rel in tracked:
        try:
            raw = (worktree / rel).read_bytes()
        except OSError as exc:
            # A tracked file we cannot read is one we cannot clear. Fail CLOSED — refuse
            # dispatch rather than silently skip it (the engine may still read it).
            raise gates.SecretFoundError(
                f"worktree file {rel!r} could not be read for secret scanning "
                f"({type(exc).__name__}); refusing to dispatch"
            ) from exc
        # Decode latin-1 (lossless byte->char), not utf-8-with-errors-ignored: an
        # ASCII-shaped credential embedded in a binary blob stays visible to the scanner
        # instead of being dropped with the surrounding invalid bytes.
        labels.update(gates.scan_for_secrets(raw.decode("latin-1")))
    if labels:
        raise gates.SecretFoundError(
            "worktree files contain credential-shaped content: "
            + ", ".join(sorted(labels))
        )


class DoerError(Exception):
    """Base class for CLI-doer failures."""


class DoerUnavailableError(DoerError):
    """The requested vendor CLI is not installed, or the engine has no CLI doer.

    Grok raises this on purpose: its CLI is non-functional, so callers are pointed at
    the agentic patch_proposal instead."""


@dataclass
class DoerResult:
    """The outcome of a CLI-doer run. The worktree is left in place for the coordinator
    to review and integrate; ``changed_files``/``diff`` are what the engine wrote."""

    engine: str
    worktree: Path
    changed_files: list[str]
    diff: str
    returncode: int
    stdout_tail: str
    footprint_violations: list[str] = field(default_factory=list)


def run_codex_in_worktree(
    worktree: Path,
    task: str,
    *,
    model: str | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Run Codex confined to an *existing* worktree under its ``workspace-write`` sandbox.

    Does not create or clean up the worktree - the caller owns it. Shared by the one-shot
    :func:`run_codex_doer` and the ratchet loop, which reuses one worktree across
    iterations.

    Raises:
        DoerUnavailableError: the ``codex`` CLI is not installed.
    """
    if shutil.which("codex") is None:
        raise DoerUnavailableError(
            "the 'codex' CLI is not installed; install it and run 'codex login' to use "
            "the Codex doer"
        )
    cmd = [
        "codex", "exec",
        "--sandbox", "workspace-write",  # writes/commands confined to the worktree (OS)
        "-C", str(worktree),
        "--skip-git-repo-check",
    ]
    if model:
        cmd += ["-m", model]
    cmd.append(task)
    # Defense in depth: Codex's own sandbox already disables network, but hand it a
    # scrubbed, minimal environment anyway so it never even sees host secrets. HOME is
    # the real home so `codex login` credentials (~/.codex) resolve; its own API key /
    # config-dir override ride the passthrough allow-list.
    env = _scrubbed_env(home=Path.home(), passthrough=_CODEX_ENV_PASSTHROUGH)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _git(worktree: Path, *args: str) -> str:
    """Run a read-only-ish git query inside the worktree and return stdout."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _footprint_violations(changed: list[str], footprint: list[str] | None) -> list[str]:
    """Report changed paths outside a declared footprint (or in a sensitive class).

    Advisory, not enforcing: the OS sandbox already confines writes to the worktree, so
    this surfaces "the engine edited something you didn't scope it to" for the human
    reviewer, rather than blocking (the engine already ran)."""
    if not footprint:
        return []
    return gates.check_changed_paths(changed, allowed_footprint=footprint)


def _bwrap() -> str | None:
    """Path to the ``bwrap`` (bubblewrap) binary, or None if it isn't installed."""
    return shutil.which("bwrap")


def _assert_grok_sandbox_available() -> None:
    """Refuse the grok doer unless both grok-cli and bubblewrap are present — Cohort
    will not run grok unconfined."""
    if shutil.which("grok") is None:
        raise DoerUnavailableError(
            "the 'grok' CLI is not installed; install grok-cli and set GROK_API_KEY to "
            "use the Grok doer"
        )
    if _bwrap() is None:
        raise DoerUnavailableError(
            "bubblewrap ('bwrap') is required to confine grok-cli's native writes and is "
            "not installed — without it grok would run unsandboxed. Install bwrap (Linux), "
            "or use 'cohort engine propose grok --agentic' for the gated patch path"
        )


def _grok_sandbox_argv(worktree: Path, sandbox_home: Path, inner: list[str]) -> list[str]:
    """A bubblewrap invocation that runs ``inner`` with ``worktree`` as the ONLY
    persistent writable path.

    grok-cli has no sandbox of its own, so Cohort imposes one with the kernel: system
    dirs are read-only, ``HOME`` is an ephemeral tmpfs (grok-cli's history/logs are
    discarded), and grok-cli's ``~/.grok`` settings are bind-mounted read-only so it
    keeps the user's base-URL/model. The user's real home — SSH keys, dotfiles, other
    repos — is NOT mounted, so grok can neither read a secret outside the worktree nor
    write anywhere but the worktree. This is the OS-level confinement Codex gets from
    ``--sandbox workspace-write``, applied from the outside.

    The network is left **fully open** — bubblewrap does no host egress filtering, so
    this is emphatically not a restriction to the xAI API. Confinement of *secrets*
    therefore rests not on the network but on (a) the read-only, committed-only worktree
    and (b) the scrubbed, minimal environment the caller hands the process (see
    :func:`_scrubbed_env`): the API key rides that environment, never argv, and no other
    host secret rides at all."""
    argv = [
        _bwrap() or "bwrap",
        "--unshare-all", "--share-net",   # isolate PID/IPC/UTS/etc; keep network for the API
        "--die-with-parent",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/etc", "/etc",
    ]
    # /lib /lib64 /bin /sbin are real dirs on some distros and usr-merge symlinks on
    # others; bind whichever actually exist so the node runtime resolves either way.
    for p in ("/lib", "/lib64", "/bin", "/sbin"):
        if Path(p).exists():
            argv += ["--ro-bind", p, p]
    argv += ["--tmpfs", str(sandbox_home)]  # ephemeral, writable HOME (grok-cli scratch)
    grok_cfg = Path.home() / ".grok"
    if grok_cfg.is_dir():
        argv += ["--ro-bind", str(grok_cfg), str(sandbox_home / ".grok")]
    argv += [
        "--bind", str(worktree), str(worktree),  # the ONLY persistent writable path
        "--chdir", str(worktree),
        "--setenv", "HOME", str(sandbox_home),
        "--",
        *inner,
    ]
    return argv


def run_grok_in_worktree(
    worktree: Path,
    task: str,
    *,
    model: str | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Run grok-cli confined to an *existing* worktree by a bubblewrap sandbox.

    grok writes natively (it is a full agentic editor), but the kernel confines every
    write to the worktree — the containment grok-cli lacks on its own. The caller owns
    the worktree lifecycle. ``GROK_API_KEY`` reaches grok-cli through a **scrubbed,
    minimal environment** (never the full host environment, never argv); every host
    secret outside the grok allow-list is dropped so grok cannot read it and exfiltrate
    it over the open network.

    Raises:
        DoerUnavailableError: the ``grok`` CLI or ``bwrap`` is not installed.
    """
    _assert_grok_sandbox_available()
    inner = ["grok", "-d", str(worktree), "--max-tool-rounds", _GROK_MAX_TOOL_ROUNDS]
    if model:
        inner += ["-m", model]
    inner += ["-p", task]
    argv = _grok_sandbox_argv(worktree, Path.home(), inner)
    # The sandbox leaves the network open, so scrub the environment to a minimal
    # allow-list: grok-cli receives ONLY this dict, never the host environment's secrets.
    env = _scrubbed_env(home=Path.home(), passthrough=_GROK_ENV_PASSTHROUGH)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)


def run_grok_doer(
    task: str,
    *,
    repo_root: Path,
    model: str | None = None,
    footprint: list[str] | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
    project_context_text: str = "",
) -> DoerResult:
    """Dispatch grok-cli as a write doer, natively editing a fresh worktree under a
    bubblewrap sandbox that confines every write to that worktree.

    The trust story mirrors the Codex doer, but the confinement is Cohort-imposed
    (bubblewrap) rather than CLI-provided: the worktree is a detached checkout (committed
    files only — no untracked ``.env`` leaks), the outbound task is egress- and
    secret-gated before grok is invoked, and nothing is committed or merged — the
    coordinator reviews the diff, the human PR-reviews.

    Raises:
        DoerError: empty task.
        DoerUnavailableError: the ``grok`` CLI or ``bwrap`` is not installed.
        EgressBlockedError / SecretFoundError: the repo opted out of egress, or the task
            (or a committed worktree file grok would send to xAI) carries
            credential-shaped content (gated before grok runs).
    """
    if not task.strip():
        raise DoerError("task is empty")

    _assert_grok_sandbox_available()  # fail fast — never create a worktree we can't use
    gates.require_egress_allowed(project_context_text)
    gates.assert_no_secrets(task)

    worktree = patch_proposal._create_worktree(repo_root)
    try:
        # The vendor CLI reads the worktree's committed files and sends them to xAI, so
        # scan those contents (not just the task string) and refuse before dispatch.
        _assert_worktree_files_have_no_secrets(worktree)
        proc = run_grok_in_worktree(worktree, task, model=model, timeout=timeout)
        _git(worktree, "add", "-A")
        diff = _git(worktree, "diff", "--cached")
        changed = [n for n in _git(worktree, "diff", "--cached", "--name-only").splitlines() if n]
        return DoerResult(
            engine="grok",
            worktree=worktree,
            changed_files=changed,
            diff=diff,
            returncode=proc.returncode,
            stdout_tail=(proc.stdout or "")[-2000:],
            footprint_violations=_footprint_violations(changed, footprint),
        )
    except BaseException:
        patch_proposal.cleanup_worktree(repo_root, worktree)
        raise


def run_codex_doer(
    task: str,
    *,
    repo_root: Path,
    model: str | None = None,
    footprint: list[str] | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
    project_context_text: str = "",
) -> DoerResult:
    """Dispatch Codex (ChatGPT) as a write doer confined to a fresh worktree.

    Raises:
        DoerError: empty task.
        DoerUnavailableError: the ``codex`` CLI is not installed.
        EgressBlockedError / SecretFoundError: the repo opted out of egress, or the task
            (or a committed worktree file the CLI would send to the vendor) contains
            credential-shaped content (gated before the CLI is invoked).
    """
    if not task.strip():
        raise DoerError("task is empty")

    # Gate the outbound task BEFORE spawning the CLI: honor the repo egress opt-out and
    # refuse a task that carries a secret. (The CLI then reads only committed worktree
    # files, and its writes are OS-confined to the worktree.)
    gates.require_egress_allowed(project_context_text)
    gates.assert_no_secrets(task)

    worktree = patch_proposal._create_worktree(repo_root)
    try:
        # The CLI reads the worktree's committed files and sends them to the vendor, so
        # scan those contents (not just the task string) and refuse before dispatch.
        _assert_worktree_files_have_no_secrets(worktree)
        proc = run_codex_in_worktree(worktree, task, model=model, timeout=timeout)

        # Capture what the engine wrote as a reviewable diff.
        _git(worktree, "add", "-A")
        diff = _git(worktree, "diff", "--cached")
        changed = [n for n in _git(worktree, "diff", "--cached", "--name-only").splitlines() if n]
        return DoerResult(
            engine="gpt",
            worktree=worktree,
            changed_files=changed,
            diff=diff,
            returncode=proc.returncode,
            stdout_tail=(proc.stdout or "")[-2000:],
            footprint_violations=_footprint_violations(changed, footprint),
        )
    except BaseException:
        # TimeoutExpired, a git failure, or Ctrl-C: never leak the worktree.
        patch_proposal.cleanup_worktree(repo_root, worktree)
        raise


def run_doer(engine: str, task: str, **kwargs) -> DoerResult:
    """Dispatch ``engine``'s CLI doer. Codex-family engines are sandboxed by the CLI
    itself; Grok is sandboxed by Cohort via bubblewrap (grok-cli has no sandbox of its
    own). Either way the write is OS-confined to a throwaway worktree."""
    name = engine.strip().lower()
    if name in _CODEX_ENGINE_ALIASES:
        return run_codex_doer(task, **kwargs)
    if name in _GROK_ENGINE_ALIASES:
        return run_grok_doer(task, **kwargs)
    raise DoerUnavailableError(f"no CLI doer for engine {engine!r}")

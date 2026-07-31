"""External-CLI doers — dispatch a vendor's own agentic CLI as a *write* doer, confined
to an isolated git worktree.

Where :mod:`cohort.engines.patch_proposal` has the engine return a diff that Cohort
applies, a CLI doer lets the vendor's own agentic loop edit files directly — it can
iterate and run its own tests. Safety rests on OS-level confinement plus a throwaway
worktree, never on trusting the engine:

* **Codex (ChatGPT)** runs under its own sandbox — ``codex exec --sandbox
  workspace-write -C <worktree>`` — so every model-generated write and shell command is
  confined to the worktree by the OS (Landlock/seccomp on Linux, Seatbelt on macOS).

  **Known residual read-exposure (codex).** ``workspace-write`` confines *writes* to the
  worktree, but it does **not** confine *reads*: codex's own read tools can read files
  anywhere on the host filesystem and include their contents in the transcript it sends
  to OpenAI. codex's stable ``codex exec`` interface offers no read-scoping — its
  ``--sandbox`` choices are only ``read-only`` (which would disable the doer's whole
  purpose), ``workspace-write`` (full-disk read), and ``danger-full-access``; ``--add-dir``
  widens *writable* roots, not readable ones. The newer permissions-profile model can
  express restricted read roots, but the legacy Landlock backend that ``workspace-write``
  uses explicitly rejects it ("Restricted read-only access is not supported by the legacy
  Linux Landlock filesystem backend"), and there is no version-stable, cross-platform
  (Landlock/Seatbelt) way to reach a read-scoped-write mode from ``codex exec``. Pinning a
  fix to one codex build's internal config would fail *open* — silently no-op — on any
  other build, which is worse than a documented limitation. So the read-exposure is not
  closed here; it is *reduced*: the child gets a scrubbed, minimal environment
  (:func:`_scrubbed_env`), so no host secret rides the process environment, and the
  worktree it edits holds only committed, secret-scanned files. A repo that must keep
  files off a vendor should use the egress opt-out or the gated ``patch_proposal`` path.
* The worktree is a **detached checkout off HEAD** — committed files only, so no
  untracked secret (a git-ignored ``.env``) is ever exposed — and it is discarded if the
  run is rejected.
* The task **egresses to the vendor**, so the repo's egress opt-out and a secret scan on
  the task text gate the dispatch *before* the CLI is invoked. The CLI also reads and
  sends the worktree's committed files, so a hard **total wire-byte cap** bounds the whole
  exposed payload (task text + tracked file bytes) fail-closed before dispatch, alongside
  a secret scan of those same files.
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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from cohort.engines import gates
from cohort.engines import patch_proposal

# A hard wall-clock cap on an external doer run — an agentic CLI that has not finished
# in this long is stuck or looping; kill it (the worktree is cleaned up on timeout).
_DOER_TIMEOUT_SECONDS: float = 900.0

# Every git query Cohort runs around a doer is a fast, local, read-only plumbing call, so a
# short cap is right: past this it is stalled (an NFS hang, a held index.lock), not slow.
# Bounding these matters because they sit on paths a user is waiting on.
_GIT_TIMEOUT_SECONDS: float = 30.0

# A hard ceiling on the TOTAL bytes a doer dispatch may expose to the vendor — the task
# text plus every tracked worktree file the CLI reads and sends. gates.assert_payload_within
# bounds only the prompt; this bounds the whole egress so a runaway worktree (a checked-in
# data blob or vendored binary tree) can never be silently shipped off-machine. The threat
# this cap targets is a runaway data/binary blob (a checked-in dataset, a vendored binary
# tree), not normal source: 50MB clears normal-to-large source repositories while still
# catching a bulk data dump. A repo legitimately larger should scope the doer to a subtree
# or raise this deliberately.
_DEFAULT_MAX_WIRE_BYTES: int = 50_000_000

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


def _tracked_worktree_files(worktree: Path) -> list[str]:
    """List the worktree's tracked (committed) files — the exact set a vendor CLI can
    read and egress. Shared by the secret scan and the wire-byte cap so both gate the
    same set of files. NUL-delimited so a path with an embedded newline is not split."""
    return [rel for rel in _git(worktree, "ls-files", "-z").split("\0") if rel]


def _worktree_exposed_byte_count(worktree: Path) -> int:
    """Sum the byte length of every tracked file the vendor CLI could read and send.

    Fail CLOSED: a tracked file whose size cannot be measured is one whose exposure we
    cannot bound, so raise (refuse the dispatch) rather than silently drop it from the
    sum — an undercounted payload could slip past the wire cap. ``stat().st_size`` is the
    on-disk byte length of the committed file in the detached checkout.

    Raises:
        gates.PayloadTooLargeError: a tracked file could not be measured; the dispatch is
            refused because its exposed byte count cannot be bounded.
    """
    total = 0
    for rel in _tracked_worktree_files(worktree):
        try:
            total += (worktree / rel).stat().st_size
        except OSError as exc:
            raise gates.PayloadTooLargeError(
                f"worktree file {rel!r} could not be measured for the wire-byte cap "
                f"({type(exc).__name__}); refusing to dispatch"
            ) from exc
    return total


def _assert_worktree_within_wire_budget(
    worktree: Path, task: str, *, max_wire_bytes: int
) -> None:
    """Refuse the dispatch if task text + tracked worktree bytes exceed the wire cap.

    The vendor CLI reads and sends the worktree's committed files, so the total egress is
    the task text plus those file bytes, not just the prompt. Counts the file bytes
    fail-closed (an unmeasurable file refuses) and delegates the ceiling check to
    :func:`gates.assert_total_wire_bytes`.

    Raises:
        gates.PayloadTooLargeError: the total exposed payload exceeds ``max_wire_bytes``,
            or a tracked file could not be measured.
    """
    file_bytes = _worktree_exposed_byte_count(worktree)
    gates.assert_total_wire_bytes(
        instruction_text=task, file_bytes=file_bytes, max_bytes=max_wire_bytes
    )


def _split_secret_allowlist(
    repo_root: Path, worktree: Path
) -> tuple[frozenset[gates.SecretSuppression], frozenset[gates.SecretSuppression]]:
    """Split declared suppressions into ``(reviewed, unreviewed)``.

    **Reviewed** entries are reachable from the **default branch**, so they have already
    been through whatever review that branch enforces. They apply silently.

    **Unreviewed** entries are committed on this branch only. They are not rejected — a
    developer adding a fake fixture is the ordinary case, and refusing outright merely
    re-blocks the engines on the very repo whose fixtures they are (which is exactly what
    a stricter first cut did). They instead require a live human decision.

    The threat this separation targets is narrower than "someone committed a suppression".
    The refusal message prints paste-ready declaration lines *into the caller's context*,
    so an agent at supervised or autopilot can commit them and re-dispatch with nobody
    looking — and egress is irreversible. A human at a keyboard confirming once is a fine
    authorisation; an agent silently authorising itself is not. So the gate asks, and an
    unattended caller (no TTY: CI, a hook, an agent's shell) is refused by default.
    """
    reviewed = gates.parse_secret_allowlist(
        default_branch_file(repo_root, gates.SECRET_ALLOWLIST_PATH) or ""
    )
    try:
        # Committed-on-this-branch, so it still shows up in a PR diff — the manifest is
        # never read from an uncommitted edit.
        local_text = (worktree / gates.SECRET_ALLOWLIST_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return reviewed, frozenset()
    return reviewed, frozenset(gates.parse_secret_allowlist(local_text) - reviewed)


def _confirm_unreviewed_suppressions(
    entries: list[tuple[str, gates.SecretFinding]]
) -> bool:
    """Ask a human, on a TTY, to authorise suppressions not yet on the default branch.

    Returns False without prompting when stdin is not a terminal. That is the whole point:
    an agent's shell, a CI job, and a git hook all run without one, so none of them can
    authorise an irreversible egress on their own. A human gets one prompt.
    """
    if not sys.stdin.isatty():
        return False
    print(
        "\nThese credential-shaped values are declared fake in "
        f"{gates.SECRET_ALLOWLIST_PATH}, but that declaration is NOT yet on the default "
        "branch, so nobody else has reviewed it:",
        file=sys.stderr,
    )
    for rel, finding in entries:
        print(f"  {finding.label:34s} {rel}", file=sys.stderr)
    print(
        "\nConfirm ONLY if you have checked these are fake. Approving a real credential "
        "here sends it to the vendor, and that cannot be undone.",
        file=sys.stderr,
    )
    try:
        answer = input("Send these files to the vendor? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _default_branch_ref(repo_root: Path) -> str | None:
    """Resolve the repo's default-branch ref, preferring the remote's own declaration.

    ``origin/HEAD`` is what the remote says its default is, so it cannot be pointed
    somewhere else by a local branch. Falls back to a local ``main``/``master`` only when no
    remote HEAD is configured (a repo cloned without one, or a purely local repo).
    """
    for args in (
        ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        ["rev-parse", "--verify", "--quiet", "refs/heads/main"],
        ["rev-parse", "--verify", "--quiet", "refs/heads/master"],
    ):
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0 and proc.stdout.strip():
            # symbolic-ref yields the ref name; rev-parse yields a hash. Either is a
            # valid revision to read a blob out of.
            return proc.stdout.strip()
    return None


def committed_file(repo_root: Path, rel: str, ref: str = "HEAD") -> str | None:
    """Return ``rel`` as committed at ``ref``, or None if unavailable.

    Used where there is no worktree to read from (the API reviewer). Reading the *live*
    file instead would let an uncommitted edit widen what may egress, which is the gap
    audit r3 flagged as M2.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{ref}:{rel}"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def default_branch_file(repo_root: Path, rel: str) -> str | None:
    """Return ``rel`` as committed on the default branch, or None if unavailable."""
    ref = _default_branch_ref(repo_root)
    if ref is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{ref}:{rel}"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _assert_worktree_files_have_no_secrets(
    worktree: Path,
    repo_root: Path,
    *,
    confirm: Callable[[list[tuple[str, gates.SecretFinding]]], bool] | None = None,
) -> None:
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
    # Resolved at call time, not bound as a default, so the prompt stays substitutable.
    confirm = confirm or _confirm_unreviewed_suppressions
    tracked = _tracked_worktree_files(worktree)
    reviewed, unreviewed = _split_secret_allowlist(repo_root, worktree)
    blocking: list[tuple[str, gates.SecretFinding]] = []
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
        findings = gates.scan_for_secret_findings(raw.decode("latin-1"))
        if not findings:
            continue
        blocking.extend(
            (rel, finding)
            for finding in gates.unsuppressed_findings(findings, rel, reviewed)
        )

    # Of what the reviewed manifest did not clear, separate the findings this branch has
    # *declared* (but nobody else has seen) from the ones nothing declares at all. Only
    # the former are a human's to wave through; the latter are undeclared secrets.
    pending = [
        (rel, finding)
        for rel, finding in blocking
        if not gates.unsuppressed_findings([finding], rel, unreviewed)
    ]
    undeclared = [item for item in blocking if item not in pending]

    if pending and not undeclared:
        if confirm(pending):
            return
        raise gates.SecretFoundError(
            "dispatch refused: "
            + ", ".join(sorted({finding.label for _, finding in pending}))
            + f" are declared fake in {gates.SECRET_ALLOWLIST_PATH}, but that declaration "
            "is not on the default branch and was not confirmed.\n\nEither merge the "
            "manifest change, or re-run this from an interactive terminal to confirm it. "
            "An unattended caller — CI, a hook, an agent's shell — cannot authorise its "
            "own egress, because egress cannot be undone."
        )

    blocking = undeclared or blocking
    if blocking:
        labels = sorted({finding.label for _, finding in blocking})
        # Print the exact manifest lines that would clear each finding. Without this the
        # only way to unblock is to guess digests, which pushes people toward exempting
        # whole paths — the blind spot this design exists to avoid.
        declarations = "\n".join(
            f"  {finding.digest}  {rel}"
            for rel, finding in sorted(blocking, key=lambda item: (item[0], item[1]))
        )
        raise gates.SecretFoundError(
            "worktree files contain credential-shaped content: "
            + ", ".join(labels)
            + f"\n\nIf — and only if — a human has confirmed these values are fake, declare "
            f"them in {gates.SECRET_ALLOWLIST_PATH}:\n"
            + declarations
            + "\n\nAn entry already on the default branch applies silently. One committed "
            "only on this branch still works, but must be confirmed interactively — an "
            "unattended caller cannot authorise its own irreversible egress."
        )


class DoerError(Exception):
    """Base class for CLI-doer failures."""


class DoerFailedError(DoerError):
    """The vendor CLI was invoked but produced no usable result.

    Distinct from :class:`DoerUnavailableError` (the CLI is not installed) and from a
    gate refusal (which is a deliberate *refusal* and must never be retried elsewhere).
    This is the third case: a CLI that is present and permitted, but broken here — it
    exited non-zero, could not be executed inside the sandbox, or returned nothing.

    It exists because the alternative is worse than an error: a CLI that fails while
    exiting 0 with empty output otherwise reads as "the engine reviewed your repo and had
    nothing to say". A caller may treat this as grounds to fall back to the vendor's API
    transport **with a printed note** — never silently.
    """


class DoerUnavailableError(DoerError):
    """The requested vendor CLI is not installed, or the engine has no CLI doer.

    grok-cli is the preferred grok doer (real, worktree-scoped file access under
    bubblewrap); this is raised only when it cannot run confined here — i.e. the ``grok``
    CLI or ``bwrap`` is absent. In that case callers fall back to the gated agentic
    patch_proposal path (``cohort engine propose grok --agentic``)."""


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


@dataclass
class GrokReviewResult:
    """The outcome of a repo-read-only grok-CLI review run (:func:`run_grok_review`).

    Repo-read-only means the *repo* is left untouched (the worktree is discarded), not that
    grok is denied writes: grok still runs write-capable inside the bubblewrap jail — its
    edits land in the throwaway worktree and are thrown away with it. Unlike
    :class:`DoerResult`, there is no surviving worktree and no diff: the read path keeps
    grok's *answer*, not its edits, and discards the throwaway worktree grok explored (any
    writes grok made there were confined by bubblewrap and thrown away with it). For a CLI
    run grok's stdout IS the transcript — there is no separate JSONL file — so
    ``transcript`` is the full captured stdout stream and ``analysis`` is that same text,
    surfaced as grok's report."""

    engine: str
    analysis: str
    transcript: str
    returncode: int


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
    # This doer is non-interactive: close stdin (DEVNULL) so a TTY the child inherits can
    # never be used to inject keystrokes (TIOCSTI), and start_new_session=True puts it in
    # its own process group.
    #
    # KNOWN GAP (audit r3, M5): on timeout, subprocess.run kills only the DIRECT child, so
    # helpers codex spawned can outlive the wall-clock cap — still burning API spend, and
    # still writing into a worktree Cohort is about to delete. start_new_session makes a
    # group kill *possible* but does not perform one. Closing this needs the child pid
    # (i.e. Popen), so it is tracked separately rather than bolted on here.
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _git(worktree: Path, *args: str) -> str:
    """Run a read-only-ish git query inside the worktree and return stdout.

    Bounded: these run after a doer on a path the user is waiting on, so a stalled git
    (a held ``index.lock``, an NFS hang) must not hang the CLI forever — the same rule
    ``gitutil`` and ``update`` already follow.
    """
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
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


def _grok_cli_available() -> bool:
    """Whether the locally-installed, bubblewrap-sandboxed grok CLI can run here.

    True only when BOTH the ``grok`` CLI and ``bwrap`` are installed: grok-cli has no
    sandbox of its own, so Cohort will only run it confined by bubblewrap (see
    :func:`_grok_sandbox_argv`). Callers use this to prefer the local CLI (which has
    real, worktree-scoped file access) and fall back to the xAI API-direct path when it
    returns False. Mirrors the two checks :func:`_assert_grok_sandbox_available` enforces,
    as a boolean rather than a raise."""
    return shutil.which("grok") is not None and _bwrap() is not None


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

    grok-cli has no sandbox of its own, so Cohort imposes one with the kernel: ``/usr``
    (and the usr-merge lib/bin dirs) is read-only, ``HOME`` is an ephemeral tmpfs
    (grok-cli's history/logs are discarded), and grok-cli's ``~/.grok`` settings are
    bind-mounted read-only so it keeps the user's base-URL/model. The user's real home —
    SSH keys, dotfiles, other repos — is NOT mounted, and grok cannot write anywhere but
    the worktree. This is the OS-level confinement Codex gets from ``--sandbox
    workspace-write``, applied from the outside.

    **Known residual read-exposure (grok), mirroring the codex framing.** This confines
    *writes* to the worktree, but ``/usr`` and a **minimal ``/etc`` subset** stay readable
    inside the jail — only the paths grok needs for TLS and name resolution (``/etc/ssl``,
    ``/etc/resolv.conf``, ``/etc/hosts``, ``/etc/nsswitch.conf``, and the CA-store dirs
    ``/etc/ca-certificates`` / ``/etc/pki`` where present), each bound only if it exists.
    The rest of ``/etc`` and the real home are NOT mounted, so grok cannot read host
    secrets outside the worktree; but this is a *reduced*, not eliminated, read surface, so
    do not read it as "grok can read nothing outside the worktree." A repo that must keep
    files off a vendor should use the egress opt-out or the gated ``patch_proposal`` path.

    The process is put in its own session (``--new-session``) so a TTY the jail might see
    cannot be used to inject keystrokes (TIOCSTI) back into Cohort.

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
        "--new-session",                  # own session: no TIOCSTI keystroke injection
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", "/usr", "/usr",
    ]
    # Grok needs TLS trust + name resolution to reach the xAI API, but does NOT need the
    # rest of /etc — bind only the resolver/cert paths it uses, each only if it exists so a
    # missing path can't break the argv. This narrows the readable jail surface from the
    # whole of /etc to this minimal subset (#227).
    for p in (
        "/etc/ssl",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/ca-certificates",
        "/etc/pki",
    ):
        if Path(p).exists():
            argv += ["--ro-bind", p, p]
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
    # This doer is non-interactive: close stdin (DEVNULL) so a TTY cannot inject keystrokes
    # into grok (TIOCSTI — belt-and-braces with the jail's --new-session).
    #
    # Unlike codex (see M5 note in run_codex_in_worktree), grok's tree is largely reaped
    # anyway by bwrap's --die-with-parent when the jail leader dies — but that guarantee
    # rests on bwrap, not on Cohort.
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _egress_gate_text(repo_root: Path, project_context_text: str) -> str:
    """Return the text the egress gate must judge, deriving it from repo state when the
    caller omitted it.

    #229: a caller that omits ``project_context_text`` must not thereby ship an opted-out
    repo — a bare ``run_*_doer(task, repo_root=...)`` would otherwise pass ``""`` and the
    egress gate would read that as "not opted out". So when the kwarg is empty we read the
    repo's own ``repo_root/.cohort/project_context.md`` and gate on that, exactly as the
    CLI entrypoints already do. A non-empty kwarg is an explicit override and is kept
    verbatim (it never *loosens* the gate — the derivation only ever adds an opt-out
    signal). The file read is not error-guarded on purpose: if it exists but cannot be
    read, the OSError propagates and the dispatch aborts (fail closed), never fails open.
    """
    if project_context_text:
        return project_context_text
    context_path = repo_root / ".cohort" / "project_context.md"
    if context_path.is_file():
        return context_path.read_text(encoding="utf-8")
    return ""


def _grok_gated_worktree(
    task: str,
    *,
    repo_root: Path,
    max_wire_bytes: int,
    project_context_text: str,
) -> Path:
    """Run every pre-dispatch gate, in order, and return a grok-ready worktree.

    This is the single gate sequence shared by the grok *write* doer
    (:func:`run_grok_doer`) and the grok *read* reviewer (:func:`run_grok_review`), so
    both paths are guaranteed — structurally, not by a copied-and-drifting duplicate — to
    apply the identical gates in the identical order, all BEFORE grok is ever invoked:

    1. refuse an empty task;
    2. :func:`_assert_grok_sandbox_available` (fail fast — never build a worktree we can't
       use);
    3. :func:`gates.require_egress_allowed` (honor the repo's egress opt-out);
    4. :func:`gates.assert_no_secrets` on the task text;
    5. create the detached, committed-files-only worktree;
    6. :func:`_assert_worktree_within_wire_budget` — bound the TOTAL egress (task +
       tracked worktree bytes the CLI reads and sends);
    7. :func:`_assert_worktree_files_have_no_secrets` — secret-scan those same files.

    If any worktree-level gate (6/7) raises, the just-created worktree is cleaned up
    before the error propagates, so a refusal never leaks a throwaway worktree. On success
    the caller owns the returned worktree's lifecycle.

    Raises:
        DoerError: empty task.
        DoerUnavailableError: the ``grok`` CLI or ``bwrap`` is not installed.
        EgressBlockedError / SecretFoundError: the repo opted out of egress, or the task
            (or a committed worktree file) carries credential-shaped content.
        PayloadTooLargeError: the total exposed payload exceeds ``max_wire_bytes``, or a
            tracked file could not be measured fail-closed.
    """
    if not task.strip():
        raise DoerError("task is empty")

    _assert_grok_sandbox_available()  # fail fast — never create a worktree we can't use
    # Derive the egress context from repo state when the caller omitted it, so an opted-out
    # repo can't be shipped by dropping the kwarg (#229). Gate ORDER is unchanged.
    egress_text = _egress_gate_text(repo_root, project_context_text)
    gates.require_egress_allowed(egress_text)
    gates.assert_no_secrets(task)

    worktree = patch_proposal._create_worktree(repo_root)
    try:
        # The vendor CLI reads the worktree's committed files and sends them to xAI, so
        # gate those files (not just the task string) before dispatch: bound the TOTAL
        # exposed bytes (task + tracked files) first, then secret-scan those same files.
        _assert_worktree_within_wire_budget(worktree, task, max_wire_bytes=max_wire_bytes)
        _assert_worktree_files_have_no_secrets(worktree, repo_root)
    except BaseException:
        patch_proposal.cleanup_worktree(repo_root, worktree)
        raise
    return worktree


def run_grok_doer(
    task: str,
    *,
    repo_root: Path,
    model: str | None = None,
    footprint: list[str] | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
    max_wire_bytes: int = _DEFAULT_MAX_WIRE_BYTES,
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
        PayloadTooLargeError: the total exposed payload (task + tracked worktree files)
            exceeds ``max_wire_bytes``, or a tracked file could not be measured
            fail-closed (both gated before grok runs).
    """
    worktree = _grok_gated_worktree(
        task,
        repo_root=repo_root,
        max_wire_bytes=max_wire_bytes,
        project_context_text=project_context_text,
    )
    try:
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


def run_grok_review(
    task: str,
    *,
    repo_root: Path,
    model: str | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
    max_wire_bytes: int = _DEFAULT_MAX_WIRE_BYTES,
    project_context_text: str = "",
) -> GrokReviewResult:
    """Dispatch grok-cli as a repo-read-only reviewer inside a throwaway bubblewrap worktree.

    Repo-read-only, not grok-read-only: grok runs **write-capable inside the jail** and its
    edits land in the throwaway worktree, which is discarded — so the *repo* is never
    modified, but grok is not run in a read-only mode. The read counterpart to
    :func:`run_grok_doer`. It applies the **identical gate
    sequence** — via the shared :func:`_grok_gated_worktree`, so the order cannot drift:
    assert sandbox, honor the egress opt-out, secret-scan the task, create the
    committed-files-only worktree, bound the total wire bytes, secret-scan the worktree
    files — every gate BEFORE grok is invoked. grok then runs confined to the worktree by
    bubblewrap (network open, scrubbed minimal env, API key on the env never argv), and
    its stdout is returned as the analysis.

    Unlike the doer, this **always discards the worktree** (``finally``): the read path
    wants grok's answer, not a diff. Any files grok wrote were confined to the worktree by
    the kernel and are thrown away with it — so grok's writes never touch this repo and
    are never surfaced. A grok failure still cleans up the worktree.

    Raises:
        DoerError: empty task.
        DoerUnavailableError: the ``grok`` CLI or ``bwrap`` is not installed.
        EgressBlockedError / SecretFoundError: the repo opted out of egress, or the task
            (or a committed worktree file grok would send to xAI) carries
            credential-shaped content (gated before grok runs).
        PayloadTooLargeError: the total exposed payload (task + tracked worktree files)
            exceeds ``max_wire_bytes``, or a tracked file could not be measured
            fail-closed (both gated before grok runs).
    """
    worktree = _grok_gated_worktree(
        task,
        repo_root=repo_root,
        max_wire_bytes=max_wire_bytes,
        project_context_text=project_context_text,
    )
    try:
        proc = run_grok_in_worktree(worktree, task, model=model, timeout=timeout)
        stdout = proc.stdout or ""
        # A review whose CLI failed must not read as "reviewed, nothing to say". grok-cli
        # exits 0 on some API-level errors, so a blank answer is treated as failure too:
        # an empty review is never a legitimate result.
        if proc.returncode != 0 or not stdout.strip():
            raise DoerFailedError(
                f"the grok CLI produced no usable review (exit {proc.returncode}); "
                f"stderr: {(proc.stderr or '').strip()[:500] or '<empty>'}"
            )
        return GrokReviewResult(
            engine="grok",
            analysis=stdout,
            transcript=stdout,
            returncode=proc.returncode,
        )
    finally:
        # The read path keeps grok's answer, not its edits — always discard the worktree,
        # on success and on failure alike (grok's confined writes go with it).
        patch_proposal.cleanup_worktree(repo_root, worktree)


def run_codex_doer(
    task: str,
    *,
    repo_root: Path,
    model: str | None = None,
    footprint: list[str] | None = None,
    timeout: float = _DOER_TIMEOUT_SECONDS,
    max_wire_bytes: int = _DEFAULT_MAX_WIRE_BYTES,
    project_context_text: str = "",
) -> DoerResult:
    """Dispatch Codex (ChatGPT) as a write doer confined to a fresh worktree.

    Raises:
        DoerError: empty task.
        DoerUnavailableError: the ``codex`` CLI is not installed.
        EgressBlockedError / SecretFoundError: the repo opted out of egress, or the task
            (or a committed worktree file the CLI would send to the vendor) contains
            credential-shaped content (gated before the CLI is invoked).
        PayloadTooLargeError: the total exposed payload (task + tracked worktree files)
            exceeds ``max_wire_bytes``, or a tracked file could not be measured
            fail-closed (both gated before the CLI is invoked).
    """
    if not task.strip():
        raise DoerError("task is empty")

    # Gate the outbound task BEFORE spawning the CLI: honor the repo egress opt-out and
    # refuse a task that carries a secret. (The CLI then reads only committed worktree
    # files, and its writes are OS-confined to the worktree.) Derive the egress context
    # from repo state when the caller omitted it so an opted-out repo can't be shipped by
    # dropping the kwarg (#229); gate ORDER is unchanged.
    egress_text = _egress_gate_text(repo_root, project_context_text)
    gates.require_egress_allowed(egress_text)
    gates.assert_no_secrets(task)

    worktree = patch_proposal._create_worktree(repo_root)
    try:
        # The CLI reads the worktree's committed files and sends them to the vendor, so
        # gate those files (not just the task string) before dispatch: bound the TOTAL
        # exposed bytes (task + tracked files) first, then secret-scan those same files.
        _assert_worktree_within_wire_budget(worktree, task, max_wire_bytes=max_wire_bytes)
        _assert_worktree_files_have_no_secrets(worktree, repo_root)
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

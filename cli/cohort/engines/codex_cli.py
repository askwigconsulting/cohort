"""The ``codex_cli`` transport — a one-shot consult through the OpenAI Codex CLI.

This is the code behind ``/consult-gpt`` (#266). Until it existed the command was prose:
the model ran ``codex exec`` itself from the live tree, and the README's promise that
the ``cohort:egress=deny`` marker "is what the code checks" was untrue for that row. Now
``cohort engine consult gpt`` runs :func:`cohort.engines.gates.preflight` on the
assembled prompt and only then calls :func:`consult` here, which spawns::

    codex exec --sandbox read-only --skip-git-repo-check --ephemeral --color never \\
        -C <empty scratch dir> [-m <model>] -

with the prompt written to the CLI's **stdin** and closed, inside a bubblewrap jail
wherever ``bwrap`` is installed. What that shape buys:

* ``--sandbox read-only`` — codex's own OS sandbox refuses writes and network for every
  command the model runs; never ``workspace-write``, never a ``danger`` flag. It does
  **not** confine reads: unjailed, codex can read any file the user can and put it in
  the transcript it sends — bytes that get neither the secret scan nor the size cap.
* **the jail** (:func:`jail_argv`) — the same kernel jail the grok doer and the ratchet
  evaluator use: ``/usr`` and a minimal ``/etc`` subset read-only, the network shared
  (codex must reach OpenAI), an **ephemeral tmpfs HOME** that holds only codex's own
  state directory (``~/.codex``, or ``$CODEX_HOME``) so a saved ``codex login`` still
  works, and the empty scratch directory as the working root. Those two are the only
  writable paths — the state dir because codex refuses to start without writing to it
  (the same writes it makes unjailed). Nothing else from the real home and nothing from
  the repository is mounted, so inside the jail codex can read nothing of yours: what
  leaves is the prompt. Without ``bwrap`` the consult still runs, announced as unjailed.
* the prompt on stdin, signalled by ``-`` — never argv: a prompt in argv is visible in
  the process list and capped by ``MAX_ARG_STRLEN`` (128 KiB on Linux, below the 200 KB
  gate), and ``codex exec`` reads instructions from stdin only when told to. Because
  Cohort *provides* stdin and closes it, the CLI can never sit on an inherited terminal
  that never closes — the classic "is it slow or hung" failure the old prose warned about.
* ``--ephemeral`` — no session rollout is persisted (the prompt already passed the
  secret scan, but there is no reason to leave a copy on disk).
* the scrubbed environment from :func:`cohort.engines.cli_doer._scrubbed_env`: only
  ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``CODEX_HOME`` pass through — never a host
  secret.

The CLI writes only the agent's final message to stdout (its banner, the echoed prompt
and the token count go to stderr), so stdout *is* the reply — and because stderr echoes
the prompt, a failure never quotes it back (see :func:`_diagnostic_lines`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cohort.engines import cli_doer

# The registry's transport key for this module; `engine consult` dispatches on it.
TRANSPORT = "codex_cli"

# A flagship consult on a hard problem legitimately takes minutes, and a consult that is
# merely slow looks identical to one that has hung. Give it a real budget rather than a
# shell-style default; `--timeout` widens it, and the process group is killed at the cap.
CONSULT_TIMEOUT_SECONDS: float = 600.0

# How many stderr lines to quote when codex fails, and how long each may be — enough
# for the reason ("not logged in", a usage limit), never a transcript.
_DIAGNOSTIC_LINES = 5
_DIAGNOSTIC_LINE_CHARS = 200

_RECOVERY_STEPS = (
    "install it with `npm install -g --prefix ~/.local @openai/codex`, then either "
    "`codex login` (interactive ChatGPT sign-in) or, for unattended use, "
    "`printenv OPENAI_API_KEY | codex login --with-api-key`"
)


class ConsultError(Exception):
    """The consult could not run or did not complete."""


class CodexUnavailableError(ConsultError):
    """The ``codex`` CLI is not installed — setup is missing, nothing was attempted."""


class CodexFailedError(ConsultError):
    """The CLI ran but exited non-zero or overran its time budget."""


def resolve_model(tier: str | None, model: str | None) -> str | None:
    """Pick the model to pin with ``-m``, or ``None`` for the CLI's default flagship.

    The consult never downgrades: there are no tiers to request a cheaper GPT through.
    ``--tier flagship`` (the default) means the CLI's default, which advances as the CLI
    does; any other tier is refused with the lever that does work, ``--model``.

    Raises:
        ConsultError: both ``tier`` and ``model`` were given, or ``tier`` names anything
            but the flagship.
    """
    if tier is not None and model is not None:
        raise ConsultError("--tier and --model are mutually exclusive; pass one or the other")
    if model is not None:
        return model
    if tier is None or tier == "flagship":
        return None
    raise ConsultError(
        f"engine 'codex' has no model tiers — a consult uses the Codex CLI's default "
        f"flagship model and never downgrades; pass --model <id> to pin one "
        f"(got --tier {tier!r})"
    )


def available() -> bool:
    """Whether the ``codex`` CLI is on ``PATH`` — the only probe made before a launch."""
    return shutil.which("codex") is not None


def jailed() -> bool:
    """Whether the consult will run inside the bubblewrap jail (``bwrap`` is installed).

    Presence, not usability: a host that has ``bwrap`` but forbids unprivileged user
    namespaces fails at launch, and that failure is reported as a failure — never a
    silent fall-back to an unjailed run.
    """
    return cli_doer._bwrap() is not None


def codex_home() -> Path:
    """Codex's auth/config directory on the host: ``$CODEX_HOME`` if set, else ``~/.codex``."""
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".codex"


def consult_argv(scratch_dir: Path, model: str | None) -> list[str]:
    """The exact ``codex exec`` invocation, with the prompt to follow on stdin (``-``)."""
    argv = [
        "codex", "exec",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "-C", str(scratch_dir),
    ]
    if model:
        argv += ["-m", model]
    argv.append("-")
    return argv


def jail_argv(scratch_dir: Path, sandbox_home: Path, inner: list[str]) -> list[str]:
    """Wrap ``inner`` in the bubblewrap jail that confines a consult to the prompt.

    Built from :func:`cli_doer._grok_sandbox_argv` — the audited minimal jail (``/usr``,
    the resolver/TLS subset of ``/etc``, a tmpfs ``HOME``, own session, network shared)
    — with every bind from the real home removed (grok's config and launcher) and
    codex's replacements added: its launcher/package tree from
    :func:`cli_doer._vendor_binary_binds`, and :func:`codex_home` bound at
    ``<sandbox_home>/.codex`` with ``CODEX_HOME`` pointed there, so the saved login is
    readable and nothing else of the user's home exists inside the jail. That state dir
    is bound **writable**: codex initialises its in-process app-server there and exits 1
    on a read-only mount, and these are codex's own writes, identical to an unjailed run.
    ``scratch_dir`` (the working directory) is the only other writable path.
    """
    home = Path.home()
    base = cli_doer._grok_sandbox_argv(scratch_dir, sandbox_home, inner)
    separator = base.index("--")
    options, tail = base[:separator], base[separator:]
    argv: list[str] = []
    i = 0
    while i < len(options):
        flag = options[i]
        if flag == "--ro-bind" and Path(options[i + 1]).is_relative_to(home):
            i += 3
            continue
        argv.append(flag)
        i += 1
    argv += cli_doer._vendor_binary_binds("codex")
    jailed_codex_home = sandbox_home / ".codex"
    host_codex_home = codex_home()
    if host_codex_home.is_dir():
        argv += ["--bind", str(host_codex_home), str(jailed_codex_home)]
    argv += ["--setenv", "CODEX_HOME", str(jailed_codex_home)]
    return argv + tail


def _diagnostic_lines(stderr: str, prompt: str) -> list[str]:
    """The last few stderr lines that are not lines of the prompt.

    codex echoes the prompt to stderr under a ``user`` heading, so quoting a raw tail on
    failure would print prompt content — including anything the secret scan did not
    recognise — into terminal and CI logs. Keep only lines that do not occur in the
    prompt, bounded in number and length.
    """
    prompt_lines = {line.strip() for line in prompt.splitlines() if line.strip()}
    kept = [
        line.strip()[:_DIAGNOSTIC_LINE_CHARS]
        for line in stderr.splitlines()
        if line.strip() and line.strip() not in prompt_lines
    ]
    return kept[-_DIAGNOSTIC_LINES:]


def consult(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = CONSULT_TIMEOUT_SECONDS,
) -> str:
    """Ask ChatGPT through the Codex CLI's read-only sandbox and return its reply text.

    The caller has already run the egress gates on ``prompt``; this function only
    spawns the CLI as described in the module docstring — inside the bubblewrap jail
    when :func:`jailed` — and returns its stdout. The scratch working root and the
    ephemeral HOME are created for the call and removed afterwards.

    Raises:
        ConsultError: ``prompt`` is empty.
        CodexUnavailableError: the ``codex`` CLI is not on ``PATH`` (nothing spawned).
        CodexFailedError: the CLI exited non-zero (a bounded, prompt-free diagnostic is
            quoted) or did not finish within ``timeout`` seconds (its process group was
            killed).
    """
    if not prompt.strip():
        raise ConsultError("prompt is empty")
    if not available():
        raise CodexUnavailableError(
            f"the 'codex' CLI is not installed; {_RECOVERY_STEPS}"
        )
    env = cli_doer._scrubbed_env(home=Path.home(), passthrough=cli_doer._CODEX_ENV_PASSTHROUGH)
    with tempfile.TemporaryDirectory(prefix="cohort-consult-") as root:
        scratch_dir = Path(root) / "work"
        sandbox_home = Path(root) / "home"
        scratch_dir.mkdir()
        sandbox_home.mkdir()
        argv = consult_argv(scratch_dir, model)
        in_jail = jailed()
        if in_jail:
            argv = jail_argv(scratch_dir, sandbox_home, argv)
        try:
            proc = cli_doer._launch_vendor_cli(argv, timeout=timeout, env=env, stdin_text=prompt)
        except subprocess.TimeoutExpired:
            raise CodexFailedError(
                f"codex did not finish within {timeout:.0f}s and was stopped; a flagship "
                f"consult can take minutes — re-run with --timeout <seconds> to widen the "
                f"budget, or check the model's availability"
            ) from None
    if proc.returncode != 0:
        where = " under the bubblewrap jail" if in_jail else ""
        diagnostic = "; ".join(_diagnostic_lines(proc.stderr or "", prompt))
        raise CodexFailedError(
            f"codex exited {proc.returncode}{where}"
            + (f" — {diagnostic}" if diagnostic else "")
            + f". Check `codex login status`; if setup is the problem, {_RECOVERY_STEPS}"
        )
    return proc.stdout

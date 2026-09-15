"""The ``codex_cli`` transport — a one-shot consult through the OpenAI Codex CLI.

This is the code behind ``/consult-gpt`` (#266). Until it existed the command was prose:
the model ran ``codex exec`` itself from the live tree, and the README's promise that
the ``cohort:egress=deny`` marker "is what the code checks" was untrue for that row. Now
``cohort engine consult gpt`` runs :func:`cohort.engines.gates.preflight` on the
assembled prompt and only then calls :func:`consult` here, which spawns::

    codex exec --sandbox read-only --skip-git-repo-check --ephemeral --color never \\
        -C <empty scratch dir> [-m <model>] -

with the prompt written to the CLI's **stdin** and closed. What that shape buys:

* ``--sandbox read-only`` — codex's own OS sandbox (Landlock/seccomp, Seatbelt) refuses
  writes and network for every command the model runs; never ``workspace-write``, never
  a ``danger`` flag.
* the prompt on stdin, signalled by ``-`` — never argv: a prompt in argv is visible in
  the process list and capped by ``MAX_ARG_STRLEN`` (128 KiB on Linux, below the 200 KB
  gate), and ``codex exec`` reads instructions from stdin only when told to. Because
  Cohort *provides* stdin and closes it, the CLI can never sit on an inherited terminal
  that never closes — the classic "is it slow or hung" failure the old prose warned about.
* ``-C <empty scratch dir>`` — codex's working root is a fresh temporary directory, so
  nothing of the repository is presented to it; the consult's context is the prompt
  Claude packaged, exactly as the docs say. **Known residual:** the read-only sandbox
  confines writes, not reads (see :mod:`cohort.engines.cli_doer`), so a repo that must
  not egress relies on the marker, which refuses before codex starts — not on this.
* ``--ephemeral`` — no session rollout is persisted under ``~/.codex`` (the prompt
  already passed the secret scan, but there is no reason to leave a copy on disk).
* the scrubbed environment from :func:`cohort.engines.cli_doer._scrubbed_env` with
  ``HOME`` kept real, so a saved ``codex login`` resolves and only ``OPENAI_API_KEY`` /
  ``OPENAI_BASE_URL`` / ``CODEX_HOME`` pass through — never a host secret.

The CLI writes only the agent's final message to stdout (its banner, the echoed prompt
and the token count go to stderr), so stdout *is* the reply.
"""

from __future__ import annotations

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

# How much of codex's stderr to quote when it fails — enough for the reason ("not logged
# in", a usage limit), never the whole stream.
_STDERR_TAIL_CHARS = 2000

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


def consult(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = CONSULT_TIMEOUT_SECONDS,
) -> str:
    """Ask ChatGPT through the Codex CLI's read-only sandbox and return its reply text.

    The caller has already run the egress gates on ``prompt``; this function only
    spawns the CLI as described in the module docstring and returns its stdout. The
    scratch working root is created for the call and removed afterwards.

    Raises:
        ConsultError: ``prompt`` is empty.
        CodexUnavailableError: the ``codex`` CLI is not on ``PATH`` (nothing spawned).
        CodexFailedError: the CLI exited non-zero (its stderr tail is quoted) or did not
            finish within ``timeout`` seconds (its process group was killed).
    """
    if not prompt.strip():
        raise ConsultError("prompt is empty")
    if not available():
        raise CodexUnavailableError(
            f"the 'codex' CLI is not installed; {_RECOVERY_STEPS}"
        )
    env = cli_doer._scrubbed_env(home=Path.home(), passthrough=cli_doer._CODEX_ENV_PASSTHROUGH)
    with tempfile.TemporaryDirectory(prefix="cohort-consult-") as scratch:
        argv = consult_argv(Path(scratch), model)
        try:
            proc = cli_doer._launch_vendor_cli(argv, timeout=timeout, env=env, stdin_text=prompt)
        except subprocess.TimeoutExpired:
            raise CodexFailedError(
                f"codex did not finish within {timeout:.0f}s and was stopped; a flagship "
                f"consult can take minutes — re-run with --timeout <seconds> to widen the "
                f"budget, or check the model's availability"
            ) from None
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-_STDERR_TAIL_CHARS:]
        raise CodexFailedError(
            f"codex exited {proc.returncode}"
            + (f": {tail}" if tail else "")
            + f" — if this is an auth problem, {_RECOVERY_STEPS}"
        )
    return proc.stdout

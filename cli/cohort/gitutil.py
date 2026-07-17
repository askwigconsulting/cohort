"""Shared git invocation hardening.

A single home for the non-interactive git environment so every module that shells
out to ``git``/``gh`` inherits the same hardening (never prompt for credentials or
host keys; fail fast when offline; refuse dangerous remote transports) and the same
default timeout — they can't drift apart over time.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def _git_config_env(pairs: dict[str, str]) -> dict[str, str]:
    """Encode git config as ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` /
    ``GIT_CONFIG_VALUE_n`` env vars — the environment-variable equivalent of ``-c
    key=value``, so every git invocation that inherits this env gets the config
    without each caller repeating ``-c`` flags (which is how they drift)."""
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (key, value) in enumerate(pairs.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# Remote-transport allowlist (default-deny). The ``ext::``/``fd::`` transports run
# an arbitrary command AS the "transport", so a crafted/attacker-supplied remote URL
# is a code path on the first fetch. A blocklist of just those two is fragile — any
# other exotic scheme slips through, and callers drift by forgetting the ``-c``
# flags. Instead deny every transport by default and allow only the safe ones we
# actually use (local paths, ssh, http/https). This bans ext/fd/git/etc. for EVERY
# git call that inherits GIT_ENV — one place, no drift.
_GIT_PROTOCOL_CONFIG = {
    "protocol.allow": "never",         # default-deny for any protocol not listed below
    "protocol.file.allow": "always",   # local paths: clones, file:// remotes, tests
    "protocol.ssh.allow": "always",    # git@host:… / ssh://
    "protocol.https.allow": "always",
    "protocol.http.allow": "always",
    # Empty credential.helper here too (not only per-caller -c) so no stored helper
    # can prompt or leak; with GIT_ASKPASS this keeps git fully silent.
    "credential.helper": "",
}

# Force git fully non-interactive: never prompt for credentials/host keys; fail
# fast when offline. (``--quiet`` only silences progress — it does NOT stop prompts.)
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=5",
    **_git_config_env(_GIT_PROTOCOL_CONFIG),
}

GIT_TIMEOUT = 10  # seconds; a hung git/gh must never stall the caller indefinitely


def git_state(repo: Path, path: Path) -> dict[str, Any]:
    """Best-effort git facts about one file inside ``repo``. Never raises.

    Used to *surface*, never to gate. A project-scoped artifact travels with the
    repo, so "is it tracked?" is the signal that matters: tracked means the change
    is reviewable — it has history and a PR can gate it; untracked (or no git at
    all) means there is no audit trail. Which of those is acceptable is the user's
    call, so Cohort reports the state and blocks neither (#182).

    Returns ``{git, tracked, dirty}``: whether ``repo`` is a work tree, whether
    ``path`` is tracked, and whether a tracked path has uncommitted changes.
    """
    unknown = {"git": False, "tracked": False, "dirty": False}

    def _git(*args: str):
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True,
                env={**os.environ, **GIT_ENV}, timeout=GIT_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0:
        return unknown
    tracked_run = _git("ls-files", "--error-unmatch", "--", str(path))
    tracked = tracked_run is not None and tracked_run.returncode == 0
    dirty = False
    if tracked:
        status = _git("status", "--porcelain", "--", str(path))
        dirty = bool(status is not None and status.stdout.strip())
    return {"git": True, "tracked": tracked, "dirty": dirty}

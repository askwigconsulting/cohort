"""Shared git invocation hardening.

A single home for the non-interactive git environment so every module that shells
out to ``git``/``gh`` inherits the same hardening (never prompt for credentials or
host keys; fail fast when offline) and the same default timeout — they can't drift
apart over time.
"""

from __future__ import annotations

# Force git fully non-interactive: never prompt for credentials/host keys; fail
# fast when offline. (``--quiet`` only silences progress — it does NOT stop prompts.)
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=5",
}

GIT_TIMEOUT = 10  # seconds; a hung git/gh must never stall the caller indefinitely

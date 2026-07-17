"""Fail-closed safety gates for external (non-Claude) engine egress — RFC 0004.

When Cohort hands context to an external engine such as xAI's Grok (the
``patch_proposal`` role), the RFC 0004 security & privacy review requires that the
safety promises live in **code**, not prose. This module is that code: a set of
small, composable gates that each **fail closed** — on any doubt they block.

The gates cover four surfaces:

* **Egress opt-out** — a repo may forbid external-engine egress outright.
* **Secret scan** — a regex backstop that flags credential-shaped content before it
  leaves the machine. Regex scanning has false negatives and is a *backstop*, not a
  guarantee; the primary control is Claude-curated, byte-bounded payloads.
* **Path/scope gate** — a produced patch may only touch its declared footprint, and
  never a sensitive class (git internals, hooks, CI, lockfiles, build/install
  scripts, auth/crypto/secret files) without a deliberate, reviewed override.
* **Payload bound** — a hard UTF-8 byte cap mirroring :mod:`cohort.engines.xai`; the
  primary cost/egress control.

Every raised error carries only non-secret context — labels, byte counts, path
names, env-var names. A matched secret **value** never appears in any label, message,
or ``repr``.
"""

from __future__ import annotations

import posixpath
import re

# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class GateError(Exception):
    """Base class for every safety-gate failure in this module."""


class EgressBlockedError(GateError):
    """The repo has opted out of external-engine egress; sending is forbidden."""


class SecretFoundError(GateError):
    """Credential-shaped content was found in a payload bound for an engine.

    The message lists only non-secret finding *labels* — never the matched value.
    """


class PathViolationError(GateError):
    """A produced patch touches a path outside its footprint or in a sensitive class."""


class PayloadTooLargeError(GateError):
    """The UTF-8 payload exceeds the configured byte cap (raised before any egress)."""


# --------------------------------------------------------------------------- #
# 1. Egress opt-out
# --------------------------------------------------------------------------- #

# Literal marker token a repo can drop anywhere in its project context to deny
# external-engine egress. Matched case-insensitively, with optional whitespace
# around the separators so a hand-typed variant still trips it (fail closed).
_EGRESS_MARKER_RE = re.compile(r"cohort\s*:\s*egress\s*=\s*deny", re.IGNORECASE)

# Heading that opens an "## Egress" policy section, and the deny-signalling words
# whose presence in that section's body means opt-out.
_EGRESS_HEADING_RE = re.compile(r"^\s{0,3}##\s+egress\b.*$", re.IGNORECASE)
_HEADING_L1_L2_RE = re.compile(r"^\s{0,3}#{1,2}\s+\S")
_EGRESS_DENY_WORDS_RE = re.compile(
    r"deny|restricted|no-egress|opt-out", re.IGNORECASE
)


def egress_opted_out(project_context_text: str) -> bool:
    """Return True if the repo has opted out of external-engine egress.

    Opt-out is signalled in ``.cohort/project_context.md`` by EITHER:

    * the literal marker token ``cohort:egress=deny`` (case-insensitive) appearing
      anywhere in the file, OR
    * a Markdown heading ``## Egress`` whose section body (the lines up to the next
      level-1 or level-2 heading) contains any of ``deny``, ``restricted``,
      ``no-egress``, or ``opt-out`` (case-insensitive).

    An absent file, an absent ``## Egress`` section, or a section with none of the
    deny words means *not opted out* (returns False). The caller decides how to feed
    the file in; passing ``""`` (e.g. a missing file) safely yields False.

    Args:
        project_context_text: The full text of the repo's project-context file.

    Returns:
        True if egress is opted out, else False.
    """
    if _EGRESS_MARKER_RE.search(project_context_text):
        return True

    lines = project_context_text.splitlines()
    in_section = False
    for line in lines:
        if in_section:
            if _HEADING_L1_L2_RE.match(line):
                # A new level-1/level-2 heading closes the Egress section.
                in_section = False
                # Fall through so this same line can *open* a new Egress section.
            else:
                if _EGRESS_DENY_WORDS_RE.search(line):
                    return True
                continue
        if _EGRESS_HEADING_RE.match(line):
            in_section = True
    return False


def require_egress_allowed(project_context_text: str) -> None:
    """Raise :class:`EgressBlockedError` if the repo has opted out of egress.

    Args:
        project_context_text: The full text of the repo's project-context file.

    Raises:
        EgressBlockedError: if :func:`egress_opted_out` returns True.
    """
    if egress_opted_out(project_context_text):
        raise EgressBlockedError(
            "external-engine egress is opted out for this repo "
            "(cohort:egress=deny marker or an '## Egress' deny section)"
        )


# --------------------------------------------------------------------------- #
# 2. Secret scan
# --------------------------------------------------------------------------- #

# AWS access key id: the fixed "AKIA" prefix plus 16 uppercase base-32 chars.
_AWS_ACCESS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")

# PEM private-key header, e.g. "-----BEGIN RSA PRIVATE KEY-----" or the bare
# "-----BEGIN PRIVATE KEY-----"; the optional algorithm sits between BEGIN and
# PRIVATE KEY on the same line.
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# HTTP bearer token; require a non-trivial (>=10 char) token so the English word
# "Bearer" followed by a short word does not trip it.
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{10,}")

# Sensitive assignment: any identifier that contains one of these keywords, set to
# a non-trivial value (>=6 non-space chars). Covers both source assignments and
# ``.env``-style ``KEY=value`` lines. The value is captured only to be discarded;
# it never reaches a label.
_SECRET_KEYWORDS: tuple[str, ...] = (
    "ACCESS_KEY",
    "API_KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
)
_ASSIGNMENT_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_\-]*)\s*[:=]\s*['\"]?([^\s'\"]{6,})",
)


def _assignment_keyword(identifier: str) -> str | None:
    """Return the first sensitive keyword contained in ``identifier``, else None."""
    upper = identifier.upper()
    for keyword in _SECRET_KEYWORDS:
        if keyword in upper:
            return keyword
    return None


def scan_for_secrets(text: str) -> list[str]:
    """Scan ``text`` for credential-shaped content and return non-secret labels.

    Each label names a *kind* of finding (e.g. ``"aws-access-key-id"``,
    ``"private-key-block"``, ``"bearer-token"``, ``"generic-assignment:API_KEY"``).
    A matched secret **value** is never included in any label.

    Detected classes:

    * ``aws-access-key-id`` — ``AKIA`` + 16 uppercase base-32 chars.
    * ``private-key-block`` — a ``-----BEGIN ... PRIVATE KEY-----`` header.
    * ``bearer-token`` — ``Bearer <token>`` with a non-trivial token.
    * ``generic-assignment:<KEYWORD>`` — an identifier containing ``API_KEY``,
      ``SECRET``, ``TOKEN``, ``PASSWORD``, ``PASSWD`` or ``ACCESS_KEY`` assigned a
      non-trivial value (both ``KEY = value`` and ``.env``-style ``KEY=value``).

    Regex scanning has **false negatives** (a value split across lines, an unusual
    key name, a short secret) and is a backstop, not a guarantee — the primary
    control is Claude-curated, byte-bounded payloads. It never returns a false label
    that leaks a value.

    Args:
        text: The payload to scan.

    Returns:
        A sorted, de-duplicated list of finding labels; empty if nothing matched.
    """
    labels: set[str] = set()

    if _AWS_ACCESS_KEY_RE.search(text):
        labels.add("aws-access-key-id")
    if _PRIVATE_KEY_RE.search(text):
        labels.add("private-key-block")
    if _BEARER_RE.search(text):
        labels.add("bearer-token")

    for match in _ASSIGNMENT_RE.finditer(text):
        keyword = _assignment_keyword(match.group(1))
        if keyword is not None:
            labels.add(f"generic-assignment:{keyword}")

    return sorted(labels)


def assert_no_secrets(text: str) -> None:
    """Raise :class:`SecretFoundError` if ``text`` contains credential-shaped content.

    Args:
        text: The payload to scan.

    Raises:
        SecretFoundError: if :func:`scan_for_secrets` returns any labels. The message
            lists only the labels, never the matched values.
    """
    labels = scan_for_secrets(text)
    if labels:
        raise SecretFoundError(
            "payload contains credential-shaped content: " + ", ".join(labels)
        )


# --------------------------------------------------------------------------- #
# 3. Path / scope gate
# --------------------------------------------------------------------------- #


def _normalize_path(path: str) -> str:
    """Normalize a repo-relative path to posix form, collapsing ``.``/``..``.

    Backslashes are folded to ``/`` first so a Windows-style path cannot smuggle a
    component past the classifier. The result is either a clean relative path, or a
    sentinel the caller treats as a violation: an absolute path keeps its leading
    ``/``; a path escaping the repo root starts with ``..``; an empty/degenerate
    path collapses to ``.``.
    """
    folded = path.strip().replace("\\", "/")
    normalized = posixpath.normpath(folded)
    # posixpath.normpath strips a leading "./" but preserves a leading "/" (absolute)
    # and a leading "../" (escapes root); both are surfaced to the caller as-is.
    return normalized


def _escapes_repo(normalized: str) -> bool:
    """True if a normalized path is absolute, escapes the repo root, or is degenerate."""
    if normalized in ("", "."):
        return True
    if normalized.startswith("/"):
        return True
    return normalized == ".." or normalized.startswith("../")


def _glob_to_regex(glob: str) -> str:
    """Translate a footprint glob to a segment-aware regex source.

    Supported tokens: ``*`` (matches within a path segment), ``**`` (matches across
    segments, including ``/``), and ``?`` (a single non-``/`` char). Every other
    character is matched literally. Segment-aware ``*`` keeps the allow-list
    conservative — it never silently spans directory boundaries.
    """
    out: list[str] = []
    i = 0
    length = len(glob)
    while i < length:
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def _within_footprint(path: str, entry: str) -> bool:
    """True if ``path`` (normalized) is covered by a single footprint ``entry``.

    An entry is either a plain prefix (matches the path exactly or as a directory
    ancestor) or a glob (``*``/``**``/``?``). ``.`` / ``./`` mean the whole repo. An
    empty entry matches nothing (fail closed).
    """
    entry = entry.strip().replace("\\", "/")
    if entry in (".", "./"):
        return True
    if entry == "":
        return False
    if "*" in entry or "?" in entry:
        return re.fullmatch(_glob_to_regex(entry), path) is not None
    entry = entry.rstrip("/")
    if entry == "":
        return False
    return path == entry or path.startswith(entry + "/")


def _classify_sensitive(path: str) -> str | None:
    """Return a sensitive-class label for ``path``, or None if it is not sensitive.

    Sensitive classes require elevated approval regardless of footprint. The check is
    deliberately broad (segment-level prefix matching for auth/crypto/secret,
    ``*.lock`` for lockfiles) so it fails closed on near-misses.
    """
    segments = [seg for seg in path.split("/") if seg not in ("", ".")]
    if not segments:
        return None
    base = segments[-1].lower()
    lower = path.lower()

    if any(seg.lower().startswith(".env") for seg in segments):
        return "dotenv"
    if lower == ".git" or lower.startswith(".git/"):
        return "git-internal"
    if "hooks" in (seg.lower() for seg in segments):
        return "git-hook"
    if lower.startswith(".github/workflows/") or lower.startswith(".circleci/"):
        return "ci-config"
    if base in {
        ".gitlab-ci.yml",
        ".travis.yml",
        "azure-pipelines.yml",
        "jenkinsfile",
    }:
        return "ci-config"
    if base.endswith(".lock") or base in {
        "package-lock.json",
        "poetry.lock",
        "uv.lock",
        "cargo.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
        "gemfile.lock",
        "composer.lock",
    }:
        return "lockfile"
    if base in {"setup.py", "setup.cfg", "pyproject.toml"}:
        return "build-manifest"
    if base.startswith("install") or (base.endswith(".sh") and len(segments) == 1):
        return "install-script"
    if any(
        seg.lower().startswith(("auth", "crypto", "secret")) for seg in segments
    ):
        return "auth-crypto-secret"
    return None


def check_changed_paths(
    paths: list[str], *, allowed_footprint: list[str]
) -> list[str]:
    """Return violation labels for any of ``paths`` a produced patch may not touch.

    A path violates if it is absolute or escapes the repo root, if it is outside
    every entry in ``allowed_footprint`` (prefixes or ``*``/``**``/``?`` globs
    relative to the repo root), or if it falls in a **sensitive** class — anything
    under ``.git/``, a git hook, CI config, a dependency lockfile, a build/install
    manifest or script, or an auth/crypto/secret/``.env`` path — which requires
    elevated approval *regardless of footprint*.

    A sensitive path can be allowed only by an **explicit, reviewed override**: a
    footprint entry that both matches the path and is itself sensitive-classified
    (e.g. listing ``src/auth.py`` or ``.env`` by name). A broad footprint such as
    ``**`` never overrides sensitivity — that is the whole point of the gate.

    Args:
        paths: The repo-relative paths the patch would change.
        allowed_footprint: Path prefixes/globs the patch is permitted to touch. An
            entry that is itself sensitive-classified acts as a deliberate override.

    Returns:
        A list of ``"<path>: <reason>"`` violation labels, in input order; empty if
        every path is allowed.
    """
    violations: list[str] = []
    for original in paths:
        normalized = _normalize_path(original)
        if _escapes_repo(normalized):
            violations.append(f"{original}: escapes-repo-root")
            continue

        sensitive_class = _classify_sensitive(normalized)
        if sensitive_class is not None:
            overridden = any(
                _within_footprint(normalized, entry)
                and _classify_sensitive(_normalize_path(entry)) is not None
                for entry in allowed_footprint
            )
            if not overridden:
                violations.append(f"{normalized}: sensitive:{sensitive_class}")
            continue

        if not any(
            _within_footprint(normalized, entry) for entry in allowed_footprint
        ):
            violations.append(f"{normalized}: outside-footprint")
    return violations


def assert_paths_allowed(
    paths: list[str], *, allowed_footprint: list[str]
) -> None:
    """Raise :class:`PathViolationError` if any path is outside footprint or sensitive.

    Args:
        paths: The repo-relative paths the patch would change.
        allowed_footprint: Path prefixes/globs the patch is permitted to touch; a
            sensitive-classified entry acts as a deliberate, reviewed override.

    Raises:
        PathViolationError: if :func:`check_changed_paths` finds any violation. The
            message lists each violating path and why.
    """
    violations = check_changed_paths(paths, allowed_footprint=allowed_footprint)
    if violations:
        raise PathViolationError(
            "patch touches disallowed paths: " + "; ".join(violations)
        )


# --------------------------------------------------------------------------- #
# 4. Payload bound
# --------------------------------------------------------------------------- #


def assert_payload_within(text: str, *, max_bytes: int = 200_000) -> None:
    """Raise :class:`PayloadTooLargeError` if ``text`` exceeds ``max_bytes`` (UTF-8).

    Mirrors the byte cap in :mod:`cohort.engines.xai`; this is the primary
    cost/egress control and is enforced before any network I/O.

    Args:
        text: The payload to bound.
        max_bytes: The maximum permitted UTF-8 byte length.

    Raises:
        PayloadTooLargeError: if the payload's UTF-8 length exceeds ``max_bytes``.
    """
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise PayloadTooLargeError(
            f"payload is {size} bytes, exceeds the {max_bytes}-byte cap"
        )


# --------------------------------------------------------------------------- #
# 5. Convenience preflight
# --------------------------------------------------------------------------- #


def preflight(
    *,
    prompt: str,
    project_context_text: str,
    max_bytes: int = 200_000,
) -> None:
    """Run the pre-egress gates in fail-closed order; the first failure wins.

    Order:

    1. egress opt-out (raise :class:`EgressBlockedError`),
    2. payload bound on ``prompt`` (raise :class:`PayloadTooLargeError`),
    3. secret scan of ``prompt`` (raise :class:`SecretFoundError`).

    An opted-out repo therefore blocks before anything is scanned, and an oversized
    prompt is rejected before the (potentially expensive) secret scan runs.

    Args:
        prompt: The payload about to be sent to an external engine.
        project_context_text: The repo's project-context file text (egress policy).
        max_bytes: The maximum permitted UTF-8 byte length for ``prompt``.

    Raises:
        EgressBlockedError: if the repo opted out of egress.
        PayloadTooLargeError: if ``prompt`` exceeds ``max_bytes``.
        SecretFoundError: if ``prompt`` contains credential-shaped content.
    """
    require_egress_allowed(project_context_text)
    assert_payload_within(prompt, max_bytes=max_bytes)
    assert_no_secrets(prompt)

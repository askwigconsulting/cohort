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
  never a sensitive class (git internals, hooks, CI, lockfiles, build/install/
  executable scripts, auth/crypto/secret files) without a deliberate, reviewed
  override.
* **Payload bound** — a hard UTF-8 byte cap mirroring :mod:`cohort.engines.xai`; the
  primary cost/egress control.

Every raised error carries only non-secret context — labels, byte counts, path
names, env-var names. A matched secret **value** never appears in any label, message,
or ``repr``.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from typing import NamedTuple

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

# Structured marker tokens a repo can drop anywhere in its project context to deny
# or (explicitly) allow external-engine egress. Matched case-insensitively, with
# optional whitespace around the separators so a hand-typed variant still trips it.
# These are the *reliable* signals — deny wins over allow (fail closed).
#
# Both markers are **line-anchored**: the directive must be the entire line (modulo
# any leading indentation and trailing whitespace), not merely a substring anywhere in
# the file. A whole-file substring search would let ordinary prose weaponize the
# marker — e.g. "do NOT add cohort:egress=allow" contains the literal allow-marker
# text inside a *prohibition*, and a substring match would misread that sentence as
# permission and disable the opt-out. Requiring the marker to stand alone on its own
# line makes that negation-proof: prose that merely *mentions* a marker never matches.
# Leading whitespace is unbounded (not just 0-3 spaces) so a marker indented under a
# list item or inside a fenced code block still trips — deny must never fail open on
# indentation, and an over-eager allow match just falls back to the safer deny-wins
# default.
_EGRESS_DENY_MARKER_RE = re.compile(
    r"^[ \t]*cohort\s*:\s*egress\s*=\s*deny\s*$", re.IGNORECASE | re.MULTILINE
)
_EGRESS_ALLOW_MARKER_RE = re.compile(
    r"^[ \t]*cohort\s*:\s*egress\s*=\s*allow\s*$", re.IGNORECASE | re.MULTILINE
)

# Heading that opens an "## Egress" policy section. Merely *having* such a section is
# a deliberate policy statement, so it flips the repo to deny-by-default; only the
# structured allow marker re-permits. Prose is never trusted to signal intent — that
# is what makes this negation-proof ("engines are NOT allowed" cannot read as allow).
_EGRESS_HEADING_RE = re.compile(r"^\s{0,3}##\s+egress\b.*$", re.IGNORECASE)


def egress_opted_out(project_context_text: str) -> bool:
    """Return True if the repo has opted out of external-engine egress.

    The signal in ``.cohort/project_context.md`` is deliberately **structured**, not
    prose, so it cannot fail open on an ambiguous or negated sentence. The repo is
    opted out (returns True) when EITHER:

    * the literal marker ``cohort:egress=deny`` (case-insensitive) appears anywhere in
      the file, OR
    * a Markdown heading ``## Egress`` appears anywhere AND the file does *not* also
      carry the explicit ``cohort:egress=allow`` marker.

    In other words, writing an ``## Egress`` section at all switches the repo to
    deny-by-default; to permit egress despite that section, add the explicit
    ``cohort:egress=allow`` marker. Free-text words in the section (``allowed``,
    ``disabled``, ``forbidden`` …) are intentionally **not** trusted — a sentence like
    "external engines are NOT allowed" must never be misread as permission. The same
    goes for the structured markers themselves: each must stand alone on its own line,
    so a sentence that merely *mentions* the marker text (e.g. "do NOT add
    cohort:egress=allow") is never read as the directive.

    An absent file or a file with no ``## Egress`` section and no deny marker means
    *not opted out* (returns False); the default is allow, per Cohort's
    code-sharing-default-allow posture. ``deny`` always beats ``allow`` (fail closed).

    Args:
        project_context_text: The full text of the repo's project-context file.

    Returns:
        True if egress is opted out, else False.
    """
    if _EGRESS_DENY_MARKER_RE.search(project_context_text):
        return True

    has_egress_section = any(
        _EGRESS_HEADING_RE.match(line)
        for line in project_context_text.splitlines()
    )
    if has_egress_section and not _EGRESS_ALLOW_MARKER_RE.search(project_context_text):
        return True
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
            "(a 'cohort:egress=deny' marker, or an '## Egress' section without an "
            "explicit 'cohort:egress=allow' marker). Add 'cohort:egress=allow' to "
            "permit egress."
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

# High-signal, fixed-prefix vendor credential shapes. Each prefix is distinctive
# enough on its own (near-zero false-positive rate) that no surrounding context is
# required, unlike the generic-assignment heuristic below.
#
# GitHub personal-access tokens: the classic `gh[pousr]_` prefix (personal, oauth,
# user-to-server, server-to-server, refresh) followed by 36 alphanumerics, and the
# newer fine-grained `github_pat_` form.
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")
_GITHUB_FINE_GRAINED_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")

# Slack tokens: bot/app/user/legacy prefixes followed by a dash-delimited body.
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")

# OpenAI/Anthropic API keys: the shared `sk-` prefix, with Anthropic's `sk-ant-`
# variant as an optional sub-prefix.
_AI_API_KEY_RE = re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")

# Google API key: the fixed "AIza" prefix plus 35 URL-safe base64 chars.
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")

# JSON Web Token: base64url header and payload segments joined by dots, followed by
# the dot that opens the signature segment. The signature itself is not required so
# the pattern still catches a token that was truncated in a log line.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")

# Connection-string credential: a `user:password@` pair immediately after a URI
# scheme separator, e.g. `postgres://svc:S3cret@db/prod` or `mysql://root:hunter2@…`.
# The password segment requires >=4 chars to keep the false-positive rate low while
# still catching short-but-real passwords.
_CONNECTION_STRING_CREDENTIAL_RE = re.compile(r"://[^/\s:@]+:[^/\s:@]{4,}@")

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
# Horizontal whitespace only around the separator — `\s*` matches newlines, which made a
# harmless label swallow the next line. `Repro:` followed by a blank line and
# `AWS_SECRET_ACCESS_KEY = "..."` matched as identifier `Repro` with the credential's NAME
# as its value: no secret keyword in "Repro", no finding, and `finditer` had consumed the
# real assignment so it was never scanned on its own. A prose label above a credential is
# the most common shape in a bug report or a doc, so this hid exactly the case that matters.
_ASSIGNMENT_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_\-]*)[ \t]*([:=])[ \t]*['\"]?([^\s'\"]{6,})",
)

# Trailing syntax that rides along on the captured value because the value pattern
# stops only at whitespace or a quote — e.g. ``max_tokens=max_tokens)`` captures
# ``max_tokens)``. Stripped before the value is classified so the self-reference and
# annotation rules below see the bare token.
_VALUE_TRAILER_CHARS = ",;)]}'\"`"

# Type expressions that follow an annotation colon. An annotated *name* that happens to
# contain a secret keyword (``token: bytes``, ``_SECRET_KEYWORDS: tuple[str, ...]``, a
# docstring's ``max_tokens: Optional cap on ...``) is a declaration, not a credential —
# the identifier is being typed, not assigned a value. Only consulted for the ``:``
# separator, so ``GITHUB_TOKEN: ghp_...`` (a real credential in colon form) still trips.
_DOTTED_REFERENCE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?P<last>[A-Za-z_][A-Za-z0-9_]*))+"
)

#
# Anchored at BOTH ends: the value must be the type name itself, or the start of a
# subscripted one (``tuple[str, ...]`` captures as ``tuple[``). Matching a mere *prefix*
# was a false-negative hole — `password: Path-2026-Xy9z-secretvals` and
# `api_key: int-8f2k1-zzz` both begin with a type name followed by a word boundary, and
# YAML (the dominant secrets-config format) uses `:` for every assignment, so a prefix
# rule silently exempted real credentials in exactly the files most likely to hold them.
_TYPE_ANNOTATION_VALUE_RE = re.compile(
    r"(?:str|bytes|bytearray|int|float|bool|complex|list|dict|set|frozenset|tuple"
    r"|object|None|Any|Optional|Union|Literal|Mapping|MutableMapping|Sequence"
    r"|Iterable|Iterator|Callable|Awaitable|Coroutine|Path|datetime|Decimal|UUID)"
    r"(?:\[.*)?"
)

# RHS shapes that are unambiguously *code*, not a credential value, even when the
# identifier on the left names a secret keyword — e.g. ``_URL_PASSWORD =
# re.compile(r"...")`` or a comment-shaped string. Checked against the start of the
# captured value token (COORD-1: precision fix for the assignment heuristic).
_CODE_SHAPED_VALUE_RE = re.compile(
    r"^(?:re\.compile\(|r['\"]|import\b|from\b|lambda\b|None\b|True\b|False\b"
    r"|[A-Za-z_][A-Za-z0-9_.]*\()"
)


def _assignment_keyword(identifier: str) -> str | None:
    """Return the first sensitive keyword contained in ``identifier``, else None."""
    upper = identifier.upper()
    for keyword in _SECRET_KEYWORDS:
        if keyword in upper:
            return keyword
    return None


def _is_self_reference(identifier: str, value: str) -> bool:
    """True if the assignment merely passes a name to itself, e.g. ``max_tokens=max_tokens``.

    A keyword argument forwarded under its own name (``max_tokens=max_tokens``,
    ``token=token``) is a *variable reference*, never a literal credential — the value is
    resolved at runtime and no secret appears in the text. Restricted to an exact
    identifier match so an ordinary bare-word value (``DATABASE_PASSWORD=hunter2secret``)
    is untouched.
    """
    return value == identifier


def _is_dotted_reference(identifier: str, value: str) -> bool:
    """True if the value forwards ``identifier`` through an attribute, e.g. ``token=srv.token``.

    ``request(..., token=srv.token)`` reads an attribute at runtime; the text contains no
    credential. Requires a **full** match against a chain of Python identifiers joined by
    dots — so anything carrying credential punctuation (``sk-...``, base64url) is untouched.

    It additionally requires the **final segment to equal the assigned name**, which is what
    makes this a *forwarding* pattern rather than "any dotted word". Without that, a
    ``name.surname`` password (``PASSWORD=jonathan.smith``) is structurally identical to
    ``token=srv.token`` and was silently exempted — a real credential shape, and a
    false-negative this scanner previously caught.
    """
    match = _DOTTED_REFERENCE_RE.fullmatch(value)
    return match is not None and match.group("last") == identifier


def _looks_secret_shaped(value: str) -> bool:
    """True if a captured assignment value looks like a credential, not source code.

    The generic-assignment heuristic previously flagged any identifier containing a
    keyword like ``PASSWORD`` regardless of what it was set to — so security source
    that merely *names* a secret (``_URL_PASSWORD = re.compile(r"...")``, a validator
    naming ``API_KEY`` in a docstring) false-positived. This rejects values that are
    unambiguously code-shaped: a compiled regex, a raw-string literal, an
    import/keyword, or a plain function/constructor call.
    """
    return _CODE_SHAPED_VALUE_RE.match(value) is None


def scan_for_secrets(text: str) -> list[str]:
    """Scan ``text`` for credential-shaped content and return non-secret labels.

    Each label names a *kind* of finding (e.g. ``"aws-access-key-id"``,
    ``"private-key-block"``, ``"bearer-token"``, ``"generic-assignment:API_KEY"``).
    A matched secret **value** is never included in any label.

    Detected classes:

    * ``aws-access-key-id`` — ``AKIA`` + 16 uppercase base-32 chars.
    * ``private-key-block`` — a ``-----BEGIN ... PRIVATE KEY-----`` header.
    * ``bearer-token`` — ``Bearer <token>`` with a non-trivial token.
    * ``github-token`` — a GitHub PAT (``gh[pousr]_...`` or ``github_pat_...``).
    * ``slack-token`` — a Slack token (``xox[baprs]-...``).
    * ``ai-api-key`` — an OpenAI/Anthropic-shaped key (``sk-...`` / ``sk-ant-...``).
    * ``google-api-key`` — a Google API key (``AIza...``).
    * ``jwt`` — a JSON Web Token (``eyJ....eyJ....``).
    * ``connection-string-credential`` — a ``user:password@`` pair in a URI, e.g.
      ``postgres://svc:S3cret@db/prod``.
    * ``generic-assignment:<KEYWORD>`` — an identifier containing ``API_KEY``,
      ``SECRET``, ``TOKEN``, ``PASSWORD``, ``PASSWD`` or ``ACCESS_KEY`` assigned a
      non-trivial, credential-shaped value (both ``KEY = value`` and ``.env``-style
      ``KEY=value``); a code-shaped RHS (a compiled regex, a raw string, an
      import/keyword, a function call) is exempted to cut false positives on
      security source that merely names a secret keyword.

    Regex scanning has **false negatives** (a value split across lines, an unusual
    key name, a short secret) and is a backstop, not a guarantee — the primary
    control is Claude-curated, byte-bounded payloads. It never returns a false label
    that leaks a value.

    Args:
        text: The payload to scan.

    Returns:
        A sorted, de-duplicated list of finding labels; empty if nothing matched.
    """
    return sorted({finding.label for finding in scan_for_secret_findings(text)})


# Each high-signal pattern paired with the label it raises. Two patterns share the
# ``github-token`` label (classic and fine-grained PATs), which is why this is a tuple of
# pairs rather than a mapping.
_HIGH_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key-id", _AWS_ACCESS_KEY_RE),
    ("private-key-block", _PRIVATE_KEY_RE),
    ("bearer-token", _BEARER_RE),
    ("github-token", _GITHUB_TOKEN_RE),
    ("github-token", _GITHUB_FINE_GRAINED_PAT_RE),
    ("slack-token", _SLACK_TOKEN_RE),
    ("ai-api-key", _AI_API_KEY_RE),
    ("google-api-key", _GOOGLE_API_KEY_RE),
    ("jwt", _JWT_RE),
    ("connection-string-credential", _CONNECTION_STRING_CREDENTIAL_RE),
)


class SecretFinding(NamedTuple):
    """One credential-shaped hit: its kind, and a digest of the matched value.

    The digest — never the value — is what makes a finding *addressable*: a repo can
    declare a specific known-fake fixture suppressed (see
    :func:`scan_for_secret_findings` callers) without exempting a whole path, so a
    *different* credential appearing in that same file still trips the gate.

    Attributes:
        label: The finding kind, e.g. ``"aws-access-key-id"``. Never carries a value.
        digest: :func:`secret_digest` of the matched text.
    """

    label: str
    digest: str


def secret_digest(value: str) -> str:
    """Return a short, stable, non-reversible digest of a matched secret value.

    Truncated to 16 hex chars (64 bits) — ample against accidental collision between the
    handful of fixtures a repo declares, short enough to stay readable in a committed
    allowlist. A digest is not a value: publishing it discloses nothing about a
    high-entropy credential, and the values a repo declares are fakes by construction.
    """
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def scan_for_secret_findings(text: str) -> list[SecretFinding]:
    """Scan ``text`` and return every distinct :class:`SecretFinding`.

    The value-carrying counterpart to :func:`scan_for_secrets`, which reduces these to
    bare labels. Detection is identical; this form additionally reports *which* value
    raised each finding, as a digest.

    Args:
        text: The payload to scan.

    Returns:
        A sorted, de-duplicated list of findings; empty if nothing matched.
    """
    findings: set[SecretFinding] = set()

    for label, pattern in _HIGH_SIGNAL_PATTERNS:
        for match in pattern.finditer(text):
            findings.add(SecretFinding(label, secret_digest(match.group(0))))

    for match in _ASSIGNMENT_RE.finditer(text):
        identifier, separator, raw_value = match.group(1), match.group(2), match.group(3)
        keyword = _assignment_keyword(identifier)
        if keyword is None:
            continue
        # The value pattern stops only at whitespace or a quote, so closing syntax rides
        # along; strip it before classifying, and re-apply the documented >=6 char floor
        # so a value that was only long enough *with* its punctuation stops counting.
        value = raw_value.rstrip(_VALUE_TRAILER_CHARS)
        if len(value) < 6:
            continue
        if _is_self_reference(identifier, value) or _is_dotted_reference(
            identifier, value
        ):
            continue
        if separator == ":" and _TYPE_ANNOTATION_VALUE_RE.fullmatch(value):
            continue
        if not _looks_secret_shaped(value):
            continue
        findings.add(
            SecretFinding(f"generic-assignment:{keyword}", secret_digest(value))
        )

    return sorted(findings)


# Repo-relative location of the committed suppression manifest. It must be *committed*
# (not git-ignored): the doers scan a detached checkout of HEAD, so only tracked files
# exist there, and being tracked is exactly what puts every suppression in front of a
# human reviewer in the PR diff.
SECRET_ALLOWLIST_PATH = ".cohort/secret-scan-allow.txt"

# One manifest entry: `<digest>  <repo-relative-path>` with an optional `# note`. Both
# fields are required — a digest alone would suppress a value everywhere in the repo, and
# a path alone is the blind spot this design exists to avoid.
_ALLOWLIST_ENTRY_RE = re.compile(
    r"^\s*([0-9a-f]{16})\s+(\S+)\s*(?:#.*)?$"
)


class SecretSuppression(NamedTuple):
    """A declared, committed exemption for one known-fake value in one file.

    Attributes:
        digest: :func:`secret_digest` of the exact value to suppress.
        path: Repo-relative path the suppression applies to.
    """

    digest: str
    path: str


def parse_secret_allowlist(text: str) -> frozenset[SecretSuppression]:
    """Parse the committed suppression manifest into a set of exemptions.

    The manifest declares credential-shaped content a repo *knows* is fake — a scanner's
    own regression fixtures, the AWS documentation example key, a docstring quoting
    ``postgres://svc:S3cret@db/prod``. Each entry binds a **value digest to a path**, so
    the exemption cannot travel: a real credential dropped into an exempted file has a
    different digest and still blocks the gate. This is the property a bare path
    allowlist gives up.

    Format — one entry per line, blank lines and ``#`` comments ignored::

        # why this fixture is fake
        a1b2c3d4e5f60718  tests/test_gates.py  # AWS documentation example key

    Fails **closed** on anything it cannot parse: a malformed line is skipped, so it
    suppresses nothing and the finding it was meant to cover still blocks. A typo can
    only ever make the gate stricter, never weaker.

    Args:
        text: Full text of the manifest file.

    Returns:
        The declared suppressions; empty if the manifest is absent or unparseable.
    """
    entries: set[SecretSuppression] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ALLOWLIST_ENTRY_RE.match(line)
        if match is None:
            continue
        entries.add(SecretSuppression(match.group(1), _normalize_path(match.group(2))))
    return frozenset(entries)


def unsuppressed_findings(
    findings: list[SecretFinding],
    path: str,
    allowlist: frozenset[SecretSuppression],
) -> list[SecretFinding]:
    """Drop findings in ``path`` that the manifest declares known-fake.

    Args:
        findings: Findings raised by :func:`scan_for_secret_findings` for one file.
        path: Repo-relative path of that file.
        allowlist: Declared suppressions, from :func:`parse_secret_allowlist`.

    Returns:
        The findings that remain — every one of which must block the gate.
    """
    normalized = _normalize_path(path)
    return [
        finding
        for finding in findings
        if SecretSuppression(finding.digest, normalized) not in allowlist
    ]


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


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Segment-level match for auth/crypto/secret names. A keyword must be followed by a
# non-alphanumeric character or end the segment, so `auth.py`, `auth_helpers.py` and
# `secrets/` classify but `authors/` and `secretariat/` do not. Bare prefix matching
# would let an innocuous directory name classify as sensitive — which, combined with
# the same-class override rule in `check_changed_paths`, previously let a footprint
# like `authors/**` launder a `.git` write beneath it.
_AUTH_SEGMENT_RE = re.compile(
    r"(auth|authn|authz|authentication|authorization"
    r"|crypto|cryptography|secret|secrets)([^a-z0-9]|$)"
)


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
    """True if a normalized path is absolute, escapes the repo root, or is degenerate.

    "Absolute" covers both posix (`/etc/passwd`) and Windows drive-qualified
    (`C:/Windows/...`) forms. The drive check matters on every platform, not just
    Windows: :func:`_normalize_path` folds `C:\\Windows` to `C:/Windows`, which has no
    leading `/` and would otherwise read as an ordinary relative path.
    """
    if normalized in ("", "."):
        return True
    if normalized.startswith("/"):
        return True
    if _WINDOWS_DRIVE_RE.match(normalized) is not None:
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
    ``*.lock`` for lockfiles, ``*.sh``/``*.bash``/``*.ps1``/``Makefile``/``Dockerfile``
    at any depth for executable scripts) so it fails closed on near-misses.
    """
    segments = [seg for seg in path.split("/") if seg not in ("", ".")]
    if not segments:
        return None
    base = segments[-1].lower()
    lower = path.lower()

    if any(seg.lower().startswith(".env") for seg in segments):
        return "dotenv"
    if any(seg.lower().rstrip(". ") == ".git" for seg in segments):
        # Any `.git` directory, not just the repo-root one — a nested `.git`
        # (a submodule's git dir, or one under any subdirectory) is just as
        # sensitive as the top-level one and must not be written blindly.
        # Windows silently strips trailing dots/spaces from a path segment when
        # opening it, so `.git.` or `.git ` opens the very same directory as
        # `.git` — strip them before the equality check so that dodge doesn't
        # slip a write past this gate.
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
    if base.endswith((".sh", ".bash", ".ps1")) or base in {"makefile", "dockerfile"}:
        # An executable script or build-entrypoint file anywhere in the tree, not
        # just at repo root — a nested `scripts/release.sh` or `docker/Dockerfile`
        # is just as capable of running arbitrary commands as a root-level one.
        return "executable-script"
    if any(_AUTH_SEGMENT_RE.match(seg.lower()) is not None for seg in segments):
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
    manifest or script, an executable script (``*.sh``/``*.bash``/``*.ps1``,
    ``Makefile``, ``Dockerfile``) at any depth, or an auth/crypto/secret/``.env``
    path — which requires elevated approval *regardless of footprint*.

    A sensitive path can be allowed only by an **explicit, reviewed override**: a
    footprint entry that matches the path and is classified into the *same* sensitive
    class (e.g. listing ``src/auth.py`` or ``.env`` by name). A broad footprint such as
    ``**`` never overrides sensitivity — that is the whole point of the gate — and an
    entry sensitive in one class never authorizes a path sensitive in another.

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
            # An override must match the path's *own* sensitive class. Accepting any
            # sensitive entry would let one class launder another — e.g. a footprint
            # of `src/auth/**` (auth-crypto-secret) authorizing a `src/auth/.git/config`
            # write (git-internal), which is not what listing an auth path consents to.
            overridden = any(
                _within_footprint(normalized, entry)
                and _classify_sensitive(_normalize_path(entry)) == sensitive_class
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


def assert_total_wire_bytes(
    *,
    instruction_text: str,
    file_bytes: int,
    max_bytes: int = 5_000_000,
) -> None:
    """Bound the **total** bytes a doer dispatch would expose to an external engine.

    :func:`assert_payload_within` caps a single prompt string, but a *doer* dispatch
    egresses far more than the task text: the vendor CLI reads the worktree's committed
    files and sends them to the vendor alongside the task. This bounds the whole exposed
    payload — the UTF-8 byte length of ``instruction_text`` plus ``file_bytes`` (the
    summed byte length of every tracked worktree file the CLI could read) — so a runaway
    worktree (a checked-in data blob, a vendored binary tree) cannot be silently shipped
    off-machine with no ceiling.

    This is a fail-closed backstop, so the caller must compute ``file_bytes``
    fail-closed: a tracked file whose size cannot be measured must abort the dispatch
    upstream rather than be dropped from the sum, or an unmeasured file could push the
    real payload past the cap while this check reads under it.

    Args:
        instruction_text: The task/instruction string sent alongside the files.
        file_bytes: The summed byte length of every tracked worktree file the CLI could
            read and egress. Must be counted fail-closed by the caller.
        max_bytes: The maximum permitted total exposed bytes.

    Raises:
        PayloadTooLargeError: if ``len(instruction_text) + file_bytes`` (UTF-8) exceeds
            ``max_bytes``.
    """
    total = len(instruction_text.encode("utf-8")) + file_bytes
    if total > max_bytes:
        raise PayloadTooLargeError(
            f"doer dispatch would expose {total} bytes "
            f"(task text + {file_bytes} bytes of tracked worktree files), "
            f"exceeds the {max_bytes}-byte wire cap"
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

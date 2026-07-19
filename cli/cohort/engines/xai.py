"""xAI (Grok) API-direct client — OpenAI-compatible chat/completions over stdlib HTTP.

Claude packages the prompt, this module POSTs it to xAI's HTTP API and returns the
assistant's *text*. The engine never executes local tool calls; it only answers.

Security invariants (from the RFC 0004 privacy review — non-negotiable):

* stdlib only — :mod:`urllib.request` / :mod:`urllib.error`, never ``requests``.
* The API key is read from the environment on demand and is **never** logged,
  printed, or embedded in any exception message or ``repr``. Every raised error
  here carries only non-secret context (status codes, byte counts, env-var names).
"""

from __future__ import annotations

import email.utils
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from cohort.engines import EngineSpec, get_engine

# Cap on how long we will honour a 429 ``Retry-After`` before giving up, so a
# hostile or misconfigured header cannot pin the process for minutes.
_MAX_RETRY_AFTER_SECONDS: float = 30.0


class EngineError(Exception):
    """Base class for all xAI client failures."""


class EngineAuthError(EngineError):
    """The auth env var is unset/empty, or the API rejected the key (401/403)."""


class EngineUnavailableError(EngineError):
    """Network error, timeout, 5xx, or malformed response after the one retry."""


class EnginePayloadError(EngineError):
    """The prompt exceeds the configured byte cap (raised before any network I/O)."""


def estimate_tokens(text: str) -> int:
    """Conservatively estimate the token count of ``text`` (~3 chars per token)."""
    return math.ceil(len(text) / 3)


def _resolve_api_key(spec: EngineSpec) -> str:
    """Return the API key for ``spec`` from the environment, stripped of surrounding
    whitespace so a stray newline cannot corrupt the ``Authorization`` header.

    Raises:
        EngineAuthError: if the spec declares no auth env var, or the var is
            unset/empty. The message names only the env var, never a secret.
    """
    env_name = spec.auth_env
    if not env_name:
        raise EngineAuthError(f"engine {spec.name!r} declares no auth env var")
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise EngineAuthError(
            f"environment variable {env_name} is unset or empty; "
            f"export it with your xAI API key to use the {spec.name!r} engine"
        )
    return key


def _build_request(
    endpoint: str, key: str, body: dict[str, Any]
) -> urllib.request.Request:
    """Build the POST request. The key is used only to set the bearer header."""
    data = json.dumps(body).encode("utf-8")
    return urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )


def _retry_after_seconds(headers: Any) -> float:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into a bounded,
    non-negative number of seconds. Unparseable or absent → ``0.0``."""
    if headers is None:
        return 0.0
    raw = headers.get("Retry-After")
    if not raw:
        return 0.0
    raw = raw.strip()
    if raw.isdigit():
        return min(float(raw), _MAX_RETRY_AFTER_SECONDS)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return 0.0
    if when is None:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(delta, 0.0), _MAX_RETRY_AFTER_SECONDS)


def _parse_assistant_text(raw: bytes) -> str:
    """Extract ``choices[0].message.content`` from an xAI chat/completions body.

    Defends against a 200 that carries an ``error`` object, a non-JSON body, a
    missing ``choices`` array, and empty content.

    Raises:
        EngineUnavailableError: if the body is not usable assistant text.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise EngineUnavailableError("xAI returned a non-JSON response") from None
    if isinstance(payload, dict) and payload.get("error"):
        raise EngineUnavailableError("xAI returned an error response")
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise EngineUnavailableError(
            "xAI response missing choices[0].message.content"
        ) from None
    if not isinstance(content, str) or not content.strip():
        raise EngineUnavailableError("xAI returned empty assistant content")
    return content


def consult(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = 60.0,
    max_tokens: int | None = None,
    max_prompt_bytes: int = 200_000,
) -> str:
    """POST an OpenAI-compatible chat/completions request to xAI and return the
    assistant text.

    Args:
        prompt: The user message to send.
        model: Model id to request; defaults to the grok flagship from the registry.
        timeout: Per-attempt timeout in seconds.
        max_tokens: Optional cap on the response length (API ``max_tokens``).
        max_prompt_bytes: Refuse (fail closed) if the UTF-8 prompt exceeds this —
            the primary cost control, enforced before any network call.

    Retry policy: at most one retry, and only when no usable response was received
    (connection error, timeout, or 5xx). A 429 is retried once honouring
    ``Retry-After``. Other 4xx are not retried (401/403 → auth error).

    Raises:
        EnginePayloadError: prompt exceeds ``max_prompt_bytes``.
        EngineAuthError: auth env var unset/empty, or the API returned 401/403.
        EngineUnavailableError: network failure, timeout, 5xx, a non-429 4xx, or a
            malformed/empty response.
    """
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > max_prompt_bytes:
        raise EnginePayloadError(
            f"prompt is {prompt_bytes} bytes, exceeds the {max_prompt_bytes}-byte cap "
            f"(~{estimate_tokens(prompt)} estimated tokens)"
        )

    spec = get_engine("grok")
    endpoint = spec.endpoint
    if not endpoint:
        raise EngineUnavailableError(f"engine {spec.name!r} has no endpoint configured")
    key = _resolve_api_key(spec)
    chosen_model = model or spec.model_tiers["flagship"]

    body: dict[str, Any] = {
        "model": chosen_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    request = _build_request(endpoint, key, body)

    retried = False
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            return _parse_assistant_text(raw)
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                # Do not echo the server body — it can reflect request material.
                raise EngineAuthError(
                    f"xAI rejected the credentials (HTTP {status})"
                ) from None
            if status == 429 and not retried:
                retried = True
                delay = _retry_after_seconds(exc.headers)
                if delay > 0:
                    time.sleep(delay)
                continue
            if 500 <= status < 600 and not retried:
                retried = True
                continue
            raise EngineUnavailableError(f"xAI returned HTTP {status}") from None
        except (urllib.error.URLError, TimeoutError):
            # Connection error or timeout — no response body was received.
            if not retried:
                retried = True
                continue
            raise EngineUnavailableError(
                "xAI request failed to reach the API after one retry"
            ) from None

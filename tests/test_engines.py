"""Tests for the external-engine registry and the xAI API-direct client.

The network is always mocked (``urllib.request.urlopen`` is monkeypatched); no test
performs real HTTP. Tests assert on behaviour — returned text, retry counts, raised
error types, and the invariant that the API key never appears in an error message.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from cohort.engines import ENGINES, EngineSpec, UnknownEngineError, get_engine
from cohort.engines import xai as xai_module
from cohort.engines.xai import (
    EngineAuthError,
    EnginePayloadError,
    EngineUnavailableError,
    consult,
    estimate_tokens,
)

_SECRET = "xai-super-secret-key-value"


class _FakeResponse:
    """Minimal context-manager stand-in for a urllib HTTP response."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self, size: int | None = None) -> bytes:
        # Mirrors the real HTTPResponse signature: the client reads a bounded number of
        # bytes (a size-less read would let a hostile endpoint buffer without limit).
        return self._body if size is None else self._body[:size]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _completion(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.x.ai/v1/chat/completions",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


class _Recorder:
    """Records how many times it is invoked and returns queued urlopen outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.calls += 1
        outcome = self._outcomes[self.calls - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", recorder)


def test_xai_client_returns_assistant_text_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_FakeResponse(_completion("hello from grok"))])
    _patch_urlopen(monkeypatch, recorder)

    assert consult("hi") == "hello from grok"
    assert recorder.calls == 1


def test_xai_client_raises_auth_error_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    recorder = _Recorder([])
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EngineAuthError) as excinfo:
        consult("hi")

    # The env-var name is helpful context; the secret must never appear, and no
    # network call may be attempted when the key is absent.
    assert _SECRET not in str(excinfo.value)
    assert "GROK_API_KEY" in str(excinfo.value)
    assert recorder.calls == 0


def test_xai_client_treats_whitespace_only_key_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A key that is only whitespace (e.g. a stray newline) is treated as unset:
    # it must fail closed as an auth error and never reach the network.
    monkeypatch.setenv("GROK_API_KEY", "  \n  ")
    recorder = _Recorder([])
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EngineAuthError):
        consult("hi")
    assert recorder.calls == 0


def test_xai_client_retries_once_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder(
        [_http_error(503), _FakeResponse(_completion("recovered"))]
    )
    _patch_urlopen(monkeypatch, recorder)

    assert consult("hi") == "recovered"
    assert recorder.calls == 2


def test_xai_client_retries_once_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder(
        [urllib.error.URLError("connection refused"), _FakeResponse(_completion("ok"))]
    )
    _patch_urlopen(monkeypatch, recorder)

    assert consult("hi") == "ok"
    assert recorder.calls == 2


def test_xai_client_backs_off_before_retrying_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The single 5xx retry must pause a bounded backoff first, not re-POST immediately.
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_http_error(503), _FakeResponse(_completion("recovered"))])
    _patch_urlopen(monkeypatch, recorder)
    slept: list[float] = []
    monkeypatch.setattr(xai_module.time, "sleep", lambda s: slept.append(s))

    assert consult("hi") == "recovered"
    assert recorder.calls == 2
    assert slept == [xai_module._RETRY_BACKOFF_SECONDS]
    assert 0 < xai_module._RETRY_BACKOFF_SECONDS <= 2  # bounded, not exponential


def test_xai_client_backs_off_before_retrying_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder(
        [urllib.error.URLError("connection refused"), _FakeResponse(_completion("ok"))]
    )
    _patch_urlopen(monkeypatch, recorder)
    slept: list[float] = []
    monkeypatch.setattr(xai_module.time, "sleep", lambda s: slept.append(s))

    assert consult("hi") == "ok"
    assert slept == [xai_module._RETRY_BACKOFF_SECONDS]


def test_xai_client_success_path_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_FakeResponse(_completion("hi back"))])
    _patch_urlopen(monkeypatch, recorder)
    slept: list[float] = []
    monkeypatch.setattr(xai_module.time, "sleep", lambda s: slept.append(s))

    assert consult("hi") == "hi back"
    assert slept == []  # no retry, no backoff


def test_xai_client_does_not_retry_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_http_error(400)])
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EngineUnavailableError):
        consult("hi")
    assert recorder.calls == 1


def test_xai_client_maps_401_to_auth_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_http_error(401)])
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EngineAuthError) as excinfo:
        consult("hi")
    assert _SECRET not in str(excinfo.value)
    assert recorder.calls == 1


def test_xai_client_gives_up_after_second_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_http_error(500), _http_error(500)])
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EngineUnavailableError):
        consult("hi")
    assert recorder.calls == 2


def test_xai_client_rejects_malformed_success_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([_FakeResponse({"choices": []})])
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EngineUnavailableError):
        consult("hi")


def test_consult_rejects_oversized_prompt_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_API_KEY", _SECRET)
    recorder = _Recorder([])  # any call would IndexError; assert none happen
    _patch_urlopen(monkeypatch, recorder)

    with pytest.raises(EnginePayloadError):
        consult("x" * 50, max_prompt_bytes=10)
    assert recorder.calls == 0


def test_estimate_tokens_uses_three_chars_per_token() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2  # ceil(4/3)


def test_get_engine_returns_grok_spec() -> None:
    spec = get_engine("grok")
    assert isinstance(spec, EngineSpec)
    assert spec.name == "grok"
    assert spec.transport == "xai_chat_completions"
    assert spec.endpoint == "https://api.x.ai/v1/chat/completions"
    assert spec.auth_env == "GROK_API_KEY"
    assert spec.roles == frozenset({"consult", "patch_proposal"})
    assert spec.cost_class == "metered"
    # Concrete verified ids, not moving aliases: `grok-4-latest`/`grok-code-fast-1`
    # resolve server-side to `grok-4.3`/`grok-build-0.1`, so the flagship alias served
    # the second tier. Pin the real ids the account lists.
    assert spec.model_tiers["cheap"] == "grok-4.3"
    assert spec.model_tiers["flagship"] == "grok-4.5"


def test_get_engine_unknown_name_raises() -> None:
    with pytest.raises(UnknownEngineError):
        get_engine("does-not-exist")
    # KeyError-compatible for callers that catch the stdlib type.
    with pytest.raises(KeyError):
        get_engine("does-not-exist")


def test_registry_contains_grok() -> None:
    assert "grok" in ENGINES


# --------------------------------------------------------------------------- #
# The timeout must be able to cover the work requested (wickwork field report)
# --------------------------------------------------------------------------- #


def test_timeout_scales_with_the_tokens_requested() -> None:
    """`engine consult` defaults to max_tokens=4096 while this client defaulted to a 60s
    timeout, and grok-4.5 emits roughly 50 tokens/second — so the command could not
    succeed at its own defaults. Every substantive consult timed out twice and reported
    a network failure."""
    from cohort.engines.xai import timeout_for

    assert timeout_for(4096) > 60.0          # the combination that was unsatisfiable
    assert timeout_for(4096) > timeout_for(500)
    assert timeout_for(None) >= 120.0        # a floor even when nothing is requested
    assert timeout_for(0) >= 120.0


def test_a_timeout_is_reported_as_a_timeout_not_as_unreachable(monkeypatch) -> None:
    """They are caught together but mean opposite things: a timeout is a healthy API being
    slow, a connection error is an unreachable one. Reporting both as "failed to reach the
    API" sent a user hunting a network problem that did not exist."""
    import urllib.request

    from cohort.engines import xai

    monkeypatch.setenv("GROK_API_KEY", "test-key-not-real")

    def always_timeout(*_a, **_k):
        raise TimeoutError("slow")

    monkeypatch.setattr(urllib.request, "urlopen", always_timeout)
    monkeypatch.setattr(xai.time, "sleep", lambda _s: None)

    with pytest.raises(xai.EngineUnavailableError) as excinfo:
        xai.consult("hi", max_tokens=4096)

    message = str(excinfo.value)
    assert "did not respond" in message      # named as a timeout
    assert "reachable" in message            # and explicitly not a network failure
    assert "max-tokens" in message           # with the lever that fixes it


def test_a_connection_error_is_still_reported_as_unreachable(monkeypatch) -> None:
    """The other half must not regress into the timeout wording."""
    import urllib.error
    import urllib.request

    from cohort.engines import xai

    monkeypatch.setenv("GROK_API_KEY", "test-key-not-real")

    def always_refused(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", always_refused)
    monkeypatch.setattr(xai.time, "sleep", lambda _s: None)

    with pytest.raises(xai.EngineUnavailableError) as excinfo:
        xai.consult("hi", max_tokens=100)

    assert "could not be reached" in str(excinfo.value)


def test_an_explicit_timeout_overrides_the_derived_one(monkeypatch) -> None:
    """`--timeout` has to win, or a user cannot recover from a bad derivation."""
    import urllib.request

    from cohort.engines import xai

    monkeypatch.setenv("GROK_API_KEY", "test-key-not-real")
    seen = {}

    def capture(_req, timeout=None):
        seen["timeout"] = timeout
        raise TimeoutError("slow")

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    monkeypatch.setattr(xai.time, "sleep", lambda _s: None)

    with pytest.raises(xai.EngineUnavailableError):
        xai.consult("hi", max_tokens=4096, timeout=7.0)

    assert seen["timeout"] == 7.0

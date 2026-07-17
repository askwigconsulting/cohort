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

    def read(self) -> bytes:
        return self._body

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
    assert spec.model_tiers["cheap"] == "grok-code-fast-1"
    assert spec.model_tiers["flagship"] == "grok-4-latest"


def test_get_engine_unknown_name_raises() -> None:
    with pytest.raises(UnknownEngineError):
        get_engine("does-not-exist")
    # KeyError-compatible for callers that catch the stdlib type.
    with pytest.raises(KeyError):
        get_engine("does-not-exist")


def test_registry_contains_grok() -> None:
    assert "grok" in ENGINES

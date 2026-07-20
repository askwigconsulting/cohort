"""Registry of external (non-Claude) engines usable as orchestrated doers.

An :class:`EngineSpec` is a *description* of a model endpoint — never a live
client. Transport code (see :mod:`cohort.engines.xai`) reads a spec to learn the
endpoint, the env var that holds the key, and the model tiers; it never hard-codes
vendor identifiers. Keeping the registry generic (no ``if name == "grok"`` branches
in accessor logic) is what lets a second engine be added by data alone.

See RFC 0004 (issue #171). Phase 1 ships the "grok" entry via xAI's
OpenAI-compatible chat/completions API, API-direct: Claude packages the context,
calls the HTTP API, and gets back *text* — the engine never executes local tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

# Roles an engine may be trusted with. "consult" = advisory second opinion;
# "patch_proposal" = may return a proposed diff (still reviewed, never applied
# blindly). Kept as a module constant so specs validate against one source.
KNOWN_ROLES: frozenset[str] = frozenset({"consult", "patch_proposal"})

# Recognised cost classes for an engine's billing model.
KNOWN_COST_CLASSES: frozenset[str] = frozenset({"metered", "subscription"})


@dataclass(frozen=True)
class EngineSpec:
    """Immutable description of one external engine endpoint.

    Attributes:
        name: Stable registry key (e.g. ``"grok"``).
        transport: Identifier of the wire protocol / client to use
            (e.g. ``"xai_chat_completions"``). Dispatch keys off this, not
            ``name``, so unrelated engines can share a transport.
        endpoint: Full HTTP(S) URL to POST to, or ``None`` if the transport
            supplies its own default.
        auth_env: Name of the environment variable holding the API key, or
            ``None`` for an unauthenticated transport. The *value* is never
            stored on the spec.
        roles: Subset of :data:`KNOWN_ROLES` this engine is trusted with.
        cost_class: One of :data:`KNOWN_COST_CLASSES`.
        model_tiers: Mapping of tier name (e.g. ``"cheap"``, ``"flagship"``)
            to the concrete model id to request.
    """

    name: str
    transport: str
    endpoint: str | None
    auth_env: str | None
    roles: frozenset[str]
    cost_class: str
    model_tiers: Mapping[str, str]


class UnknownEngineError(KeyError):
    """Raised by :func:`get_engine` when no engine is registered under a name.

    Subclasses :class:`KeyError` so callers may catch either the specific type or
    the generic ``KeyError``.
    """


ENGINES: dict[str, EngineSpec] = {
    "grok": EngineSpec(
        name="grok",
        transport="xai_chat_completions",
        endpoint="https://api.x.ai/v1/chat/completions",
        auth_env="GROK_API_KEY",
        roles=frozenset({"consult", "patch_proposal"}),
        cost_class="metered",
        # Pin concrete, verified model ids — never moving aliases. The xAI aliases
        # `grok-4-latest` and `grok-code-fast-1` silently resolve to `grok-4.3` and
        # `grok-build-0.1` respectively (confirmed against the response `model` field),
        # so the "flagship" alias was quietly serving the second tier. Name the real
        # ids the account lists so the tier we request is the tier we get.
        model_tiers=MappingProxyType(
            {"cheap": "grok-4.3", "flagship": "grok-4.5"}
        ),
    ),
}


def get_engine(name: str) -> EngineSpec:
    """Return the registered :class:`EngineSpec` for ``name``.

    Raises:
        UnknownEngineError: if no engine is registered under ``name``.
    """
    try:
        return ENGINES[name]
    except KeyError:
        raise UnknownEngineError(name) from None

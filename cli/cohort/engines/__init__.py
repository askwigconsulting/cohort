"""Registry of external (non-Claude) engines usable as orchestrated doers.

An :class:`EngineSpec` is a *description* of a model endpoint — never a live
client. Transport code (see :mod:`cohort.engines.xai` and
:mod:`cohort.engines.codex_cli`) reads a spec to learn the endpoint, the env var that
holds the key, and the model tiers; it never hard-codes vendor identifiers. Keeping the
registry generic (no ``if name == "grok"`` branches in accessor logic) is what lets a
second engine be added by data alone.

Every command that takes an engine name resolves it here, through
:func:`get_engine` / :func:`resolve_engine_name`, so ``gpt``, ``chatgpt``, ``openai``
and ``codex`` mean one engine everywhere (``consult``, ``review``, ``work``,
``ratchet``) and ``xai`` is ``grok`` everywhere — one alias set, one owner (#243).

See RFC 0004 (issue #171). Phase 1 ships the "grok" entry via xAI's
OpenAI-compatible chat/completions API, API-direct: Claude packages the context,
calls the HTTP API, and gets back *text* — the engine never executes local tools.
The "codex" entry (#266) is ChatGPT through the OpenAI Codex CLI's read-only sandbox,
registered for ``consult`` only: Cohort runs the gates on the assembled prompt, then
the CLI owns the wire.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

# Roles an engine may be trusted with. "consult" = one-shot advisory second opinion
# (Claude packages the context); "review" = an advisory read-only tool loop over the
# repo (each read egress-gated); "patch_proposal" = may return a proposed diff (still
# reviewed, never applied blindly). Kept as a module constant so specs validate against
# one source.
KNOWN_ROLES: frozenset[str] = frozenset({"consult", "review", "patch_proposal"})

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
            supplies its own default (or owns the wire entirely, as a vendor CLI does).
        auth_env: Name of the environment variable holding the API key, or
            ``None`` for an unauthenticated transport. The *value* is never
            stored on the spec.
        roles: Subset of :data:`KNOWN_ROLES` this engine is trusted with.
        cost_class: One of :data:`KNOWN_COST_CLASSES`.
        model_tiers: Mapping of tier name (e.g. ``"cheap"``, ``"flagship"``)
            to the concrete model id to request. Empty when the transport picks the
            model itself (a CLI's default) and only an explicit ``--model`` overrides.
        aliases: Other names that resolve to this engine (e.g. ``"gpt"`` for
            ``"codex"``). Lower-case; must not collide with any other engine's name or
            aliases (checked when the registry is built).
    """

    name: str
    transport: str
    endpoint: str | None
    auth_env: str | None
    roles: frozenset[str]
    cost_class: str
    model_tiers: Mapping[str, str]
    aliases: frozenset[str] = frozenset()


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
        roles=frozenset({"consult", "review", "patch_proposal"}),
        cost_class="metered",
        # Pin concrete, verified model ids — never moving aliases. The xAI aliases
        # `grok-4-latest` and `grok-code-fast-1` silently resolve to `grok-4.3` and
        # `grok-build-0.1` respectively (confirmed against the response `model` field),
        # so the "flagship" alias was quietly serving the second tier. Name the real
        # ids the account lists so the tier we request is the tier we get.
        #
        # Every id below was probed live against /v1/models and /v1/chat/completions
        # (2026-07-31) and confirmed to serve back its own name. Two ids people reach
        # for do NOT work and are deliberately absent:
        #   * `grok-4-heavy` — not on the account at all ("Model not found").
        #   * `grok-4.20-multi-agent-0309` — listed by /v1/models, but chat/completions
        #     refuses it ("Multi Agent requests are not allowed on chat completions");
        #     reaching it needs a different transport, so naming it as a tier here would
        #     hand callers a model that fails at dispatch.
        # `reasoning` is the third distinct tier the audit's model rotation needs (#240).
        model_tiers=MappingProxyType(
            {
                "cheap": "grok-4.3",
                "flagship": "grok-4.5",
                "reasoning": "grok-4.20-0309-reasoning",
            }
        ),
        aliases=frozenset({"xai"}),
    ),
    "codex": EngineSpec(
        name="codex",
        transport="codex_cli",
        # The Codex CLI owns the wire: Cohort never POSTs to OpenAI itself, it runs
        # `codex exec --sandbox read-only` and reads the reply from the CLI's stdout.
        endpoint=None,
        # Optional. A saved `codex login` (ChatGPT sign-in under ~/.codex) is the
        # default and needs no variable at all; the key is the unattended path (CI,
        # loops) and rides the scrubbed environment through to the CLI when set.
        auth_env="OPENAI_API_KEY",
        # `consult` only. `review`/`patch_proposal` would need a codex-specific tool
        # loop or patch parser that does not exist; leaving the roles unregistered makes
        # `engine review gpt` / `engine propose gpt` refuse with a clear error instead
        # of routing an OpenAI key at xAI's endpoint. `work`/`ratchet` reach codex's
        # own sandboxed doer by transport, not by role.
        roles=frozenset({"consult"}),
        cost_class="subscription",
        # No tiers: a consult uses the CLI's default flagship model (never a silent
        # downgrade, and it advances as the CLI's default does); `--model` pins one.
        model_tiers=MappingProxyType({}),
        aliases=frozenset({"gpt", "chatgpt", "openai"}),
    ),
}


def _build_alias_index(engines: Mapping[str, EngineSpec]) -> Mapping[str, str]:
    """Map every name (canonical or alias) to its canonical engine name.

    Raises:
        ValueError: if a name reaches two engines — an ambiguous registry must fail at
            import, not route one user's ``gpt`` to the wrong vendor at dispatch.
    """
    index: dict[str, str] = {}
    for spec in engines.values():
        for candidate in (spec.name, *spec.aliases):
            owner = index.get(candidate)
            if owner is not None and owner != spec.name:
                raise ValueError(
                    f"engine name {candidate!r} is claimed by both {owner!r} and {spec.name!r}"
                )
            index[candidate] = spec.name
    return MappingProxyType(index)


ENGINE_ALIASES: Mapping[str, str] = _build_alias_index(ENGINES)


def resolve_engine_name(name: str) -> str:
    """Return the canonical registry key for ``name`` (a canonical name or an alias).

    Matching ignores case and surrounding whitespace, so ``" GPT "`` is ``"codex"``.

    Raises:
        UnknownEngineError: if ``name`` reaches no registered engine.
    """
    key = name.strip().lower()
    try:
        return ENGINE_ALIASES[key]
    except KeyError:
        raise UnknownEngineError(name) from None


def get_engine(name: str) -> EngineSpec:
    """Return the registered :class:`EngineSpec` for ``name`` (canonical or alias).

    Raises:
        UnknownEngineError: if no engine is registered under ``name``.
    """
    return ENGINES[resolve_engine_name(name)]


def aliases_of(name: str) -> frozenset[str]:
    """Every name that resolves to the engine ``name`` names — its canonical key plus
    its aliases — so a caller can keep one alias set per engine without restating it.

    Raises:
        UnknownEngineError: if ``name`` reaches no registered engine.
    """
    spec = get_engine(name)
    return frozenset({spec.name, *spec.aliases})


def describe_registered_engines() -> str:
    """One line naming each engine with its aliases, for "unknown engine" errors —
    e.g. ``codex (aliases: chatgpt, gpt, openai), grok (aliases: xai)``."""
    parts: list[str] = []
    for key in sorted(ENGINES):
        spec = ENGINES[key]
        if spec.aliases:
            parts.append(f"{spec.name} (aliases: {', '.join(sorted(spec.aliases))})")
        else:
            parts.append(spec.name)
    return ", ".join(parts)

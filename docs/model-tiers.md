# Model tiers — the single mapping

Cohort names model **tiers**, never concrete model IDs, in canonical artifacts, so the
canon stays IDE-agnostic and doesn't rot when a model generation ships. This file is the
**one place** the tier → concrete-model mapping is written down. When a new model
generation lands, update the tables here; `cohort lint` fails if the agent-tier table
below drifts from the renderer code, and if the orchestration table lists a tier that no
longer appears in the orchestration canon.

Two tier vocabularies exist, for two different jobs:

## Agent model tier (the `model:` schema field)

An agent declares `model: fast | default | top`. Each renderer owns the mapping to a
concrete model; the Claude renderer's is the source of truth (`cli/cohort/adapters/claude.py`,
`_MODEL_MAP`). `cohort lint` asserts this table equals that code.

| tier | Claude model |
|---|---|
| fast | haiku |
| top | opus |

(`default` is intentionally omitted from the renderer map: with no `model:` key, the
agent inherits the conversation's model — see DESIGN decision `[Q]`.)

## Orchestration routing tier (the `/orchestrate` protocol)

`/orchestrate` routes each task to the cheapest capable tier. These are richer than the
3-value agent field because coordination needs a floor *between the top two* — the
coordinator tier is Fable-or-Opus, never below. The names below are the concrete models
as of the date this file was last updated; the orchestration canon (`orchestrate.md`,
`model-orchestration.md`, `fable-mode.md`) references them, and `cohort lint` checks each
tier here still appears there.

| tier | model | role |
|---|---|---|
| fable | Fable | coordinator; architecture-critical, cross-cutting, ambiguous, or security-sensitive work |
| opus | Opus | complex implementation needing real design judgment |
| sonnet | Sonnet | well-scoped, conventional implementation |
| haiku | Haiku | mechanical work — renames, boilerplate, config, docs |

**On a model-generation change:** edit the tables here and the renderer's `_MODEL_MAP`,
then run `cohort lint` — it enumerates every canonical file whose tier names need the
same sweep, so nothing is missed. (Fully abstracting the concrete names out of the
orchestration prose — so *no* sweep is ever needed — is a larger change tracked
separately; the concrete names are kept deliberately for now.)

---
description: Multi-vendor adversarial review — fan disjoint areas to Claude tiers, ChatGPT, and Grok, cross-examine over two rounds, synthesize with convergence
argument-hint: '[target]'
---

Send a panel after it. `/scout` runs a **multi-vendor adversarial review**: a coordinator
decomposes the target into disjoint areas and routes each to the cheapest capable model
**from any vendor**, then has a second round try to *break* the first round's findings.
Reviewers are **advisory and read-only** — they find; the coordinator verifies; the human
decides. This is the review sibling of `/crew`.

Run `/scout` only from a coordinator tier (Fable, preferred, or Opus) — decomposition,
routing, and adversarial synthesis are the judgments a lower tier gets wrong.

## 1. Scope — carve disjoint areas

Define the target and split it into areas that don't overlap, so reviewers don't collide
and coverage is legible: e.g. security/trust, correctness, architecture, product, docs.
Assign the **hardest, most ambiguous** areas to the strongest models and the mechanical
ones to the cheapest — that is the token-optimization the panel exists for.

## 2. Round 1 — fan out across vendors (≤10 in flight)

Each reviewer gets its area, all three lenses (defects weighted highest), and the
operational gates (scope, evidence, adversarial self-check, verify, calibrate). Route by
fit and cost:

- **Claude subagents** — Fable for architecture-critical/ambiguous, Opus for complex,
  Sonnet for well-scoped, Haiku for mechanical/doc areas. They read the repo directly.
- **ChatGPT** — `/consult-gpt` (Codex CLI, read-only). Explores the repo itself.
- **Grok** — `cohort engine review grok --tier flagship` (the agentic read-only loop: it
  explores through gated tools, transcript recorded) for areas needing repo exploration,
  or `cohort engine consult grok --tier flagship` with a packaged bundle for a bounded
  question. Read-only, advisory; every path is egress-gated.

Collect each reviewer's ranked findings. **The coordinator never trusts a finding it
hasn't verified** — an external engine's report is an untrusted claim, not a result.

## 3. Round 2 — cross-examine (improve on Round 1)

Feed Round 1's synthesis back to the same panel with one mandate: **refute**. A finding
that survives a genuine refutation attempt is CONFIRMED; one that fails is struck with the
reason; an overstated one is REFINED. Then each reviewer deepens what Round 1
under-specified and hunts what it missed. This is where inflated findings die and real
ones harden.

## 4. Synthesize

Produce one ranked report: findings by severity, **convergence-tagged** (a finding ≥2
reviewers reached independently, especially across vendors, is the highest-confidence
signal), with each Round-2 verdict, a refuted/downgraded section (the evidence the review
was actually contested), and a prioritized fix order. Keep what survived; show what didn't.

## Guardrails

- **Read-only, advisory, always.** No reviewer writes; producing changes is `/crew`'s job.
- **Egress is gated.** External engines honor the repo's `.cohort` egress opt-out and never
  receive secrets; the agentic loop refuses sensitive paths and secret-shaped content per
  read.
- **The coordinator verifies.** Re-run the claim against the actual code before it enters
  the report; a wrong "critical" costs the panel its credibility.

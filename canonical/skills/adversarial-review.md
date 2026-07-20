---
name: adversarial-review
kind: skill
scope: global
description: How to run a multi-vendor adversarial review — disjoint areas, model/vendor routing, two-round refutation, convergence synthesis (the /scout method).
targets: [claude]
triggers: [adversarial review, multi-model review, multi-vendor review, cross-vendor review, panel review, review this thoroughly, scout]
---
A **multi-vendor adversarial review** puts independent models from different vendors on a
target, then makes a second round try to break the first. It is the method behind
`/scout`; reach for it when a review needs to be exhaustive and trustworthy rather than
quick — security-sensitive code, an architecture bet, a pre-merge audit. Reviewers are
**advisory and read-only**; the coordinator verifies; the human decides.

## The shape

1. **Carve disjoint areas.** Split the target so reviewers don't overlap (security,
   correctness, architecture, product, docs). Disjoint areas make coverage legible and let
   findings converge meaningfully.
2. **Route by fit and cost.** Put the strongest models on the most ambiguous/high-stakes
   areas, the cheapest on mechanical ones — that is the whole point of a mixed panel.
   Claude tiers (Fable → Opus → Sonnet → Haiku) read the repo directly; **ChatGPT** joins
   via `/consult-gpt` (read-only Codex); **Grok** via `cohort engine review` (an agentic
   read-only loop that explores through gated tools) or `cohort engine consult` for a
   bounded, bundled question. Keep **≤10 reviewers in flight**.
3. **Round 1 finds; Round 2 refutes.** The improvement between rounds is the value: feed
   Round 1's synthesis back with a mandate to *break* each finding. Survives → CONFIRMED;
   fails → struck with the reason; overstated → REFINED. Then deepen and hunt what was
   missed.
4. **Convergence is the signal.** A finding two reviewers reached independently — most of
   all across vendors — is far more trustworthy than any single strong claim. Tag it.
5. **Synthesize honestly.** Rank by severity, show the Round-2 verdicts, and keep a
   refuted/downgraded section — the proof the review was actually contested, not a rubber
   stamp.

## Non-negotiables

- **Read-only, advisory.** A reviewer never writes; changes are `/crew`'s job.
- **Egress is gated, not assumed.** External engines honor the repo's `.cohort` egress
  opt-out, never receive secrets, and the agentic loop refuses sensitive paths and
  secret-shaped content per read — and records an inspectable transcript, because
  exploration you can't audit is trusted, not advisory.
- **Verify before it counts.** An external engine's finding is an untrusted claim; the
  coordinator re-checks it against the real code before it enters the report.

## Common mistakes

- Overlapping areas → reviewers repeat each other and convergence means nothing.
- Skipping Round 2 → inflated findings survive; the refutation pass is where they die.
- Trusting an unverified external claim → treat every off-vendor finding as a lead to
  confirm, never a conclusion.
- One tier for everything → either you overpay (all-flagship) or miss depth (all-cheap);
  match the model to the area.

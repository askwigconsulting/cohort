---
name: crew
kind: command
scope: global
description: Cross-vendor build — a coordinator routes work to the cheapest capable model from any vendor; Claude subagents write, external engines propose gated patches
targets:
- claude
invocation: crew
args:
- name: task
  required: false
  description: The work to build (a feature, a refactor, a set of fixes); defaults to the task under discussion
dry_run: true
---
Put a crew on it. `/crew` is `/orchestrate` widened across vendors: a coordinator
decomposes substantial work and routes each task to the cheapest capable model **from any
vendor**, for the same token-optimization reason `/scout` reviews across vendors. `/crew`
is the build sibling of `/scout`. Everything the `model-orchestration` and `fable-mode`
memories say about coordination — tier floor, disjoint footprints, coordinator signoff —
holds here; this command adds the one rule that makes cross-vendor *building* safe.

Run `/crew` only from a coordinator tier (Fable, preferred, or Opus).

## The line: Claude writes, external engines propose

This is the invariant, not a preference:

- **Claude subagents write directly.** A Claude worker is inside the trust boundary; it
  edits files in a disjoint footprint (or its own worktree) and the coordinator verifies
  the diff. Route by tier: **fable** architecture-critical, **opus** complex, **sonnet**
  well-scoped, **haiku** mechanical.
- **External engines (Grok, ChatGPT) never write.** They are tools the coordinator
  invokes for a candidate change, exactly like `git` or `pytest`. A non-Claude doer
  produces a **gated patch proposal** — `cohort engine propose <engine>` runs the change
  in an isolated worktree, and Cohort (never the engine) parses the reply and applies it
  behind the egress/secret/footprint gates. The coordinator then verifies it like any
  other worker's output, and it still passes the unchanged human PR review.

That is the difference between "let Grok do the work" being useful and being an RCE: an
external engine's diff is an untrusted claim applied through a gate, never a direct write.

## Loop

1. **Research & plan** yourself (coordinator tier). Decompose into tasks with disjoint
   file footprints; identify dependencies.
2. **Route** each task to the cheapest capable model and vendor. Prefer Claude tiers for
   most work; reach for an external doer where it genuinely adds diversity or fits the
   task, remembering it costs more to *verify* (an untrusted diff) than a Claude worker.
3. **Fan out**, **≤10 in flight**. Concurrent writers get per-task git worktrees — a
   shared `.git/index.lock` is not isolated by disjoint files. Workers never commit in
   the coordinator's checkout; the coordinator integrates serially and runs the full
   suite after each integration.
4. **Sign off.** Every worker's output — Claude subagent or external patch — is a claim,
   not a completion. The coordinator reads the diff, re-runs the tests, and verifies the
   acceptance criteria itself before marking a task done, then runs the full suite as the
   integration gate. The human's commit/PR review is unchanged.

## Guardrails

- **Coordinator floor.** Never run `/crew` below Opus — decomposition, routing, and
  cross-vendor signoff are exactly the judgments a lower tier gets wrong.
- **External doers are gated, always.** Direct write access for a non-Claude engine is
  the one thing this command exists to prevent. If a task seems to need it, it's a
  `patch_proposal`, not a shortcut.
- **Verify before done.** An external engine's diff that passes its own gates is still an
  untrusted claim until the coordinator has re-run the tests against it.

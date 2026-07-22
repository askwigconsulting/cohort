---
name: karpathy-discipline
description: Four sharp coding rules (Karpathy) — think before coding, simplicity first, surgical changes, goal-driven — as measurable tests, complementing fable-mode's process gates.
---

Four battle-tested rules for writing code, distilled from Andrej Karpathy's observations
about how LLMs fail at software engineering. They sharpen fable-mode's five process gates
(scope, evidence, adversarial, verify, calibrate) with four *behavioral* rules — each with
a concrete test you can apply to your own diff before you ship it.

## 1. Think before coding — don't assume, surface tradeoffs
State assumptions explicitly and ask rather than guess when the ask is ambiguous. Present
the interpretations when there's real ambiguity. Push back when a simpler approach exists.
Name the unclear thing and get it resolved before writing code that bakes in a wrong guess.

## 2. Simplicity first — the minimum that solves the problem
Implement only what was requested. No speculative abstractions for single-use code, no
unrequested "flexibility" or "configurability," no error handling for scenarios that can't
occur. If 200 lines could be 50, write the 50.
> **Test:** Would a senior engineer call this overcomplicated? If yes, simplify.

## 3. Surgical changes — touch only what you must
Don't improve adjacent code, comments, or formatting. Don't refactor working code you
weren't asked to. Match the file's existing style. Flag unrelated dead code, but don't
delete it. Remove only the imports and names *your* change made unused.
> **Test:** Every changed line traces directly to the request. If a line doesn't, revert it.

## 4. Goal-driven execution — turn tasks into verifiable goals
Before implementing, define the success criteria. For a bug, write the failing test that
reproduces it *first*, then make it pass. For a refactor, capture behavior before and after
and prove it's unchanged. State a brief plan — the steps and the checks — then iterate
against it independently.
> **Test:** Can you name, right now, the check that will tell you this is done and correct?

## How this fits Cohort

fable-mode is the *process* (how a coordinator scopes, gathers evidence, reasons
adversarially, verifies, calibrates); these four are the *craft* rules for the code itself.
Use them as the last pass before you commit — and, ironically, apply #2 and #3 hardest to
the very agents that most want to over-engineer and over-reach: the LLMs writing the code.

## When to use
Use when: karpathy, coding discipline, keep it simple, surgical change, minimal diff, over-engineered, is this overcomplicated, before you code.

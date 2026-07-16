---
name: orchestrate
kind: command
scope: global
description: Fable-led development — research and plan on Fable, fan implementation out to model-tiered agents (max 10 in flight), Fable signs off completion
targets:
- claude
invocation: orchestrate
args:
- name: task
  required: false
  description: The development task to orchestrate (or point at an existing plan/issue)
dry_run: true
---
The standard development protocol: the **coordinating session researches, plans, and
signs off; tiered subagents implement**. `/orchestrate` wraps the existing loops — it
uses `/plan`'s decomposition discipline for phase 2 and `/build`'s
implement–test–verify–commit discipline inside each worker — and adds the model-routing
and signoff layer on top. It is human-invoked; the human gates (commit review, PR
review) are unchanged.

## 0. Coordinator model

Research, decomposition, routing, and signoff are the highest-judgment steps, so
orchestration runs only on a **coordinator-tier** model — **Fable (preferred) or Opus**.
Both are first-class orchestrators; pick by which model is running the session:

- **On Fable** — the ideal coordinator. Proceed.
- **On Opus** — a full coordinator in its own right, not a degraded fallback. Orchestrate
  directly, operating in **Fable mode** (the five-gate discipline in the `fable-mode`
  memory). You may note that Fable suits the largest or most ambiguous work, but do not
  stall to switch. When Opus coordinates, **every fable-tier task routes to opus** — Opus
  is already the top model in the room — and everything else (the cap, the signoff, the
  isolation rule) is unchanged.
- **On Sonnet or Haiku — do not orchestrate.** Decomposition, routing, and adversarial
  signoff are exactly the judgments a lower tier gets wrong, so **the pattern never
  repeats below Opus.** Say so and recommend `/model opus` (or `fable`) before running
  the protocol; if the user prefers to stay put, do the work inline rather than
  coordinating a fan-out from a sub-Opus session.

Whichever model coordinates never delegates the plan or the signoff to a subagent.

## 1. Research — coordinator only

Before any decomposition, the coordinator builds its own picture:

- Read the relevant code, tests, and docs directly (or via read-only Explore agents
  for breadth). Understand the current behavior, the constraints, and the blast radius.
- Surface what is ambiguous. Resolve ambiguity with the user *now* — a wrong
  assumption fanned out to ten agents is ten times the waste.
- State the goal back in one paragraph and get confirmation if the task was vague.

## 2. Plan — decompose into routable tasks

Break the work into tasks the way `/plan` does, with two extra fields per task:

- **Acceptance criteria** — observable outcomes the signoff step will verify.
- **File footprint** — which files the task touches. Tasks with overlapping
  footprints must be sequenced or given worktree isolation; only tasks with
  **disjoint footprints run in parallel**.

Order tasks by dependency. **If the plan contains any fable-tier task**, cross-examine
the plan with `/consult-gpt` before presenting it — ChatGPT's job is to find the flaw,
not to bless the plan; fold anything that survives your own verification back in. If
the consult is unavailable, follow `/consult-gpt`'s unavailability rules: setup
missing → proceed single-model and note the skip; flagship model unavailable (limits,
errors) → ask the user whether to wait or have Fable proceed single-model. Present the plan (tasks, tiers,
parallelism, and the consult's outcome) to the user before fanning out.

## 3. Route — assign each task a model tier

Assign the cheapest tier that can do the task well; escalate on doubt:

| Tier | Route here |
|---|---|
| **fable** | Architecture-critical, cross-cutting, ambiguous, or security-sensitive work; anything where a subtle mistake is expensive |
| **opus** | Complex implementation needing real design judgment within a defined scope |
| **sonnet** | Well-scoped, conventional implementation with clear acceptance criteria |
| **haiku** | Mechanical work — renames, boilerplate, config, doc updates, simple test scaffolding |

Each worker prompt must carry: the task, its acceptance criteria, its file footprint
(and an instruction not to write outside it), the relevant context gathered in
research, `/build`'s discipline (test-first, run the suite, no dead code), and — for
every non-fable worker — the **Fable-mode five gates** (scope before work, evidence
before reasoning, reason adversarially, verify before declaring done, calibrate and
report), stated in the prompt verbatim: a subagent does not inherit the `fable-mode`
memory automatically.

## 4. Fan out — never more than 10 agents in flight

Launch independent tasks concurrently, dependent tasks in dependency order.
**Never more than 10 agents in flight at once, across all tiers** — queue the rest.
Parallel writers require disjoint file footprints or worktree isolation; when in
doubt, serialize. If a worker stalls or dies, its task returns to the queue — the
cap is on live agents, not total tasks. If a fable-tier launch fails mid-run for
credit or availability reasons, reroute that task to opus and note it in the
completion report.

## 5. Signoff — the coordinator verifies, never rubber-stamps

Worker output is a **claim, not a completion**. For every task the coordinator:

1. Reads the diff itself.
2. Verifies each acceptance criterion against the actual code and test results —
   re-running tests rather than trusting a worker's "tests pass" report. Capture
   real exit codes (a piped `pytest | tail` reports tail's status, not pytest's).
3. On failure: return the task **once** to the same worker with the concrete
   failing criteria; if it fails again, escalate one tier and retry; if it fails
   at fable tier, stop and report to the user.
4. For **fable-tier** tasks, additionally gets an independent ChatGPT opinion on the
   diff via `/consult-gpt` (advisory — its findings are claims to verify, never a
   veto or an approval); if the consult is unavailable, apply `/consult-gpt`'s
   unavailability rules (setup missing → skip with a note; model unavailable → ask
   the user: wait, or Fable proceeds single-model).
5. Marks the task complete only after criteria pass.

After all tasks land, the coordinator runs the **full test suite and build** as an
integration check — per-task green does not compose automatically. Report completion
to the user with per-task outcomes; anything skipped or failing is reported plainly,
never presented as done.

## 6. Close the loop

End by offering `cohort snapshot` (capture the decomposition, routing choices, and
outcomes into project context) and `/feedback` on the routing — did the tier
assignments hold up? That signal tunes future routing.

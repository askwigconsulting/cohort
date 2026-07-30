# Orchestration patterns — flat and federated `/crew`

Cohort's orchestration is **coordinator discipline, never runtime-enforced** (DESIGN `[S]`).
This doc is the reference the canon points at for *how* a coordinator fans work out — the
default flat model, and the federated three-tier model for large work — plus the platform
prerequisite the federated model needs and the line it must not cross.

## The two models

### Flat (default): one coordinator → workers

A single coordinator-tier session (Fable or Opus) runs `/crew`: it researches, decomposes,
routes each task to the cheapest capable tier, keeps **≤20 agents in flight at once across
all tiers** (queueing the rest), verifies every task's acceptance criteria itself, and
signs off. Concurrent writers get disjoint file footprints or their own worktrees. This is
the right model for almost everything.

### Federated (three-tier): Director → Managers → Agents

For large work, the coordinator becomes a **Director** that delegates each task-group to an
ephemeral **manager** — itself a coordinator-tier (Fable/Opus) sub-session that runs this
same protocol over its group and returns a result. Numbers:

- **≤5 agents per manager at a time**, and
- **≤20 agents in flight globally** (managers + their live agents combined) — the Director's
  responsibility to keep the total within the global cap. So up to ~4 managers × 5 agents.

The key property that makes 20-under-one-Director safe: **each manager actually verifies its
own group's acceptance criteria** (a real sub-coordinator, not a rubber stamp), and the
Director re-verifies each manager's *group contract* plus **every** foreign-engine diff —
that adversarial scrutiny of untrusted external code is never delegated. Without real
per-manager verification you'd have one coordinator verifying 20 tasks, which is exactly
what the flat cap of the old ≤10 model guarded against.

## When federation pays for itself (and when it's over-engineering)

Use the three-tier model only when **all** of these hold — otherwise stay flat:

- **~20+ cleanly separable task-groups** where the single coordinator's *context* (not the
  cap) is the bottleneck.
- **Disjoint subtrees / worktrees per group** (e.g. `frontend/`, `services/auth/`, `infra/`)
  so each manager owns a footprint-disjoint region and conflicts stay within a group.
- **Group-local verification is meaningful** — each group has an isolable test/acceptance
  surface a manager can run, so its signoff is a real gate.
- **Groups run on different clocks** (long-running / async) so serializing one coordinator's
  attention across them wastes wall-clock.

It is **over-engineering** when: total tasks are ≲15; groups share files (integration must
serialize at the Director anyway); the work is one coherent feature; or any group is
fable-tier (that judgment stays with the top coordinator — never pushed to a manager).

## Platform prerequisite

Three tiers require the platform to permit nested agents. On current Claude Code:

- **Subagent nesting** is off by default (since v2.1.217); enable it with
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=2` (2 layers below the main session: managers, then
  their agents), or
- **Agent Teams** (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) for the top tier — a lead plus
  teammates that message each other; documented best practice is **3–5 teammates**, which is
  why the per-manager cap is 5. Note Agent Teams itself does *not* nest (teammates can't spawn
  teammates), so a full three tiers uses the spawn-depth path for managers→agents.

Where neither is available, **stay flat and queue** — the federated numbers simply don't
apply.

## The line federation must not cross

Federation is a *runtime recursion* of the coordinator→worker pattern. It stays on the right
side of the declared-graph rejection (DESIGN `[S]`) only if it adds **none** of:

- a canonical graph schema or orchestration manifest,
- static validation claiming to prove runtime concurrency or actual file writes,
- machine-maintained node state (queued / running / retry / complete),
- automatic dependency release, retry, heartbeat, or recovery,
- a runtime counter or hook deciding whether another agent may launch,
- **durable manager sessions** — a manager is *ephemeral* (returns a result and is gone). A
  persistent manager reintroduces cross-session scheduler state (which managers are alive,
  their queues), whose crash → stale-state → deadlock is precisely why the scheduler was
  rejected.

What *is* mechanically enforced is only the anti-drift lint on the two cap numbers
(`cohort lint`, single-sourced in [`model-tiers.md`](model-tiers.md)) — it keeps the canon
from stating two different caps; it does not, and cannot, enforce them at runtime.

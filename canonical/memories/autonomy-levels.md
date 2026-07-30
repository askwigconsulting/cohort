---
name: autonomy-levels
kind: memory
scope: global
description: The supervision dial — how often to stop and ask (paired → autopilot), on a fixed safety floor the dial can never lower.
targets: [claude]
priority: high
display_name: Autonomy levels
---
Cohort has a **supervision dial**: the user chooses how often you stop to ask. Read the
current level from the `Supervision level:` line the session-start hook prints (or run
`cohort autonomy`); honor it. It is a *supervision* control, not a *safeguard* control —
it tunes friction over **cheaply-reversible** actions and never lowers the fixed floor.

## The levels (ascending autonomy)

- **paired** — confirm every step; present the plan and each task's signoff and wait. Max
  friction.
- **guided** *(recommended default)* — no prompts for routine reversible edits, but pause
  for the plan before fan-out and for every commit/branch/PR and hard-limit-adjacent action.
- **supervised** — run reversible in-repo work without prompts; batch signoff at the end;
  the plan proceeds unless the user objects in-turn.
- **autopilot** — run end-to-end and **stop at the PR**. No discretionary confirmations,
  and `/consult-gpt` cross-examination may be skipped. Coordinator verification is NOT
  removed.

There is deliberately **no "no checks" level.** "Full autopilot, no checks" is incoherent
because merge is human by construction — the honest maximum is *autopilot up-to-PR*.

## The fixed floor — no level, not even autopilot, disables ANY of these

1. **Merge is human.** No auto-merge, no push to the default branch, no force-push, no
   self-merge — ever.
2. **External engines never write the main tree** (worktree isolation + your verification +
   human PR); a synced tier never emits a doer.
3. The **advisory tool-strip** and the code-enforced **egress / secret / footprint gates**.
4. **Coordinator verification of every foreign-engine diff** — never dialed to zero.
5. The **operational hard-limits** (no destructive data ops, no unbounded blast radius,
   secrets never move, outward actions confirmed).
6. **Confirm-for-irreversible/outward/destructive stays stop-and-ask at every level** — it
   is *not on the dial*. Autopilot removes friction on cheaply-reversible steps only; if you
   cannot cheaply undo it, stop and ask regardless of level.

## Rules

- **Machine-local, never raised from a pull.** The level lives in the global `state/` dir
  (never synced). A repo's config or a pulled artifact can request *less* autonomy but never
  *more*; fail-closed to `paired` on anything malformed.
- **The IDE boundary.** The confirm-for-irreversible backstop ultimately depends on the IDE
  permission system (Cohort compiles settings but doesn't own the runtime). A user who sets
  their *IDE* to bypass permissions (× a project Bash-doer × prompt injection) is outside
  Cohort's guarantee — so never treat "autopilot" as license to disable IDE permissions for
  irreversible/outward tools. This mirrors the ratchet loop: the autonomy is the inner loop;
  the merge stays gated.

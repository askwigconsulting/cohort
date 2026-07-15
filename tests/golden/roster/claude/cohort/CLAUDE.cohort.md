# Cohort office memories

<!-- Compiled from canonical memories; edit canonical and recompile. -->

## Model orchestration

The standard pattern for substantive development work (multi-file changes, features,
refactors) is the `/orchestrate` protocol: the coordinating session — which should run
on **Fable** — does the research, planning, and coordination itself, then decomposes the
work and routes each task to the cheapest capable model tier (**fable** for
architecture-critical or ambiguous work, **opus** for complex implementation, **sonnet**
for well-scoped implementation, **haiku** for mechanical work), with **never more than
10 agents in flight at once**. If Fable is unavailable (credits exhausted, model
errors, or not offered), the protocol defaults to **Opus** — Opus coordinates and
fable-tier tasks route to opus; everything else is unchanged. Parallel workers need
disjoint file footprints or worktree isolation. The coordinator verifies every task's acceptance criteria itself —
re-running tests, reading diffs — before marking it complete, and runs the full suite as
an integration check at the end. Trivial single-file edits stay inline; invoke
`/orchestrate` for anything larger.

## Office routing

A Cohort office of advisory specialist agents is installed in this environment. For questions that
span business functions (legal, finance, HR, compliance, security posture, cloud architecture,
procurement, communications), invoke the **ChiefOfStaff** agent first: it names the right
specialist(s) to consult. Invoke those specialists yourself and hand their input back to
ChiefOfStaff for one reconciled recommendation. Specialists are read-only and advisory — they
recommend; the user decides. A repository may add its own project-scoped specialists under its
`.claude/agents/`; invoke those directly by name.

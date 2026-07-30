---
name: session-recall
kind: hook
scope: global
description: On a fresh session start, surface a prior session's auto-captured record once and nudge promoting its context into durable memory — the exit→next-session bridge.
targets: [claude]
event: session_start
action: cohort session-recall
---
Session exit is harness-side — at `SessionEnd` the model gets no turn and its context
is already gone, so nothing model-authored can be saved *at* exit (only the deterministic
`session-capture` record is written then). This hook closes that gap from the next
session's side: on a `session_start`, `cohort session-recall` checks for a fresh
auto-captured record (`.cohort/sessions/`) that hasn't been surfaced yet, and if it finds
one, injects a standing instruction into the new context — read that record and promote
its key decisions, in-flight state, and open questions into durable memory before
resuming. It writes nothing but a machine-local marker under `state/`, so each record is
surfaced **at most once** and ordinary starts stay quiet.

It is deliberately silent on the `compact` source: `post-compact-memory` already owns the
post-compaction recall, and a compaction also writes an auto record that would otherwise
double-surface here. Pairs with `session-capture` (the deterministic record written at
exit) as the exit-time analog of the `pre-compact-capture` / `post-compact-memory` pair.

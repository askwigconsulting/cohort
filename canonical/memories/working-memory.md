---
name: working-memory
kind: memory
scope: global
description: Stage durable context as you work with `cohort working-note`; it's consolidated into memory at the next compaction or restart, so an abrupt exit never loses the reasoning.
targets: [claude]
priority: normal
display_name: Working memory
---
Context is lost when a session ends without compacting: at exit there is no model turn, so
nothing you'd want to remember can be written *then*. Get ahead of it — write as you work,
not at the end.

As you finish a substantive task that produced durable context — a decision and *why*, a
non-obvious constraint you discovered, in-flight state a fresh session would need — stage
it immediately:

```
cohort working-note "Chose X over Y because Z; migration must run before the code change."
```

These are **disposable working notes** (git-ignored scratch in `.cohort/state/working-memory/`),
cheap to write — so err toward writing. They survive an abrupt exit precisely because they
are written *during* the turn, not deferred to exit. Don't stage trivia, routine edits, or
anything already in durable memory.

At the next compaction or session start, Cohort surfaces the staged notes (yours plus the
mechanical per-turn records the `Stop` backstop captured) and asks you to promote the
durable ones into your persistent memory directory and clear the rest. Curation happens at
that boundary — which is exactly why writing freely now is safe. This is the mid-session
companion to the compaction memory circuit, not a replacement for `cohort snapshot`
(the richer, human-authored, repo-shared record).

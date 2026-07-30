---
name: autonomy-recall
kind: hook
scope: global
description: On session start, print the current supervision level so the coordinator honors it — with the reminder that the safety floor is not on the dial.
targets: [claude]
event: session_start
action: cohort autonomy-recall
---
The supervision dial only affects behavior if the coordinator knows the current level. This
`session_start` hook runs `cohort autonomy-recall`, which prints the machine-local level
(`paired` / `guided` / `supervised` / `autopilot`) and its one-line meaning into the fresh
session, plus the standing reminder that the level tunes *friction over cheaply-reversible
steps only* — it never lowers the fixed floor (the human PR-merge gate, the code
egress/secret/footprint gates, verification of every foreign diff, the operational
hard-limits), and confirm-for-irreversible/outward/destructive stays stop-and-ask at every
level.

Print-only; writes nothing (the level is set out-of-band with `cohort autonomy <level>`,
stored machine-locally so a pull can never raise it). Pairs with the `autonomy-levels`
memory, which carries the full spectrum and floor. Fail-closed: an unreadable level reads
as `paired`.

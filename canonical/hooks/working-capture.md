---
name: working-capture
kind: hook
scope: global
description: After each turn, stage a mechanical working-memory record when the turn changed the tree — the deterministic backstop under the model-authored working notes.
targets: [claude]
event: stop
action: cohort working-capture
---
The deterministic half of working memory. `Stop` fires after each assistant turn, and
`cohort working-capture` stages a small mechanical record (branch, change summary) into
the git-ignored `.cohort/state/working-memory/` staging dir — but only when the turn
actually changed the tree, and never twice for the same unchanged state (a hash marker
dedupes). It is the backstop beneath the model-authored `cohort working-note` calls: even
if the model doesn't write a semantic note, *what changed* is captured per task, so an
abrupt exit before `SessionEnd` loses nothing.

Governed by the same `auto_capture` opt-out as `session-capture`; a silent no-op outside a
Cohort repo or when a repo sets `auto_capture = false`. Never blocks or fails the turn.
The staged records are surfaced for consolidation into durable memory at the next
compaction or session start (`compact-recall` / `session-recall`), then cleared.

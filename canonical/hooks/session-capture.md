---
name: session-capture
kind: hook
scope: global
description: Capture a minimal session record at session end (on by default; opt out per repo).
targets: [claude]
event: session_end
action: cohort session-capture
---
On session end, write a small machine-generated session record (timestamp, branch,
change summary) into the repo's `.cohort/sessions/` — the observation fuel for the
improvement loop (`weekly-report`, `propose-improvement`) and the next session's
`session-recall`. On by default so exit context is never lost silently; a repo opts out
by setting `auto_capture = false` in `.cohort/cohort.toml`. Never blocks or fails the
session; `cohort snapshot` remains the richer, human-authored entry.

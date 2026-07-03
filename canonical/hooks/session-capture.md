---
name: session-capture
kind: hook
scope: global
description: Capture a minimal session record at session end (opt-in per repo).
targets: [claude]
event: session_end
action: cohort session-capture
---
On session end, write a small machine-generated session record (timestamp, branch,
change summary) into the repo's `.cohort/sessions/` — the observation fuel for the
improvement loop (`weekly-report`, `propose-improvement`). Strictly opt-in per repo:
a silent no-op unless `.cohort/cohort.toml` sets `auto_capture = true`. Never blocks
or fails the session; `cohort snapshot` remains the richer, human-authored entry.

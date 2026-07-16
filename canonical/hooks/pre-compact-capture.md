---
name: pre-compact-capture
kind: hook
scope: global
description: Write the mechanical session record before compaction squeezes the context (opt-in per repo).
targets: [claude]
event: pre_compact
action: cohort session-capture
---
The deterministic backstop to `post-compact-memory`: before compaction runs, write
the same minimal machine-generated session record `session-capture` writes at session
end (timestamp, branch, change summary) into the repo's `.cohort/sessions/` — so a
before-the-squeeze record exists even if the post-compaction memory commit never
happens. Same rules as session end: strictly opt-in per repo (a silent no-op unless
`.cohort/cohort.toml` sets `auto_capture = true`), and never blocks or fails anything.

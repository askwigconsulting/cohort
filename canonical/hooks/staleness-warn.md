---
name: staleness-warn
kind: hook
scope: global
description: Warn at session start when the project context is stale.
targets: [claude]
event: session_start
action: cohort staleness-check
---
On session start, check the current repo's Cohort context freshness and, if the
newest session/context activity is older than the configured threshold, print a
non-blocking warning suggesting `cohort snapshot`. Read-only; never edits the
working tree; throttled to once per UTC day per machine.

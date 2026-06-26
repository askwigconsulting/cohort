---
name: update-check
kind: hook
scope: global
description: Notify at session start when a newer Cohort is available.
targets: [all]
event: session_start
action: cohort update-check
---
On session start, check (throttled to once per UTC day per machine) whether the
local Cohort clone is behind its upstream and, if so, print a one-line non-blocking
advisory suggesting `/update`. Read-only: a quiet, non-interactive `git fetch`
updates only remote-tracking refs — never the working tree, never a merge. Exits 0
always; degrades silently when offline, on a diverged/detached checkout, or when the
source repo cannot be resolved.

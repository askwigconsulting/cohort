---
name: session-stamp
kind: hook
scope: global
description: Stamp or refresh the project context once per day at session start.
targets: [all]
event: session_start
action: cohort snapshot --stamp
---
On the first session of the day, stamp the project context with a dated entry.

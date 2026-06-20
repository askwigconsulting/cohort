---
name: stale-warn
kind: hook
scope: global
description: Warn at session start when the project context is stale.
targets: [claude]
event: on_stale
action: python3 "$HOME/.cohort/bin/stale_check.py"
---
At session start, check project-context freshness and warn if past threshold.

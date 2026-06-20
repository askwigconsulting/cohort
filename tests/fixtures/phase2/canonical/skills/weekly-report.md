---
name: weekly-report
kind: skill
scope: global
description: Assemble a dated weekly report from session logs and git history.
targets: [claude]
triggers: [weekly-report, status update]
---
Read the session store and git history for the period, then emit a dated
markdown report grouped by theme.

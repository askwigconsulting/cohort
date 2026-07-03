---
name: feedback
kind: command
scope: global
description: Rate an office agent or command from inside the IDE (fuels the improvement loop).
targets: [claude, cursor]
invocation: feedback
args:
  - name: rating
    required: false
    description: up or down
  - name: subject
    required: false
    description: the agent or command being rated
dry_run: true
---
Record a piece of feedback about the office — the raw signal `cohort propose-improvement`
aggregates.

Work out from the user's words (or ask) whether the rating is `up` or `down` and which
agent or command it is about, then run:

    cohort feedback --rating <up|down> [--agent <name> | --command <name>] [--note "<short note>"]

Keep the note short and factual — one sentence on what worked or what was missing. Notes
may later be summarized into improvement proposals; do not put secrets, credentials, or
personal data in them. Run it from inside the repo (feedback is recorded per project in
`.cohort/feedback/`); if the repo is not a Cohort project yet, suggest `cohort init`.

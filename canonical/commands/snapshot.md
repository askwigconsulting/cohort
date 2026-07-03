---
name: snapshot
kind: command
scope: global
description: Capture a session snapshot for this repo's Cohort context, filled from this conversation.
targets: [claude, cursor]
invocation: snapshot
dry_run: true
---
Capture a dated session record into this repo's `.cohort/sessions/` — the shared context
teammates and future sessions read.

Run `cohort snapshot` (from inside the repo; it prints the file it created under
`.cohort/sessions/`). The file contains a captured change summary plus placeholder
sections. Then — this is the point of running it from the IDE — open that file and
replace the placeholders with real content from this session:

- **Decisions**: what was decided and why, in one or two sentences each.
- **Open items**: what is unfinished or blocked, concretely enough to resume from.

Keep entries factual and free of secrets. If the repo is not a Cohort project yet,
suggest `cohort init` first.

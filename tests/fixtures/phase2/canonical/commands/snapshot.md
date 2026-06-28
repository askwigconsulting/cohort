---
name: snapshot
kind: command
scope: global
description: Write a dated session snapshot to the project context.
targets: [claude, codex, cursor]
invocation: snapshot
dry_run: true
args:
  - name: note
    required: false
    description: An optional note to attach to the snapshot.
---
Capture changed files, decisions, and open items into a dated Session Log entry.
Attach $ARGUMENTS as a note if provided.

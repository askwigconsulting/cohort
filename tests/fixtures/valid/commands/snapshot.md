---
name: snapshot
kind: command
scope: project
description: Write a dated session snapshot to the project context.
targets: [claude, codex, cursor]
invocation: snapshot
dry_run: true
---
Capture changed files, decisions, and open items into a dated Session Log entry.

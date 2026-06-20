---
name: command-no-dryrun
kind: command
scope: project
description: A command that wrongly disables dry_run.
targets: [claude]
invocation: dangerous-thing
dry_run: false
---
This must fail with E060: commands must not set dry_run false.

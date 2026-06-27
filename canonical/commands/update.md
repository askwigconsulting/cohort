---
name: update
kind: command
scope: global
description: Update Cohort to the latest upstream version and recompile your IDEs.
targets: [claude, cursor]
invocation: update
dry_run: true
---
Update this Cohort install to the latest upstream version.

First run `cohort update --dry-run` to preview the incoming commits and changed
artifacts without touching anything. Then run `cohort update` to fast-forward the
clone, reinstall the package if its dependencies changed, and recompile every
installed IDE.

Cohort refuses to update a dirty or diverged working tree — commit, stash, or
reconcile first. Updates are never silent: nothing changes until you run the
command, and only a clean fast-forward is ever applied.

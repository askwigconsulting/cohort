---
name: non-advisory-agent
kind: agent
scope: global
description: An agent that wrongly disables the advisory safety invariant.
targets: [all]
department: Security
advisory: false
---
This must fail validation: agents must be advisory.

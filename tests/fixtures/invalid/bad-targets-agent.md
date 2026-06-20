---
name: bad-targets-agent
kind: agent
scope: global
description: An agent that mixes 'all' with another target.
targets: [all, claude]
department: People
advisory: true
---
This must fail: 'all' must be the only target when present.

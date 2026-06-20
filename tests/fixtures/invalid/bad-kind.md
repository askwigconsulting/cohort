---
name: bad-kind
kind: wizard
scope: global
description: Declares a kind outside the allowed enum.
targets: [all]
---
This must fail with E020 for the unknown kind; no per-kind noise.

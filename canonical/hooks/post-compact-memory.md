---
name: post-compact-memory
kind: hook
scope: global
description: Right after compaction, instruct the model to commit the session's critical parts to durable memory.
targets: [claude]
event: post_compact
action: cohort compact-recall
---
Compaction is harness-side — the model gets no turn between "context full" and
"summary replaces context," so nothing model-authored can be saved *at* that moment.
This hook closes the gap from the other side: it fires immediately after compaction
(SessionStart, source=compact) and injects a standing instruction into the fresh
context — commit the critical parts of the pre-compaction session to durable memory
now (decisions and rationale, in-flight work state, unresolved questions, user
directives) before resuming the task. Print-only; the hook itself writes nothing.
Pairs with `pre-compact-capture`, the deterministic before-the-squeeze record.

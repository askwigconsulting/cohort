---
description: Metric-gated autonomous optimization loop — propose, evaluate, keep the gain or revert, in a throwaway worktree; you review the staircase and merge
argument-hint: '[objective]'
---

Point it at a number and let it climb. `/ratchet` is a **metric-gated optimization loop** —
Karpathy's AutoResearch pattern (propose a change, run a fixed-budget evaluator, keep the
commit if the metric improved, `git reset` if not, repeat) adapted to Cohort's human gate:
the whole climb runs **inside a throwaway git worktree**, bounded by a budget, and you
review the *staircase* and merge via PR. The autonomy is the inner loop; the merge stays
gated. Reach for it when the win is measurable and the search is tedious — a perf number, a
benchmark score, a failing-test count, a lint count.

Runs on a **coordinator tier** (Fable or Opus): you set up the contract and read the
staircase; the loop does the methodical climbing.

## The three-part contract

Make these three things explicit before you start — it is what makes autonomy safe:

- **The immutable evaluator** — one command that prints the objective number, and that the
  loop never edits. This is the ground truth (Karpathy's `prepare.py`). If the doer could
  change the evaluator, it could optimize the metric by lying; it can't, because it only
  ever touches the worktree's tracked code, which you review.
- **The sandbox** — a detached worktree off HEAD. Every proposal lands only here; your
  working tree is never touched, and a bad run is thrown away.
- **The direction** — the objective in words. Keep it tight and surgical ("lower p99
  latency in `handler.py`; change nothing else").

## Run it

```
cohort engine ratchet gpt \
  --evaluator "pytest tests/bench.py -q 2>&1 | tail -1" \
  --metric-regex 'score=([0-9.]+)' \
  --goal maximize --budget 15 --footprint src/handler.py
```

- `gpt` (Codex, edits under its own sandbox) or `grok` (egress-gated agentic patch) does
  the proposing; the loop's keep/revert, worktree, ledger, and budget are enforced in code.
- Each iteration is fed the current best and the recent ledger so it calibrates what to try
  next — the `ratchet-results.tsv` staircase is the loop's memory.
- Ties and non-improvements revert. The lineage only advances on a real gain.

## Then you gate it

When the budget is spent, read the staircase and the accumulated diff in the worktree.
Verify the gain is real (not a metric artifact), then merge via PR — the same human gate as
every other change. Nothing was committed to your branch and your working tree is unchanged.

## Guardrails

- **The evaluator is trusted code you supply.** The *doer* is gated (Codex sandbox / Grok
  egress+secret+footprint gates); the evaluator is your own test/benchmark, run in the
  worktree.
- **Bounded by construction** — a hard iteration budget, a per-evaluation timeout, and the
  worktree wall. A runaway or prompt-injected proposal harms only a disposable worktree.
- **Verify the win, don't trust the number.** A metric that jumped because the change broke
  the evaluator is a revert, not a keep — that's why *you* review the staircase before merge.

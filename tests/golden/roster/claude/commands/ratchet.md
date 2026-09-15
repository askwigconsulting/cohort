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
  loop never edits. This is the ground truth (Karpathy's `prepare.py`). The *command* is
  yours; the *code it executes* is the engine's — it runs in the worktree the engine just
  wrote to, so it is confined like a doer (see Guardrails). If the doer could change the
  evaluator, it could optimize the metric by lying; it can only touch the worktree's
  tracked code, which you review — so a metric that jumped because the change broke or
  gamed the evaluator is yours to catch at the staircase.
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
- The evaluator runs with a **scrubbed environment** and, on Linux with bubblewrap, **no
  network and no access to your home** — so put any variable it needs *inside* the
  command (`FOO=1 pytest …`) and make sure the tools it calls are system-installed or live
  in the worktree; a venv under your home directory is invisible inside the jail.
- Each iteration is fed the current best and the recent ledger so it calibrates what to try
  next — the `ratchet-results.tsv` staircase is the loop's memory.
- Ties and non-improvements revert. The lineage only advances on a real gain.

## Then you gate it

When the budget is spent, read the staircase and the accumulated diff in the worktree.
Verify the gain is real (not a metric artifact), then merge via PR — the same human gate as
every other change. Nothing was committed to your branch and your working tree is unchanged.

## Guardrails

- **The evaluator executes engine-written code, and is confined accordingly.** Your
  command is trusted; what it runs is not — a `conftest.py` or an import the engine
  planted in the worktree runs the moment the evaluator does. This is the one place an
  external engine's output is *executed* rather than reviewed, so the evaluator runs like
  a doer: a scrubbed environment (no host secrets, an ephemeral `HOME`), its own process
  group killed whole on timeout, and on Linux with bubblewrap the same kernel jail as the
  grok doer with the network **unshared** — it can write only the worktree, read only
  `/usr` and the worktree, and reach nothing. Without bubblewrap (macOS, Windows) only the
  environment scrub and the group kill apply: the evaluator can still write outside the
  worktree and reach the network. Install bubblewrap to close that.
- **The doer is gated.** Codex runs under its own sandbox and, each iteration, the
  worktree it will read is wire-capped and secret-scanned before dispatch; Grok's patch
  passes the egress, secret, and footprint gates. The repo's egress opt-out is read from
  `.cohort/project_context.md` whether or not the caller passes context.
- **Bounded by construction** — a hard iteration budget, a per-evaluation timeout, and the
  worktree wall; the loop's own git calls are non-interactive, bounded, and never sign,
  so a hung git fails the iteration, not the loop. Inside the jail a runaway or
  prompt-injected proposal harms only a disposable worktree.
- **Verify the win, don't trust the number.** A metric that jumped because the change broke
  the evaluator is a revert, not a keep — that's why *you* review the staircase before merge.

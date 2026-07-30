---
description: Recurring deep adversarial audit of the whole application — rotates dimensions and subsystems, keeps a coverage ledger, and sweeps the project's critical path every run
argument-hint: '[dimension]'
---

`/audit` is the **recurring** whole-application adversarial sweep — the recurring sibling of
`/scout`. `/scout` reviews a *target* you name; `/audit` reviews *the application you did
not name*, on a rotation, and remembers what it already covered.

Run it from a coordinator tier (Fable preferred, Opus acceptable). Reviewers are
**advisory and read-only** — they find, the coordinator verifies, the human decides.

## Why a rotation, not a full sweep

A whole-codebase audit every week is either shallow or unaffordable. So `/audit` **rotates**:
each run takes a slice of dimensions × subsystems, records what it covered, and picks the
staler half next time. Two rules make that sound:

1. **The critical path is swept every run**, never rotated out. Each project declares its
   critical path in the ledger — the highest-stakes, hardest-to-reverse path: whatever can
   **move money, write production data, grant or revoke access, deploy, or take an
   irreversible or outward action**. If a project hasn't named its critical path yet, naming
   it is the first run's job. (For a trading app it's the order path; for a CMS, the
   publish/permission path; for a harness like Cohort, the install/merge/egress path.)
2. **Nothing else goes more than ~4 runs without a look.** The ledger is what enforces
   this; without it a rotation silently becomes "the same three areas forever".

## 1. Read the ledger first

Read `docs/audit/ledger.md` (create it on the first run). It records, per dimension ×
subsystem: last audited, findings raised, findings that turned out false, and the project's
declared critical path. Pick this run's slice from the stalest entries — plus the critical
path, always. If the caller named a dimension, audit that and still sweep the critical path.

## 2. The dimensions

Distinct on purpose — each finds a class of defect the others structurally miss. **This
list is a floor, not a ceiling**; if a reviewer finds a class not listed (or one specific to
this domain), add it to the ledger as a new dimension.

| Dimension | What it hunts | The tell |
|---|---|---|
| **critical-path** | Anything that can reach, trigger, size, or gate a high-stakes action — a payment, a production write, an access grant, a deploy, an irreversible/outward call. Caps, gates, approvals, kill switches, idempotency, the clients that talk to money/prod/third parties | A bound that holds by convention rather than construction |
| **security** | Authz gaps, tenant isolation, secret handling and rotation, SSRF, injection, path traversal, credential custody, dependency CVEs | A check that runs on declared input rather than verified state |
| **correctness** | Numeric/Decimal discipline, rounding direction, off-by-one, migration safety, NULL-vs-zero semantics, time/timezone/DST boundaries, encoding | A fabricated default standing in for absent data |
| **concurrency** | Races, deadlocks, lock ordering, TOCTOU, non-atomic read-modify-write, lost updates, ordering assumptions across async or distributed work | A read-modify-write with no lock or transaction around it |
| **resilience** | Missing timeouts, retries without backoff or idempotency, unhandled partial failure, no graceful degradation, resource exhaustion under load or error | An external call with no timeout on a path a user waits on |
| **dead-ends** | Unreachable routes, unused endpoints, dead config keys, orphaned components, duplicated logic, commented-out blocks | Code that greps as present and executes never |
| **honesty** | UI claiming what the backend cannot prove; **placebo controls** (a setting nothing reads); silent failure; optimistic states; a chart implying a conclusion the data does not support | A control whose value is never read by any consumer |
| **performance** | Full-table aggregates in polled paths, N+1, unbounded queries, missing indexes, hot-path writes, cost per request | An aggregate whose cost grows with the table in a path that polls |
| **naming** | The same concept under different nouns across layers; the same noun meaning different things; vocabulary drift between UI, API and domain | One idea with two names, or two ideas with one |
| **docs** | **Comments that assert something false**, stale runbooks, load-bearing decisions with no recorded rationale, drift between docs and behaviour | A comment claiming parity, invariance or coverage that no test pins |
| **tests** | Tests that pass for the wrong reason, time- or order-dependent flakes, critical-path coverage gaps, assertions that survive mutation | A test still green after you break the thing it names |
| **supply-chain** | Unpinned or floating dependency versions, lockfile drift or absence, unmaintained/abandoned deps, license incompatibility, transitive CVEs, unverified install/post-install scripts | A dependency that can change under you between two builds |
| **accessibility** | Keyboard-unreachable controls, missing screen-reader labels/semantics, contrast below threshold, focus traps, motion without a reduced-motion path | An interactive element a keyboard alone cannot operate |
| **ops** | Alerting that reaches nobody, unenforced entitlements, config vs reality drift, untested restore, capacity a live loop depends on | A control configured but never observed working |

**Weight actual defects highest**, then risks, then improvements. "Consider scalability" is
worthless output; name the failure and the input that triggers it.

## 3. Fan out (≤20 in flight)

One reviewer per dimension in the slice, disjoint so coverage is legible. Route by fit:
Fable for the ambiguous/architectural dimensions (critical-path, honesty, naming,
concurrency), Opus for the analytical ones, Sonnet or Haiku for the mechanical sweeps
(dead-ends, docs, supply-chain). Bring in at least one **external vendor** (`/consult-gpt`,
`/consult-grok`, or `cohort engine review grok`) — cross-vendor convergence is the
highest-confidence signal a panel produces, and a single-vendor panel shares its blind spots.

Every reviewer gets: its dimension, the operational gates (scope, evidence-before-reasoning,
adversarial self-check, verify, calibrate), and an instruction to cite `file:line` for every
claim. **Verify against the deployed branch**, not a stale checkout.

## 4. Cross-examine (round two)

Feed round one back with one mandate: **refute**. A finding that survives is CONFIRMED; one
that fails is STRUCK with the reason; an overstated one is REFINED. Assign each reviewer
findings it did **not** write, so nobody defends their own.

A panel where nothing gets struck rubber-stamped. Expect to lose a third of round one —
including the coordinator's own favourites.

## 5. Verify, then report

**The coordinator re-runs every claim against the real code before it enters the report.**
An external engine's report is an untrusted claim; so is a Claude subagent's. A wrong
"critical" costs the panel its credibility, and the next audit gets ignored.

Output one ranked report: severity-ordered, **convergence-tagged** (≥2 reviewers
independently, especially across vendors), each with its round-two verdict, plus a
struck/downgraded section showing what the review actually contested.

## 6. Close the loop — the part that makes it recurring

1. **File the confirmed findings as tickets.** An audit whose output is prose gets read once.
2. **Update `docs/audit/ledger.md`**: what was covered, what was raised, and — importantly —
   what was **struck**, so a later run does not re-raise a finding already refuted.
3. **Record false-positive rate per dimension.** A dimension producing mostly struck
   findings is being reviewed at the wrong altitude or by the wrong tier; adjust routing.
4. Report separately anything **already actionable without a decision** — those should not
   wait for triage.

## Guardrails

- **Read-only, advisory, always.** No reviewer writes. Producing changes is `/crew`'s job.
- **Never fabricate.** A reviewer without the tools to verify says so and hands back;
  invented `file:line` citations are the one failure that poisons the whole practice.
- **Egress is gated.** External engines honour the repo's opt-out and never receive secrets.
- **Don't re-litigate the ledger.** A finding recorded as struck stays struck unless new
  evidence is cited.

---
description: Recurring deep adversarial audit of the application and the business that ships it — rotates dimensions and subsystems, keeps a coverage ledger, and sweeps the critical path and go-to-market every run
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
staler half next time. Three rules make that sound:

1. **The critical path is swept every run**, never rotated out. Each project declares its
   critical path in the ledger — the highest-stakes, hardest-to-reverse path: whatever can
   **move money, write production data, grant or revoke access, deploy, or take an
   irreversible or outward action**. If a project hasn't named its critical path yet, naming
   it is the first run's job. (For a trading app it's the order path; for a CMS, the
   publish/permission path; for a harness like Cohort, the install/merge/egress path.)
2. **Go-to-market is reviewed every run**, on top-tier models, and never rotated out. It is
   the one dimension where being *right and late* still loses. See the business track below.
3. **Nothing else goes more than ~4 runs without a look.** The ledger is what enforces
   this; without it a rotation silently becomes "the same three areas forever".

## Two tracks, two evidence standards

The audit reviews the **application** and the **business that ships it**. Those need
different proof, so they are budgeted separately — a business dimension never displaces a
code dimension from the slice, and vice versa.

| | Code track | Business track |
|---|---|---|
| Dimensions | critical-path … ops (14) | go-to-market, business-ops |
| Evidence | `file:line` in the deployed branch | a named artifact, or the **documented absence** of one |
| A finding is | a defect with a trigger | an unmet obligation, an untested assumption, or a missing artifact with a deadline |
| Always-on | critical-path | go-to-market |

**"Absence" is the business track's core finding shape.** "No DPA template exists" and "no
one owns the state filing" are the real defects, and they have no line number. A business
reviewer must state what it searched (repo, `docs/`, the ledger, the issue tracker) and
found nothing — an absence claimed without a search is as bad as an invented citation.

## 1. Read the ledger first

Read `docs/audit/ledger.md` (create it on the first run). It records, per dimension ×
subsystem: last audited, findings raised, findings that turned out false, and the project's
declared critical path. Pick this run's slice from the stalest entries — plus the critical
path, go-to-market and vendor-reachability, always. If the caller named a dimension, audit
that and still sweep all three always-on dimensions.

The ledger also declares the **business context**, without which the business track is
guesswork. Naming it is the first run's job, same as the critical path:

- **Stage** — pre-launch, private beta, paid GA. Determines which obligations are live now
  versus scheduled.
- **Jurisdictions** — where users are, where the entity is. Drives which privacy regimes
  and filings apply.
- **Entity and filings** — legal form, registered agent, and the recurring calendar
  (annual report, franchise tax, registrations) with **who owns each**.
- **Personal data held** — the actual categories, per store. This is the input to the
  privacy dimension, and writing it down is usually when someone discovers a category
  nobody knew was retained.
- **Regulatory posture** — the claim the product does *not* make (e.g. "not a
  broker-dealer", "not custodial", "not investment advice") and what in the code or terms
  keeps that true. A posture with nothing enforcing it is the highest-value business finding
  there is.

## 2. The dimensions

Distinct on purpose — each finds a class of defect the others structurally miss. **This
list is a floor, not a ceiling**; if a reviewer finds a class not listed (or one specific to
this domain), add it to the ledger as a new dimension.

| Dimension | What it hunts | The tell |
|---|---|---|
| **critical-path** | Anything that can reach, trigger, size, or gate a high-stakes action — a payment, a production write, an access grant, a deploy, an irreversible/outward call. Caps, gates, approvals, kill switches, idempotency, the clients that talk to money/prod/third parties | A bound that holds by convention rather than construction |
| **security** | Authz gaps, tenant isolation, secret handling and rotation, SSRF, injection, path traversal, credential custody, dependency CVEs | A check that runs on declared input rather than verified state |
| **privacy** | Whether the **rights the law grants are actually executable in code**: access/export, deletion/erasure, correction, portability, do-not-sell/share, marketing and contact opt-out, consent capture and withdrawal. Plus retention limits, minimization, sub-processor egress, and children's/sensitive-category data | A right the terms promise that no endpoint, job or runbook can perform end to end |
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
| **vendor-reachability** *(always on)* | Whether real work still reaches **each** vendor **right now**: every doer and reviewer path actually dispatched end to end, CLI-first→API-fallback holding, the announced channel matching the one that answered, and every model id in the registry resolving to itself | A vendor path that returns success with an empty body |

**Weight actual defects highest**, then risks, then improvements. "Consider scalability" is
worthless output; name the failure and the input that triggers it.

### vendor-reachability is dispatched, never reasoned about

This is the one dimension a reviewer **cannot** answer by reading code, and the only
acceptable evidence is a **command that ran and the output it produced**. Reading the
dispatch path and concluding "grok works" is exactly the failure mode: on 2026-07-31 the
code was correct, the gates passed, and grok was unreachable anyway — the sandbox could not
execute a CLI installed under `~/.local`, and the CLI itself was rejected by the vendor's
current API. Neither is visible in the source.

Each run, for **every** vendor and **both** transports:

- **Dispatch a real task** and quote the answer. A round-trip that returns nothing is a
  failure even when the exit code is 0.
- **Name the channel that actually answered** and check it matches what was announced. A
  silent downgrade to a weaker transport invalidates every finding routed through it.
- **Resolve every registered model id** against the live endpoint and confirm it serves
  back its own name. Aliases drift, and a tier that quietly resolves elsewhere means the
  rotation below is measuring the wrong model.
- **Report unreachable vendors in the audit itself.** A run that lost a vendor is a
  *narrower* run, and the report must say so — never quietly redistribute its share.

**Never present a single-vendor result as cross-vendor.** If a vendor could not be reached,
say which, say why, and state what that leaves unreviewed. A fabricated cross-vendor
convergence is worse than an admitted gap.

### The privacy dimension has a trap worth naming

**Deletion and retention are frequently in direct conflict**, and the conflict is the
finding. A regulated product can be *required* to retain transaction records for years while
a user has an absolute-sounding right to erasure. The correct answer is a documented
lawful-basis exemption with a defined scope — not a delete that quietly skips tables, and
not a refusal to delete anything. So the reviewer's job is to determine which of three
states the product is in:

1. Deletion is complete and the retention exemption is documented and scoped. ✅
2. Deletion runs but silently leaves personal data behind, while the terms promise erasure.
   **This is the defect** — it is a false promise, and it is the common case.
3. There is no deletion path at all, and the terms promise one.

State 2 hides behind a `DELETE` that looks like it works. The test is to enumerate every
store holding personal data (from the ledger's declared categories) and ask which the
deletion path provably reaches — including backups, logs, analytics, caches, external
sub-processors, and any append-only or immutable ledger. **An immutable audit trail
containing personal data is the single most common unsolved case.**

The same enumeration applies to do-not-market: a suppression flag is worthless if any
sender can read the address without consulting it. The tell is a send path that queries
users directly rather than a view that has suppression built in.

## 2b. The business track

| Dimension | What it hunts | The tell |
|---|---|---|
| **go-to-market** | The path from "it works" to "someone pays for it": positioning and who it is *for*, the wedge, pricing and packaging against the cost to serve, activation and the first-run funnel, the top acquisition channel, competitive shifts since last run, and the **stated assumption that has never been tested against a real prospect**. Reviewed **every run, top tier.** | A strategy whose success depends on an assumption no one has tried to falsify |
| **business-ops** | Legal, compliance, filings, purchasing. Entity standing and the filing calendar with owners; contracts (terms, privacy notice, DPAs, sub-processor list) and whether they match what the code does; regulatory posture and licensing exposure; insurance; IP assignment; vendor spend, renewal dates, auto-renew traps, single-vendor dependency, and per-seat/per-token cost growth against revenue | An obligation with a deadline and no owner |

Business-track findings still need a **concrete consequence**, not a vibe: which obligation,
which deadline, what happens on breach, and what it would cost to fix now versus later.
"Improve positioning" is as worthless as "consider scalability".

**The hard boundary on legal and regulatory work:** reviewers **identify and frame**
questions, gather what the codebase and public sources actually say, and flag exposure. They
**never issue a legal conclusion, a licensing determination, or a compliance sign-off** —
and never imply the audit constitutes one. Every such finding is tagged
**REQUIRES PROFESSIONAL OPINION**, states the specific question to put to counsel, and says
what it would cost to be wrong. Confident-sounding fabricated regulatory advice is the one
output of this track that could do real damage, and it is strictly worse than silence.
`counsel`, `compliance`, `privacy-officer`, `procurement` and `finance-analyst` specialists
are the right reviewers here — all advisory by construction — routed via `chief-of-staff`
when a finding spans functions.

## 3. Fan out (≤20 in flight)

One reviewer per dimension in the slice, disjoint so coverage is legible. Route by fit:
Fable for the ambiguous/architectural dimensions (critical-path, honesty, naming,
concurrency), Opus for the analytical ones, Sonnet or Haiku for the mechanical sweeps
(dead-ends, docs, supply-chain). Bring in at least one **external vendor** (`/consult-gpt`,
`/consult-grok`, or `cohort engine review grok`) — cross-vendor convergence is the
highest-confidence signal a panel produces, and a single-vendor panel shares its blind spots.

**The business track routes top tier, always.** Go-to-market and business-ops are judgment
under ambiguity with no compiler to catch a wrong answer, so they get Fable and the flagship
external models — never a cheap tier, even when the rest of the slice is mechanical. Run
go-to-market on **at least two vendors every run**: strategy is exactly where a single
vendor's priors go unchallenged, and disagreement between two flagships is more informative
than either one's confident answer. Give business-track reviewers **web search** — a
competitive or regulatory review against a stale training cutoff is worse than none, and
`privacy` and `business-ops` both turn on what is true *now*.

Every reviewer gets: its dimension, the operational gates (scope, evidence-before-reasoning,
adversarial self-check, verify, calibrate), and its track's evidence standard — `file:line`
for the code track, a named artifact or a **searched-and-documented absence** for the
business track. **Verify against the deployed branch**, not a stale checkout.

### Rotate the tier across runs (the sine wave)

Routing by fit alone is stable, and stability is the problem: a dimension always reviewed by
one tier permanently inherits that tier's blind spots. So the *phase* advances each run —
read the last phase from the ledger and move to the next:

| Phase | Routing | What it is for |
|---|---|---|
| **balanced** | mixed tiers, by fit as above | the default read |
| **complex-heavy** | Fable/Opus across the board, including the mechanical sweeps | subtlety a cheap tier glosses |
| **simple-heavy** | Sonnet/Haiku wherever the floor allows | mechanical defects a reasoning model reads past |

Then `balanced` again. Within a phase, prefer a tier that has **not** reviewed this
dimension recently — the aim is that over several runs every dimension has been seen by
every tier, because the *union* of their perspectives is what hardens a finding.

Two rules the phase never overrides. **The always-on dimensions keep their quality floor** —
critical-path, go-to-market and vendor-reachability never drop below a capable tier just
because it is a simple-heavy run. And a **simple-heavy run is not a cheap run**: it is an
experiment in what a mechanical reader catches, so it still cross-examines and still
verifies.

Record the tier per dimension in the ledger. That makes the existing per-dimension
false-positive rate **tier-aware**: a dimension producing mostly-struck findings under one
tier is a candidate for re-run under another, and over time the ledger shows which tier
actually finds signal where.

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
5. **Business-track output is routed, not just filed.** A go-to-market finding is a decision
   for the owner, and a business-ops finding with a deadline needs a named owner and that
   date — a ticket with neither is how a filing gets missed. Anything tagged REQUIRES
   PROFESSIONAL OPINION is listed separately as a question for counsel, never as a task an
   engineer can close.
6. **Keep the business context current.** Stage, jurisdictions, data categories and the
   filing calendar all drift; re-confirm them each run, because every business finding is
   derived from them and a stale premise invalidates the lot.

## Guardrails

- **Read-only, advisory, always.** No reviewer writes. Producing changes is `/crew`'s job.
- **Never fabricate.** A reviewer without the tools to verify says so and hands back;
  invented `file:line` citations are the one failure that poisons the whole practice. On the
  business track the equivalent failure is an unsearched absence or an invented citation of
  law, filing deadline, or vendor term.
- **No legal or compliance conclusions.** The audit frames questions and flags exposure; it
  never determines that something is compliant, lawful, or licensed. Tag every such finding
  **REQUIRES PROFESSIONAL OPINION** with the specific question for counsel.
- **Never touch personal data to test a privacy claim.** Prove deletion and export by
  reading the code and schema, not by running them against real records.
- **Egress is gated.** External engines honour the repo's opt-out and never receive secrets.
- **Don't re-litigate the ledger.** A finding recorded as struck stays struck unless new
  evidence is cited.

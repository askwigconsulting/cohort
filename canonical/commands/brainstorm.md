---
name: brainstorm
kind: command
scope: global
description: Recurring generative sweep for new opportunities and ideas — the creative sibling of /audit. Diverges wide across creative lenses, converges on the strongest, turns survivors into pursuit plans, and keeps an idea ledger so it compounds run over run and never re-generates a dead idea.
targets:
- claude
invocation: brainstorm
args:
- name: focus
  required: false
  description: The opportunity area or open question to brainstorm (e.g. "differentiation vs competitors", "monetization model", "the community flywheel"). Omit to let the rotation pick the stalest open question from the ledger.
dry_run: true
---
`/brainstorm` is the **generative** sibling of `/audit`. `/audit` finds what is *wrong with
what exists*; `/brainstorm` finds *what could exist*. It runs the same machinery — recurring,
on a rotation, cross-vendor, ledger-backed, coordinator-synthesized, and **read-only /
advisory** — but deliberately **inverts** it wherever creativity demands the opposite of
critique.

Run it from a coordinator tier (Fable preferred, Opus acceptable). Reviewers and generators
are **advisory** — they generate and evaluate; the coordinator synthesizes; the human
decides what to pursue. Producing anything real (a spike, a prototype, a research run) is
`/scout`'s or `/crew`'s job, never this command's.

## The one discipline: diverge before you converge

The single failure that kills a brainstorm is letting judgment into the room too early. An
agent asked to generate *and* evaluate in one breath produces three safe, obvious ideas and
self-censors the non-obvious one — which was the entire point. So the phases are strictly
separated and never share a prompt:

1. **Diverge** — generate *quantity*, defer all judgment, chase the non-obvious.
2. **Converge** — only now cluster, score, and red-team; kill the weak.
3. **Pursue** — turn the survivors into the cheapest experiment that could validate or kill
   each one.

And like `/audit`, it **rotates** with a ledger, so it builds cumulatively instead of
re-generating the same five ideas every run. Two rules make that sound:

1. **Every open question gets a look.** The project declares its open questions / opportunity
   areas in the ledger; nothing goes more than a few runs without a brainstorm aimed at it.
2. **A dead idea stays dead.** An idea the convergence step killed is logged **with the
   reason**, so a later run doesn't burn a generator re-proposing it — the generative analog
   of `/audit`'s "don't re-litigate a struck finding." New evidence can revive it; a fresh
   restatement cannot.

## 1. Frame — read the ledger first

Read `docs/brainstorm/ledger.md` (create it on the first run). It records, per focus area:
ideas generated, which were **pursued / parked / killed** (and why), and the project's
declared **opportunity areas** and **decision context** — the goals, constraints,
differentiators-to-beat, and non-negotiables that keep ideation grounded rather than random.
Then read the project's own context (`.cohort/project_context.md` — its purpose, decisions,
and **open questions**; those open questions are the richest fuel there is).

Pick this run's **focus** — one named opportunity area or open question (e.g. *"differentiation
vs the incumbents"*, *"monetization"*, *"the community flywheel that compounds with
adoption"*). If the caller named a focus, use it; otherwise take the stalest area from the
ledger. Naming the decision context is the first run's job, same as `/audit` naming the
critical path — a brainstorm with no stated goal or constraint generates noise.

## 2. Diverge — the creative lenses

One generator per lens, each producing **many** ideas from a genuinely different angle — the
generative analog of `/audit`'s dimensions. The list is a floor, not a ceiling; a lens that
fits this domain and isn't here gets added to the ledger.

| Lens | What it generates | The provocation |
|---|---|---|
| **jobs-to-be-done** | The real job the user hires the product for, and the unmet or badly-served jobs around it | What is the user actually trying to accomplish, and where do they hack around today's tools? |
| **analogy / cross-industry** | Mechanisms proven elsewhere, ported in | Who has *already* solved a shaped-like-this problem in another domain — and what did they do? |
| **inversion / anti-problem** | Features found by solving the opposite | How would we *guarantee* the worst outcome? Every prevention is a feature. |
| **first-principles** | Ideas rebuilt from the core value after stripping every inherited assumption | If we deleted every current assumption, what is the irreducible thing this must do? |
| **constraint-shift** | What opens up when a limit is removed — or added | If budget/tech/data were no object… and, separately, if we could ship exactly *one* feature? |
| **tech-leverage** | What is *newly* possible this year that wasn't | Which new capability (on-device ML, new data source, cheaper compute) unlocks something previously impossible? |
| **moat / network-effect** | Ideas whose value **compounds with adoption** and raises switching costs | Which idea gets *better the more users there are* — and harder for a competitor to copy? |
| **adjacent-expansion** | The next market, segment, or use case the core unlocks | Given the core asset, what adjacent audience or use case is one step away? |
| **segment / persona** | Ideas that fall out of a specific under-served user | Who is badly served by every incumbent, and what would delight *only them*? |
| **timing / trend** | Why *now* — a shift that makes this the right moment | What changed in the market, regulation, or behavior that makes this newly winnable? |

**Rules for a generator:** produce *quantity*; do **not** self-censor on feasibility, cost,
or "we'd never build that" — that is convergence's job, and pre-censoring is how the good
idea dies unsaid. Chase the non-obvious; a run that returns only safe ideas failed. **Ground
any market claim** (a competitor does X, the market is worth Y) as an *assumption to verify*
or cite real research — never assert it as fact.

**Cross-vendor here is for divergence, not convergence.** Bring in at least one other vendor
(`/consult-gpt`, `/consult-grok`, or `cohort engine review grok`) *because* different models
carry different priors and therefore generate genuinely different ideas — the opposite of
`/audit`, where cross-vendor *agreement* is the signal. Here, cross-vendor *disagreement* is
the yield.

## 3. Converge — cluster, score, red-team

Only now does judgment enter. The coordinator (with a convergence panel for a large field):

1. **Cluster & dedup** the raw ideas into themes; a theme that three lenses reached
   independently is a strong signal.
2. **Score** each surviving cluster against a rubric — weight *value* and *differentiation*
   highest:

   | Axis | The question |
   |---|---|
   | **value** | How much real user or market pain does it kill? |
   | **differentiation** | Does it distinguish us from the incumbents, or just match them? |
   | **moat** | Does it compound with adoption / raise switching costs, or is it copyable in a week? |
   | **feasibility** | Technical, data, legal/regulatory — what has to be true to build it? |
   | **evidence-needed** | What is the single assumption we'd have to believe for this to work? |

3. **Red-team the top ideas.** A contrarian challenges each survivor — *why won't this work?
   what would a well-funded incumbent do in response? what is the killer assumption?* This is
   `/audit`'s "refute" round, but aimed at **strengthening**: an idea that survives is
   sharper; one that dies is logged with the reason so it isn't re-generated.

Plot the survivors on **impact × effort** — name the big bets, the quick wins, and the money
pits explicitly, rather than returning an undifferentiated list.

## 4. Pursue — the part that makes it more than a list

For each idea worth keeping, produce a **pursuit plan** — this is the "pursuing" half of the
command, and its most valuable output:

- **The riskiest assumption** the idea rests on (from the rubric's *evidence-needed*).
- **The cheapest experiment** that would validate or kill it — a user interview, a market
  scan, a data-availability check, a throwaway spike, a landing-page test — chosen to buy the
  most certainty per hour.
- **The decision criterion** — what result advances it vs. shelves it.
- **The hand-off** — to `/scout` (market/competitive research), a spike, or `/crew` (a real
  prototype). `/brainstorm` never builds; it points at what to build or learn next.

An idea without a next cheap step is a daydream. An idea with one is a bet you can afford.

## 5. Fan out (≤20 in flight) & route

One generator per lens in the slice, plus the convergence and red-team roles. Route by fit
and cost — the inverse economics of a review panel:

- **Diverge is cheap and wide.** Volume generation runs well on **Sonnet/Haiku** and on the
  external vendors; more lenses beat deeper ones here.
- **Converge and pursue are the judgment steps** — scoring, red-teaming, and the pursuit
  plans go to **Fable/Opus**, the way `/audit` routes its ambiguous dimensions up.
- **Ground the market.** Give the analogy, differentiation, and timing lenses **web search**
  (`/scout`, `/consult-gpt`, `/consult-grok`) — a competitive or trend claim against a stale
  training cutoff is worse than none.

## 6. Close the loop — the part that makes it recurring

1. **File the pursued ideas** as tickets or decisions, each with its riskiest assumption and
   cheapest experiment — an idea whose output is prose gets read once.
2. **Update `docs/brainstorm/ledger.md`:** what focus was swept, what was generated, and —
   importantly — what was **pursued / parked / killed and why**, so a later run compounds
   instead of repeating.
3. **Keep the decision context current.** Goals, constraints, and the competitive landscape
   drift; re-confirm them each run, because every idea is scored against them and a stale
   premise mis-ranks the lot.
4. Surface separately anything **already actionable without a decision** — those shouldn't
   wait for triage.

## Guardrails

- **Read-only, advisory, always.** No generator builds anything. Producing a spike, a
  prototype, or research is `/scout`'s or `/crew`'s job.
- **Diverge before you converge.** Never let the scoring panel see the generation prompt, and
  never ask one agent to both generate and judge — it is the one rule that, broken, wastes
  the whole run.
- **Never fabricate the market.** An invented competitor feature, made-up market size, or
  asserted user behavior is the brainstorm equivalent of `/audit`'s invented `file:line` — it
  poisons every downstream score. Flag it as an assumption to verify, or research it.
- **Quantity has a floor.** Three safe ideas is a failed diverge. The non-obvious idea is the
  reason the command exists — push past the obvious ones.
- **Egress is gated.** External engines honour the repo's opt-out and never receive secrets.
- **Don't re-litigate the ledger.** A killed idea stays killed unless *new evidence* is
  cited — not a fresh restatement.

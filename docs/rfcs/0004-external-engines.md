# RFC 0004 — External engines: non-Claude models as orchestrated doers

- Status: **Draft** (Opus draft 2026-07-17, for Fable pressure-test + office security/privacy review before acceptance)
- Author: Cohort maintainers
- Created: 2026-07-17
- Depends on: `/orchestrate` (the coordinator protocol — delivered), `/consult-gpt` (advisory external consult — delivered), the worker-kickback + coordinator-verify signoff (delivered)
- Reviewed by: _pending_ — SecurityEngineer, PrivacyOfficer, Procurement (community-CLI supply chain), Steward; reconciled by ChiefOfStaff
- Tracking: issue #171

## Summary

Let **non-Claude models — ChatGPT and Grok today, others later — do design and
implementation work** inside Cohort, orchestrated by a Claude (Fable or Opus) coordinator
that stays the leader and the verifier. External engines enter through a **declarative
registry**, each confined by role; the coordinator routes work across a **vendor axis** by
task fit and token cost; the **hardest reviews and designs go to a flagship council** (Claude
+ GPT-flagship + Grok-4) that aligns on one recommendation. Every external contribution
arrives as a **Claude-verified diff in an isolated git worktree** behind the **unchanged human
PR gate**.

Cohort does **not** become a multi-LLM platform. Claude always coordinates and signs off; no
external engine coordinates, and no external output is ever accepted unverified. This is the
next step past `/consult-gpt` (advisory-only): from *asking* another vendor's model for an
opinion to *delegating* bounded work to it under Claude's command.

## Motivation

`/consult-gpt` proved the safe shape for a second vendor in the room: read-only, advisory,
untrusted reply, Claude decides. The maintainer now wants two things beyond that:

1. **External engines that contribute code**, not just opinions — leveraged by task and by
   token cost (ChatGPT and Grok each have strengths and each has a metered/subscription cost
   profile).
2. **A flagship council** for the hardest problems, reviews, and designs — Claude, ChatGPT,
   and Grok's best models discussing and aligning on a single recommendation rather than one
   model deciding alone.

The value: diversity of approach on hard problems (three independent flagship perspectives
catch failure modes one model misses), and cost-appropriate delegation (route mechanical or
well-scoped external work to a cheap engine tier, reserve flagships for where they earn their
tokens).

## Design principles (inherited, non-negotiable)

These come from the existing invariants and do not bend for this feature:

- **Advisory by default; Claude coordinates and verifies.** No external engine coordinates.
  Every external output is an untrusted **claim** the coordinator re-verifies (re-run tests,
  read the diff) before signoff — the `/orchestrate` §5 discipline, applied with *extra*
  adversarial scrutiny to foreign-authored code.
- **The human PR gate is unchanged.** External work lands as a diff a human reviews and
  merges; Cohort never merges unattended.
- **Worktree isolation for any external write.** Foreign-authored changes never touch the
  main working tree until Claude has verified them — they are produced in a throwaway git
  worktree, exactly as `/orchestrate` already isolates parallel writers.
- **External content is untrusted input.** An engine's output (prose, diff, or tool call) can
  carry prompt injection; it is data to verify, never instructions to execute. Same stance as
  `/consult-gpt` and `distill`.
- **Secrets never egress; keys never commit.** No credentials, tokens, or `.env` contents in
  any engine prompt; API keys live in the environment only.
- **Stdlib-only, daemon-free.** No new Python runtime dependencies; an external engine is a
  subprocess Claude invokes and reaps, never a background service.

## Architecture

### 1. The engine registry (declarative, not hardcoded)

An **engine** is declared, never wired ad-hoc, so a new vendor is one descriptor and the
orchestration logic never names a vendor. Each engine declares:

| field | meaning |
|---|---|
| `name` | stable id (`chatgpt`, `grok`) |
| `invocation` | how Claude calls it (a CLI command template, or a direct HTTP request) |
| `roles` | which roles it may play: `consult` (advisory read) / `design` / `implement` (doer) |
| `confinement` | how it is confined *per role* (see §3) — the safety contract |
| `auth` | how it authenticates (subscription login vs API key) and where the secret lives |
| `cost_class` | `subscription` (flat) or `metered` (pay-per-token) — feeds routing |
| `model_tiers` | the engine's own cheap→flagship models (e.g. Grok `grok-code-fast-1` → `grok-4-latest`) |

**Entry: `chatgpt`** — invocation `codex exec` (OpenAI Codex CLI); roles `consult` (today) →
`design`/`implement` (this RFC); confinement `--sandbox read-only` for consult,
`workspace-write` in an isolated worktree for implement; auth ChatGPT subscription login
(cached by Codex, Cohort never reads the token); cost_class `subscription`; model_tiers the
current GPT flagship.

**Entry: `grok`** — invocation `grok -p` (community `@vibe-kit/grok-cli`, pinned version);
roles `implement`/`design`; confinement **worktree + external sandbox** (see §3 — grok-cli
has *no* read-only mode); auth `GROK_API_KEY` (env only); cost_class **`metered`**;
model_tiers `grok-code-fast-1` (cheap) → `grok-4-latest` (flagship).

### 2. Roles and how the coordinator uses them

- **consult** — advisory read-only opinion (the `/consult-gpt` shape). `/consult-grok` is the
  new sibling. Output is a claim to weigh, never executed.
- **design** — the engine proposes an approach/plan (no writes); a council contribution.
- **implement** — the engine produces a **diff** for a bounded task in an isolated worktree.
  This is the doer role and the one that crosses the invariant (§4).

The coordinator assigns *(tier, engine, role)*. Default engine is Claude; an external engine
is chosen only for a reason (approach diversity, a model's known strength, cost fit, user
preference), and its output always re-enters the `/orchestrate` signoff.

### 3. Confinement per engine × role — the load-bearing safety section

The two engines are **not** equally confinable, and the RFC must not pretend they are:

- **ChatGPT (Codex)** has a first-class `--sandbox read-only`. `consult` runs read-only
  (reads the repo, cannot write). `implement` runs `workspace-write` **inside a dedicated git
  worktree**, so writes are contained and produce a reviewable diff.
- **Grok (grok-cli)** is an **agentic text editor with no read-only flag** (audited v0.0.34:
  `--max-tool-rounds` default 400, `fs-extra`/git in its tool set). Therefore:
  - Grok is **not offered as `consult`** in Phase 1 — a read-only guarantee it cannot give is
    not one Cohort will fake. If a Grok consult is wanted, it runs either against a
    **throwaway read-only copy** of the repo or **API-direct with context packaged by Claude**
    (no filesystem access at all), decided in Phase 2.
  - Grok's `implement` role runs **only in an isolated worktree**, and the worktree is the
    sandbox: even 400 tool rounds can touch nothing outside it, and the result is a diff
    Claude verifies before anything reaches the main tree.

**Rule:** an engine may play a role **only if that role's confinement is satisfiable for that
engine.** No role without its confinement.

### 4. The advisory-boundary argument (the crux)

Cohort's spine is *advisory by default; only a project-scoped, PR-reviewed doer writes*
(README). An external engine writing code looks like a doer from another vendor — so does
this break the invariant?

**No, and here is the line.** The invariant governs **Cohort agents** — the synced roster and
my-office, which stay advisory so a synced artifact can never carry write access. An external
engine is **not a Cohort agent**; it is a **tool the coordinator invokes** to produce a
candidate diff, exactly as the coordinator invokes `git` or `pytest`. That diff is:

1. produced in an **isolated worktree** (never the main tree),
2. **verified by Claude** (re-run tests, read the diff, adversarial scrutiny), and
3. gated by the **human's PR review** before it merges.

So the external engine has **no more authority than a code generator whose output Claude
reviews** — it cannot self-accept, cannot reach the main tree, cannot merge. The thing the
invariant actually guards — *unreviewed write authority travelling across a sync boundary* —
never happens: nothing about the engine is synced, and its every write is reviewed twice
(Claude, then human). The **leak to guard** is promotion — an external diff quietly becoming a
committed change without the double review. The mitigation is structural: worktree isolation
makes "reached the main tree" impossible without passing signoff.

### 5. Cost-aware cross-vendor routing

Routing gains a **vendor axis** orthogonal to the difficulty tier. The coordinator picks the
cheapest engine×tier that fits, honoring token cost:

- **Metered engines (Grok) are spent deliberately.** Reserve them for where they add value;
  never route mechanical work to a metered flagship. Within Grok, `grok-code-fast-1` for cheap
  bounded work, `grok-4-latest` only for hard problems and council seats.
- **Subscription engines (ChatGPT, Claude)** carry no per-call meter, so they are the default
  for routine external work.
- The coordinator **discloses** a metered call's expected cost class in the plan, and a
  **cost cap** (open question, §9) bounds a run's metered spend.

### 6. The flagship council

For the hardest problems, reviews, and designs, the coordinator convenes a **council**: Claude
(coordinator), **GPT-flagship**, and **Grok-4** each produce an independent opinion, and the
coordinator **synthesizes an aligned recommendation** — explicitly surfacing where the three
disagree rather than averaging them away. The council is **advisory**: it recommends; the
human decides. It composes the existing consult primitives (`/consult-gpt`, `/consult-grok`)
plus a synthesis step, and is invoked only where the stakes justify three flagship calls
(architecture, security-sensitive design, a review that must not be wrong).

### 7. The doer loop (implement role, end to end)

1. Coordinator scopes a bounded task with acceptance criteria and a file footprint.
2. It spins an **isolated worktree** and invokes the engine (`codex exec --sandbox
   workspace-write` / `grok -p`) confined to it, with the task, criteria, and repo conventions
   in the prompt.
3. The engine produces a diff. The coordinator **verifies** it against the criteria (re-run
   tests, read the diff), applying *extra* adversarial scrutiny because the author is foreign
   and may not follow repo conventions — a kickback/redo or escalation to Claude on failure.
4. On pass, the verified diff is attributed (a commit trailer naming the engine) and offered
   to the human as part of the branch's PR. The human gate is unchanged.

### 8. Consent, egress, keys

- **Reads** are default-allow (per the existing code-sharing decision; per-repo opt-out
  honored). A metered read discloses its cost class.
- **Foreign writes** (the `implement` role) require **explicit per-repo opt-in** — accepting
  foreign-authored code is a bigger act than accepting an opinion, so it is opt-in, not
  default.
- **Secrets** never enter any engine prompt. **Keys** (`GROK_API_KEY`) live in the environment
  only, never committed, never logged.

## What Cohort explicitly does NOT do (non-goals)

- **No external engine coordinates.** Orchestration stays on Claude (Fable/Opus), never below
  Opus (per `/orchestrate` §0).
- **No unverified acceptance.** No external output — opinion or diff — is ever used without
  Claude's verification and the human's PR review.
- **No unsandboxed foreign writes.** An engine never writes to the main tree; worktree
  isolation is mandatory for the `implement` role.
- **No offering a role an engine can't be confined for** (e.g. Grok `consult` in Phase 1).
- **Cohort does not become a multi-LLM platform.** It gains the ability to *delegate bounded,
  verified work* to external engines — nothing more.

## Adversarial risks and mitigations

- **Foreign code quality / convention drift** → the worker prompt carries repo conventions;
  Claude's signoff catches drift; extra scrutiny for foreign authors.
- **Security of foreign-authored code** (subtle vuln, backdoor) → worktree isolation + Claude
  adversarial review + human PR gate; foreign writes are opt-in.
- **Prompt injection via engine output** → output is untrusted data, never executed; the
  coordinator does not run commands an engine's reply proposes.
- **Community-CLI supply chain** (grok-cli is unofficial, v0.0.34, npm) → **pin the version**,
  audit on upgrade, treat it as untrusted transport; Procurement review before adoption. This
  is the biggest new trust surface and deserves explicit sign-off.
- **Cost blowout** (metered engine in a loop) → cost cap per run (§9); metered flagships never
  used for routine work; disclosure in the plan.
- **Attribution / licensing** of external-model code → commit-trailer attribution; note the
  provenance in the PR.
- **Key leakage** → env-only, never committed/logged; a lint/secret-scan guard on the diff.

## Phased delivery (each phase decision-gated)

- **Phase 1 — advisory + registry scaffolding.** The engine registry; `/consult-grok`
  (confined per §3 — API-direct or read-only-copy, since grok-cli has no read-only mode). No
  writes yet. Lowest risk; proves the registry and the Grok transport.
- **Phase 2 — cost-aware routing + the flagship council.** The vendor axis in `/orchestrate`;
  the council synthesis. Still advisory — no foreign writes.
- **Phase 3 — external doers (the invariant-crossing part).** The `implement` role: worktree
  isolation, coordinator verification, per-repo write opt-in, attribution. This is the phase
  the advisory-boundary argument (§4) must fully satisfy the office review before it ships.

## Open questions for review

1. **grok-cli trust.** Is a pinned community CLI an acceptable transport, or should Grok be
   **API-direct** (stdlib `curl`, no third-party agent) — safer confinement (no local tool
   execution) at the cost of writing the agent loop ourselves for the doer role? (Procurement +
   Security.)
2. **Grok consult confinement.** API-direct (context-packaged, no fs) vs a read-only repo
   copy. API-direct is the cleaner read-only guarantee.
3. **Foreign-write opt-in mechanism.** A per-repo flag in `.cohort/cohort.toml`? A session
   confirmation? Both?
4. **Cost cap policy.** Per-run metered-spend ceiling — where configured, what default, what
   happens on hit (stop and ask, per the `/consult-gpt` unavailability pattern)?
5. **Council quorum.** If one flagship is unavailable (no key, rate limit), does the council
   proceed with two and say so, or defer? (Mirror `/consult-gpt`'s unavailability rules.)

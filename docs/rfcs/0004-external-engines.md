# RFC 0004 — External engines: non-Claude models as orchestrated doers

- Status: **Accepted; implementation under review** (2026-07-17) — office review complete; **Grok enters API-direct, not via the community grok-cli**. Two PRs: foundation (registry + xAI client + `/consult-grok`) and the `patch_proposal` loop + code-enforced egress gates. **Amended 2026-07-31** — see the [Amendment](#amendment--2026-07-31-grok-cli-adopted-as-a-sandboxed-doer) below: v0.10.0/v0.12.0 reversed the transport and doer decisions (items 1–2) below in favor of a bubblewrap-sandboxed local grok-cli.
- Author: Cohort maintainers
- Created: 2026-07-17
- Depends on: `/crew` (the coordinator protocol — delivered), `/consult-gpt` (advisory external consult — delivered), the worker-kickback + coordinator-verify signoff (delivered)
- Reviewed by: SecurityEngineer, PrivacyOfficer, Procurement (done 2026-07-17); reconciled by ChiefOfStaff; design cross-examined via `/consult-gpt`
- Tracking: issue #171

## Decision (2026-07-17) — review outcome

Security, Privacy, and Procurement reviewed the doer proposal independently and converged;
ChiefOfStaff reconciled, and the fable-tier design was cross-examined with `/consult-gpt`.
The binding decisions that supersede the draft below:

1. **Transport: API-direct, not `@vibe-kit/grok-cli`.** The community CLI is a 400-round
   agentic editor with no read-only mode whose purpose is to execute its own output — a
   direct conflict with Cohort's invariant that *external output is untrusted input, never
   executed as instructions*. "The worktree is the sandbox" is also false (a linked worktree
   shares `.git` and the process keeps full env/fs/network). **Grok is reached over the xAI
   HTTP API (`urllib`, `GROK_API_KEY` from env); it returns text, never executes tool
   calls.** grok-cli stays off the table unless it ever gains a real OS-sandboxed, read-only
   mode — and since API-direct yields the same outcome, that day should not come.

   > **Superseded 2026-07-31.** grok-cli was adopted as the preferred local transport,
   > bubblewrap-sandboxed. See the [Amendment](#amendment--2026-07-31-grok-cli-adopted-as-a-sandboxed-doer)
   > below — the original text is preserved above for the decision history.
2. **Vocabulary: Grok is a `patch_proposal` engine, never a "doer".** Cohort is the doer that
   applies, constrains, and verifies; Grok proposes an untrusted patch. Roles are
   `consult` and `patch_proposal`.

   > **Superseded 2026-07-31.** grok-cli now also runs as a write doer
   > (`cohort engine work grok`), bubblewrap-sandboxed. `patch_proposal` remains as the
   > gated fallback (`engine propose grok --agentic`). See the
   > [Amendment](#amendment--2026-07-31-grok-cli-adopted-as-a-sandboxed-doer) below — the
   > original text is preserved above for the decision history.
3. **The registry is a small Python registry, not per-engine descriptor files** — declarative
   metadata over two engines with different transports would be a fake abstraction; revisit
   descriptor files only when a third engine proves the shape.
4. **Patch contract: structured exact edits, not model-authored unified diffs** (which fail
   to apply); apply exactly or fail.
5. **Enforcement moves from prose to code:** per-repo egress opt-out is a hard preflight
   block; a fail-closed worktree secret-scan aborts on hit; the payload is byte-bounded
   (the primary cost/egress control), with a per-task token cap (`ceil(chars/3)` estimate +
   API `max_tokens`).
6. **Gating preconditions:** xAI no-train/bounded-retention terms need Counsel sign-off
   **before any customer/sensitive repo is routed** (not a blocker for this OSS repo);
   FinanceAnalyst models metered fan-out cost when routing lands. Until the enforcement gates
   ship, the patch-proposal role is limited to non-sensitive/OSS repos.

The architecture below is retained for context; where it conflicts with the above (notably
the grok-cli confinement in §3 and the "doer" framing), **the decision wins** — except where
the Amendment below has since superseded it.

## Amendment — 2026-07-31: grok-cli adopted as a sandboxed doer

**What changed.** Two releases reversed decisions 1 and 2 above:

- **v0.10.0 (PR #204):** `cohort engine work grok` dispatches grok-cli as a write doer that
  edits a throwaway worktree directly — Grok can now implement and review with real
  filesystem access, not only propose a gated patch. Because grok-cli has no sandbox of its
  own, Cohort imposes one with **bubblewrap**: every write is kernel-confined to the
  worktree, the user's real home (SSH keys, other repos) is not mounted, and only the
  network for the xAI API is left up. Requires `bwrap` (Linux); where it's absent the doer
  refuses rather than run grok unconfined, and `engine propose grok --agentic` remains the
  gated fallback.
- **v0.12.0 (PR #223):** grok-cli became the **preferred transport** for `review` /
  `consult` / `propose` too, not just `work`. When `grok` and `bwrap` are installed, Cohort
  runs the local CLI in a throwaway worktree (real, worktree-scoped repo access) and falls
  back to the xAI API-direct path — with a printed note — only when the CLI is absent. Both
  paths share one gate helper (identical egress/secret/wire-byte gates), and a gate refusal
  on the CLI path never falls back to the API.

grok-cli is now the **preferred** doer and transport for Grok — the opposite of what
decision 1 concluded — and Grok plays the `implement`/doer role directly (decision 2), not
only `patch_proposal`; `patch_proposal` remains available as the gated fallback
(`engine propose grok --agentic`).

**Why the original decision was reversed.** Decision 1 rejected grok-cli because it is "a
400-round agentic editor with no read-only mode" and because "the worktree is the sandbox"
is false for a bare linked worktree (it shares `.git` and the process keeps full env/fs/
network) — API-direct was adopted instead because it produced the same safety outcome
without that exposure. **Bubblewrap changes that calculus.** Cohort now imposes real
OS/kernel confinement grok-cli lacks on its own: the process is jailed to the worktree, the
user's real home is not mounted, and network is restricted to the xAI API — a materially
different, and materially stronger, sandbox than the bare linked worktree decision 1
correctly rejected. Combined with the worktree always being discarded after the run, the
result is read-only **with respect to the repo**, even though the grok-cli process itself
remains write-capable inside the jail.

**The honest caveat — the precondition is only half-met.** Decision 1's bar for
reconsidering grok-cli was "a real OS-sandboxed, **read-only** mode." Bubblewrap satisfies
the **OS-sandboxed** half. It does **not** satisfy the **read-only** half: grok-cli still has
no read-only mode of its own; it runs write-capable inside the jail, and the read-only
guarantee Cohort now offers (`run_grok_review`) comes from discarding the worktree
afterward, not from grok-cli being unable to write. This was a **deliberate tradeoff** —
kernel confinement plus a throwaway worktree substituting for a read-only mode the upstream
tool doesn't have ("that day should not come," per decision 1 — it hasn't; Cohort built
around the gap instead).

That tradeoff carries a real, currently-open cost, and the audit surfaced it: run 2 of
`/audit` (2026-07-31) flagged the exact residual risks a genuine read-only mode would have
foreclosed, still live against the sandbox-only substitute:

- **TIOCSTI escape** (issue #225) — a sandboxed-but-write-capable process can still attempt
  a terminal-injection escape that bubblewrap's default profile doesn't block.
- **`/etc` read-egress** (issue #227) — the bubblewrap profile's default mounts allow
  reading host `/etc`, a narrower but real echo of the "full env/fs" exposure decision 1
  warned about for bare worktrees.

Both are being hardened now (issues #225, #227), not treated as closed. Until they are, the
honest position is: grok-cli meets the OS-sandbox half of decision 1's precondition and not
the read-only half — and that gap is exactly where the audit found live findings, which is
the predicted failure mode of this tradeoff, not a surprise one.

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
  read the diff) before signoff — the `/crew` §5 discipline, applied with *extra*
  adversarial scrutiny to foreign-authored code.
- **The human PR gate is unchanged.** External work lands as a diff a human reviews and
  merges; Cohort never merges unattended.
- **Worktree isolation for any external write.** Foreign-authored changes never touch the
  main working tree until Claude has verified them — they are produced in a throwaway git
  worktree, exactly as `/crew` already isolates parallel writers.
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
preference), and its output always re-enters the `/crew` signoff.

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
  Opus (per `/crew` §0).
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
- **Phase 2 — cost-aware routing + the flagship council.** The vendor axis in `/crew`;
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

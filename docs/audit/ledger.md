# Audit ledger

`/audit` coverage log. Each run records what it swept, what it found, and what it struck,
so the rotation stays honest and a later run doesn't re-raise a refuted finding.

## Critical path (swept every run, never rotated out)

Cohort's highest-stakes, hardest-to-reverse surface — anything that writes the user's real
filesystem, moves data off-machine, executes external-engine code, or bootstraps trust:

- **Executor / merge / install** (`executor.py`, `merge.py`, `install.py`) — writes/links/merges into `~/.claude` & repo `.claude`; ownership-hash model; reverse/uninstall.
- **Engine gates** (`engines/gates.py`) — egress opt-out, secret scan, footprint/scope, payload bound.
- **Doer confinement** (`engines/cli_doer.py` codex-sandbox + grok-bwrap, `patch_proposal.py`, `patch.py`, `xai_agentic.py`, `ratchet.py`) — where external-engine code reads/writes/egresses.
- **Trust bootstrap** (`update.py` signed-commit + git transport allowlist, `quarantine.py` + `myoffice.py` sync quarantine).
- **Advisory tool-strip** (`ir.py` `is_doer` + the four adapters).

## Business context

Every business-track finding is derived from these facts, so a stale or absent premise
invalidates the lot — which is why `/audit` treats declaring them as a first-run job. Three
runs went without, so `business-ops` (r3) reasoned from guesses and said so.

**Fields marked 🔲 are unanswerable from the repository and need the owner.** Leave the box
unticked rather than guessing: a plausible-looking wrong jurisdiction is worse than a blank,
because the next audit will silently build on it. Re-confirm the whole block each run.

| Field | Value | Source |
|---|---|---|
| **Stage** | Free open source, **no revenue model intended** — the owner's stated goal is that the world has this and helps improve it (owner, 2026-08-01). In use by 5 people including the owner, at a range of technical levels | owner |
| **Distribution** | Git clone, or an AI agent following `AGENTS.md`. **Deliberately not on PyPI** (owner, 2026-07-31); the name there is an unrelated project (#234) | owner + `AGENTS.md` |
| **Jurisdictions — entity** | No entity. "Askwig Consulting" is **not a registered name**; the owner operates personally (owner, 2026-08-01). Operating jurisdiction itself still 🔲, and now lower-stakes: with no entity and no data collected, it bears mainly on B3 (trademark) | owner, `LICENSE:3` |
| **Jurisdictions — users** | 🔲 — unknown; a public repo has no user registry | owner |
| **Entity form & filings** | **None.** Unregistered, operated personally, so there is no filing calendar to own (owner, 2026-08-01). The consequence to keep visible: liability for the tool's behaviour sits with the owner personally, not a company | owner |
| **Insurance** | 🔲 — not stated; follows from the unregistered/personal posture rather than being an independent decision | owner |
| **Licence (outbound)** | MIT | `LICENSE` |
| **Licence (inbound)** | Implicit GitHub ToS until `CONTRIBUTING.md` stated terms (2026-08-01). Before that commit the tree carries **1 non-owner commit author + 2 non-owner human `Co-authored-by` trailers (one malformed/unresolvable)** — r3's "two users" counted people, the old row counted commits (r4 correction). None after | `CONTRIBUTING.md`, `git log` (r4) |
| **Dependencies** | `typer`, `click`, `PyYAML` (+ their trees) — all permissive, none copyleft | `pyproject.toml`, `requirements.lock` |
| **Personal data — collected by us** | **None automatically.** No telemetry, no analytics, no accounts, no server. Footnotes (r4): `cohort report` (0.17.0) is an explicit, user-confirmed **public GitHub issue** whose environment block is version/OS/Python only — an inbound channel by which the owner receives user content, with GitHub as processor; the `update-check` session hook runs one `git fetch` per day against the source remote (no content, but automatic outbound traffic — README's "no phone-home" needs the qualifier, #271) | `grep` over `cli/` (r4: only `xai.py:270`, `xai_agentic.py:444` open sockets); `report.py:58-70` |
| **Personal data — on the user's machine** | Session records, snapshots, feedback, proposals, engine transcripts, working memory — all local, under `~/.cohort/` and `<repo>/.cohort/`. Never transmitted to us | `project.py`, `myoffice.py` |
| **Third-party processors** | Anthropic (as the harness Cohort runs inside — never called directly), OpenAI (`codex exec`), xAI (`api.x.ai`) — reached **with the user's own credentials**; **GitHub** (`gh`/`git` for `report`, `submit-proposals`, `my-office sync`, `update`). Cohort orchestrates the call; it is not a party to it. Complete reachable-host list re-derived r4 | `engines/`, `report.py`, `improve.py:637-639`, `myoffice.py:170,209`, `update.py:306,369` |
| **Egress default** | **Allow**, opt-out per repo via the literal `cohort:egress=deny` marker — enforced in code for every `cohort engine …` path and `ratchet`; **prose-only for `/consult-gpt`** (#266) and **not consulted by `cohort report`** (#268); GitHub-bound commands are outside the marker's documented scope | `engines/gates.py`, README, r4 |
| **Regulatory posture** | The claim: Cohort is a **locally-run developer tool**, not a service, not a processor, and holds no user data off-machine. What keeps it true: no telemetry, no server, no account system — verifiable by the greps above, and worth re-verifying each run rather than assuming | derived |

### Adoption evidence — and what it does to the go-to-market premise

**r3's G2 said the company-fork assumption had never been tested against a single real org,
with no named user, pilot or LOI. The first half is now refuted:** four people besides the
owner use Cohort, at deliberately varied technical levels (owner, 2026-07-31). That range is
worth more than the count — it is the only way to find where onboarding actually breaks, and
it arrived the same week the install path became a documented contract (`AGENTS.md`).

**The second half is not refuted so much as redirected, and it is the finding worth carrying
forward.** The owner is bringing Cohort into their company by **porting the important parts
into an existing company harness**, not by running a company Cohort fork. That is the
best-informed adopter available choosing a path the architecture does not centre — behaviour,
not stated intention, which is the stronger evidence.

If that generalises, the wedge is **the pieces, not the topology**: the gates, the canonical
artifact format, the commands — things another harness can absorb — rather than the
office / my-office / project three-tier machinery built to host them. It changes what a 1.0.0
stability promise should cover, since people depend on what they ported, not on what they
forked.

Answered by the owner, 2026-07-31, and three of the four answers move something:

**The acquisition channel works, and worked before it was documented.** Three of the four
were installed by the owner telling them to point their AI IDE at the repo — no walkthrough,
no `AGENTS.md` (it did not exist yet), and all three succeeded. Agent-mediated install is not
a hypothesis; it is the observed default. `AGENTS.md` hardens a channel already carrying
traffic rather than opening one.

**Onboarding cost was mis-ranked, and the coordinator's prediction was wrong.** r2's GTM pass
flagged the ~10-step onboarding as an activation risk, and the r3 follow-up predicted the
durable-PATH symlink as the likely sticking point. Nobody got stuck on anything. The reason
is structural rather than luck: **the agent absorbs the friction**, so step count is close to
free when a machine performs the steps. Onboarding cost should be re-weighted for the
agent-install path, and only counts for a human following the README by hand.

**Two of the four have committed back** — so the inbound-IP gap is no longer one stray commit.
r3's business-ops finding (no DCO, no CLA, inbound relying on GitHub ToS) now has several
contributors behind it, and 1.0.0 is the moment provenance freezes. This is the cheapest
pre-1.0.0 item on the list: one line in `CONTRIBUTING.md`.

**The company path is a third pattern, and not the one the architecture centres.** It is not a
fork, and not a one-time port: the owner built a separate company harness in parallel and
**asks it weekly to pull the good parts out of Cohort**. Recurring, agent-mediated extraction.
What travelled: **agents, skills and commands** — the canonical artifacts. What did not: the
**engine layer**, deliberately, because company code cannot carry the same risk.

**Scope correction (owner, same day).** An earlier draft of this entry read the company's
choice as evidence that the engine layer "travels with nobody". That was one data point
generalised into a pattern, and it is wrong. **The four individual users run Cohort whole,
engine included** — the engine layer is load-bearing for them, and they are currently most of
the user base. The company is a single adopter with a specific constraint, not the trend.

The accurate picture is two populations depending on two different surfaces:

| Population | Depends on | n |
|---|---|---|
| Individual users | **All of Cohort**, engine and gates included | 4 |
| A company harness (employer code) | The **canonical artifacts** — agents, skills, commands — pulled weekly; own engine | 1 |

Consequences, scoped to what the evidence actually supports:

1. **1.0.0 has to promise stability on both surfaces, for different reasons.** The canonical
   artifact format is what crosses a harness boundary, so it is what an outside consumer
   pins against. The engine and gate behaviour is what individual users run directly, so it
   is what breaks their day if it moves. Neither can be treated as the incidental one.
2. **Default-allow egress looks like a barrier where the code belongs to an employer — n=1,
   and worth watching rather than acting on.** The company swapped the engine out
   specifically to avoid that risk profile. That is a real signal from a well-informed
   adopter, and it is one organisation; it does not yet establish that the posture is wrong
   for companies generally. The cheap response is a documented low-risk configuration, not a
   change of default.

The audit-attention observation survives the correction in weaker form: the engine layer is
the most heavily hardened subsystem here, and the canonical artifact format — depended on by
both populations, and the only surface that crosses a boundary — has had comparatively little
of it. A later run should weight the slice toward the artifacts.

### The posture is the part worth attacking

A posture with nothing enforcing it is the highest-value business finding there is. Here the
enforcement is *absence* — no server, no telemetry — which is cheap to hold and easy to lose:
a single "just send us anonymous usage stats" feature would move Cohort from "not a processor"
to "processor", and the first place it would show up is a new outbound call in `cli/`. A
future run should re-run that grep rather than trusting this row.

The unresolved question is narrower: Cohort's *default-allow* egress means the author chose,
on the user's behalf, that source goes to three vendors unless the user opts out. That is
**B1** in the r3 report's
[§BUSINESS-OPS](r3-2026-07-31.md#business-ops--legal-compliance-filings-fable) section,
alongside **B2** (inbound licensing of commits merged before `CONTRIBUTING.md` stated terms)
and **B3** (trademark clearance on the name).

**Owner decision, 2026-08-01: assessed as low risk; no counsel consult for now.** Recorded as
the owner's risk acceptance, not as a legal conclusion — this ledger cannot make one, and the
questions stay framed above so a later run finds the reasoning rather than re-deriving it.

Two things to keep accurate under that decision, because the record should survive the
summary:

* The stated basis was "we are not collecting personal info at all." That is exactly right
  about **Cohort itself** — no telemetry, no accounts, no server, verifiable by grep. The
  narrower fact B1 turns on is different: Cohort **transmits the user's source by default**,
  and that source can contain personal data. It travels under the user's own keys and vendor
  agreements, which is the substance of the distributor-not-processor position; the design
  choice of the *default* is the part left unexamined. Nothing here says that is wrong — only
  that the decision covers a slightly narrower question than the sentence suggests.
* **B2 gets more important under the stated goal, not less.** "I want the world to have this
  and help improve it" is a bet on contribution, and provenance is a project-health question
  before it is a liability one: relicensing, or dual-licensing, later means chasing every
  contributor. `CONTRIBUTING.md` (0.16.0) stops the gap growing; commits merged before it
  remain as they were. **B3** is likewise a cost question rather than a liability one — a
  post-1.0 rename breaks every install script, since the command *is* the brand.

## Dimension coverage

`Tiers seen` records which model tier reviewed the dimension on each run, so the FP rate
below can be read **per tier** rather than as one blended number — the point of the model
rotation in `/audit` §3. A dimension only ever seen by one tier has that tier's blind spots
baked into its history.

| Dimension | Last run | Tiers seen | FP rate | Notes |
|---|---|---|---|---|
| critical-path | 2026-09-10 (r4) | opus (r1), fable (r2), fable (r3), fable + gpt-5.6 codex (r4) | 0/5 (r1), 0/2 (r2), 0/8 (r4 Fable), 2/6 struck (r4 Codex) | swept every run; r4 found two scanner shapes by executing the scanner; Codex's two strikes were by-design/moot items |
| security | 2026-07-31 (r2) | opus (r1), fable (r2) | 0/6 (r1) | not in the r4 slice (critical-path overlapped); due r5 |
| correctness | 2026-09-10 (r4, **partial**) | opus (r1), opus (r4, stalled) + coordinator probes | 1/8 struck (r1) | r4: the Opus reviewer hung on a permission prompt and returned nothing; coordinator executed scanner/timeout/gc-age probes and verified the reviewer's one lead (session-capture `diff --stat` blind spot, #280). **Weight into r5** |
| concurrency | 2026-07-31 (r2) | opus (r1), opus (r2) | 0/7 (r1) | r2 downgraded the filelock finding on likelihood (#230) |
| honesty | 2026-09-10 (r4) | opus (r1), fable (r4) | 0/2 (r1), 0/12 (r4; 2 HIGH) | r4's richest dimension: `/consult-gpt` prose-only, global `--dry-run` placebo, four wrong README egress rows |
| tests | 2026-09-10 (r4) | opus (r1), sonnet (r4) | 0/4 (r1), 1/3 downgraded (r4: Copilot killing test HIGH→LOW, E060 pre-empts) | r4 ran a real copy-and-mutate probe; found the real-venv test never runs in CI |
| supply-chain | 2026-09-10 (r4) | opus (r1), haiku (r4) | 0/5 (r1), 0/4 (r4, all LOW) | r1 fixes hold; lockfile is decorative; Haiku was adequate for this mechanical sweep |
| performance | 2026-07-31 (r2) | opus (r2), grok-4.3 (r3) | r3 review **struck** | r2 covered it (found #226); the r3 *re-review* was struck as unreliable, so r2 remains the real coverage |
| resilience | 2026-07-31 (r3) | opus (r2), grok-4.5 (r3) | 0/6 (r3) | strongest external review of r3 |
| naming | 2026-07-31 (r3) | gpt-codex (r3) | 0/2 | first coverage |
| docs | 2026-07-31 (r2) | opus (r2), grok-4.20-0309-reasoning (r3, **no answer**) | — | r2 covered it; the r3 attempt hit max_iterations and returned nothing |
| ops | 2026-09-10 (r4) | grok-4.5 (r3), sonnet (r4) | 1/7 downgraded (r3), 0/4 (r4) | r4: ticket reconciliation (13/16 fixed in code), PVR disabled, v0.17.0 untagged |
| accessibility | 2026-07-31 (r3) | opus (r2), gpt-codex (r3) | 0/6 (r3) | r2 found #232; r3 found the keyboard-operability gaps |
| vendor-reachability | 2026-09-10 (r4) | opus (r3), coordinator (r4) | 0/6 (r3) | **always-on**; r4: codex reachable (ChatGPT login), **grok unreachable — no `XAI_API_KEY` in the session environment** (environment, not code). Record reachability before routing |
| artifact-contract | 2026-09-10 (r4) | opus (r4) | 2 refined of 6 (r4) | **new dimension** (the surface a company harness pulls weekly): Cursor/Codex drop the `tools` allow-list; install path skips E080; no schema version or stability doc (#272, #273) |
| go-to-market | 2026-09-10 (r4) | fable (r2), grok-4.5 (r3), fable + gpt-5.6 codex (r4) | r4: 1 refined, 2 self-struck of 6 | always-on; **two vendors met in r4**; both converged on "stop leading with multi-IDE" and "stop expanding topology" |
| business-ops | 2026-09-10 (r4) | fable (r3), fable (r4) | 0/8 (r3), 1 struck + 3 refined of 7 (r4) | r4 re-confirmed every context row; corrected the inbound-licence count; added the GitHub processor and the `report` footnote |
| privacy | 2026-09-10 (r4) | opus (r3 follow-up), sonnet (r4) | 0/1 (r3), 1 refined of 3 (r4) | r4: engine worktrees carry sessions/feedback/proposals with git author name+email (#275) |
| dead-ends | 2026-09-10 (r4) | opus (r3 follow-up), haiku (r4) | 0/2 (r3), 0/0 (r4) | r4: clean; `office_reconcile` now wired (`update.py:758`) |

### Tier signal so far

Early, but two data points worth carrying forward: **grok-4.5 produced the strongest
external reviews** (resilience, ops — dense, line-accurate, correctly hedged). **grok-4.3
produced a false negative** on code changed hours earlier, and **grok-4.20-0309-reasoning
could not finish** a normal review within the default iteration budget. Do not route a
dimension to `cheap` when the target includes recently-changed code, and do not route to
`reasoning` at all until the iteration budget is raised.

**r4 signal:** Fable on honesty and critical-path produced the run's three HIGHs with zero strikes; Haiku was sufficient for supply-chain and dead-ends (mechanical, 0 false positives); Sonnet on ops/privacy/tests was accurate but over-rated severity (3 of 5 HIGHs refined down); Codex (gpt-5.6, `codex exec`) found two real MEDs (TOML quoted key, ancestor symlink) and two by-design items — usable as a second vendor on critical-path with coordinator verification. **Operational:** two agents (Opus correctness, Fable refuter) hung >1h on a `PreToolUse` prompt for `rm -rf` on their own scratch dir — forbid `rm -rf` in worker prompts and treat a silent worker after 30 min as stalled.

Both r3 losses landed on dimensions **r2 had already covered with Opus**, so the practical
damage was smaller than the r3 report first implied — that report was written before the r2
ledger was merged in, and overstated `performance` and `docs` as uncovered. `privacy` and
`dead-ends` are the real gaps: never swept in three runs.

## Model phase

`/audit` advances the routing phase each run — `balanced` → `complex-heavy` →
`simple-heavy` → `balanced` — so no dimension is permanently bound to one tier's blind
spots. Record the phase each run and read the *next* one from here.

| Run | Phase |
|---|---|
| r1 (2026-07-29) | balanced (retroactive — routing predates the rotation) |
| r2 (2026-07-31) | complex-heavy (retroactive — Fable + Opus throughout) |
| r3 (2026-07-31) | complex-heavy — 50% grok / 25% GPT / 25% Claude, all grok API-direct |
| r4 (2026-09-10) | simple-heavy — Haiku ×2, Sonnet ×3, Opus ×3, Fable ×4 (always-on + business + honesty), Codex ×2; grok unavailable |
| **next run** | **balanced** — and weight `correctness` (partial in r4) and `security` (last r2) into the slice; keep `performance`/`docs` at a capable tier |

## Run 4 — 2026-09-10 — six weeks after 0.17.0

Full report: [`r4-2026-09-10.md`](r4-2026-09-10.md). Coordinator: Fable 5.1. 12 Claude
reviewers + 2 Codex passes; round two = 2 Fable refuters (business track completed; the
code-track refuter stalled and the coordinator finished its pass). Baseline 1,383 tests green.

Headline: **three HIGHs, all honesty-shaped** — the secret scanner misses `{"password": …}` and
`WARN: PASSWORD=…` on every egress path (#265); `/consult-gpt` is prose-only while README and
AGENTS.md say the code checks the marker (#266); the global `--dry-run` is ignored by
`approve`, every `engine …`, `report` and `try` (#267). Thirteen MED findings, one LOW batch.
**13 of 16 open audit tickets were already fixed in code** and are ready to close (#236, #237,
#259 stay open). `v0.17.0` was never tagged.

Business track (two vendors on GTM for the first time): both converged that the orchestration
and adapter layers are being commoditised and the governance layer is the durable asset;
1.0.0 needs per-population promises; the untested assumption is that anyone consumes a
non-Claude compile. Market scan filed as a ranked candidates list (#281); owner decisions and
counsel questions routed in #282.

### Struck / downgraded (carry forward — do not re-raise without new evidence)
- Codex: empty `.cohort` passes provenance — by design (F5).
- Codex: SCAFFOLD reverse without hash — moot under `deinit --purge`.
- BO-M3 Anthropic hook canary — no trigger; events current; function hooks additive/default-off.
- GTM: Cursor `/goal` collision (never compiled to Cursor); Copilot AGENTS.md support (2025).
- Refined down: report/marker (category), AC tools-drop (README promises `readonly` only),
  E080 duplicate (CI + quarantine mitigate), PII in worktrees (disclosed as "committed
  contents"), Copilot killing test (E060 pre-empts), OpenAI "steers away" (docs: default is
  saved login, keys *recommended* for CI).

### Tickets
New: #265–#282. Verified fixed, owner to close: #225 #226 #227 #228 #229 #230 #231 #232 #244
#245 #246 #258 #260.

## Run 3 — 2026-07-31 — the path to 1.0.0

Full report: [`r3-2026-07-31.md`](r3-2026-07-31.md). Coordinator: Opus (Fable mode).
10 reviewers, 3 vendors. **No round-two refutation panel was run** — coordinator
verification substituted for it, and findings not independently verified are marked
REPORTED in the report.

Headline: four defects were found in *the branch's own new code* and fixed before the report
closed — including that the same session's "zero-loss" scanner precision fixes were **not**
zero-loss (`PASSWORD=jonathan.smith` and `password: Path-2026-Xy9z-secretvals` both passed
on the branch and were caught on master). Regression tests now pin them.

Six HIGH findings stand for 1.0.0; the two cheapest are documentation — a user-facing
statement of what egresses by default and how to stop it (H4), and declaring the business
context this ledger has never held (M9).

## Run 2 — 2026-07-31 (mostly Fable + Opus panel)

Panel: 8 Claude reviewers (Fable: critical-path, security, go-to-market; Opus: concurrency,
resilience, performance, docs, accessibility) + a grok cross-vendor pass. Coordinator (Opus)
re-verified every HIGH + convergent finding against the code. Slice weighted to the large NEW
surface since r1 (the v0.12.0 grok CLI-preference dispatch, the manifest-lock completion, the
release machinery) + the never-covered dimensions.

**grok cross-vendor pass: fail-closed blocked** — the worktree (a full cohort checkout) tripped
the secret scanner on the repo's own detection **test fixtures**, so nothing was sent. The gate
worked correctly; no independent grok findings this run (→ finding #233).

### Confirmed — HIGH
1. **grok/codex doer TIOCSTI escape** — `_grok_sandbox_argv` omits `--new-session` (`cli_doer.py:373`); doers inherit the TTY (`:296`,`:428`, no `stdin=`). Compromised engine → host RCE; mitigated on kernels ≥6.2. **#225.** Coordinator-verified.
2. **Dashboard `/api/state` unbounded+uncached every 6s, no failure isolation** — `check_parity` re-parses 55 files/IDE (`parity.py:113`); cross-project activity/scorecards re-read every session/feedback file/project (`dashboard.py:203,224`); `do_GET` no try/except → one bad `.md` 500s all. **CONVERGENT (performance + resilience).** **#226.**

### Confirmed — MED
3. **grok `/etc`+`/usr` readable & un-scanned; docstring overstates read-confinement** (`cli_doer.py:378,365`) — CONVERGENT (security + critical-path). **#227.**
4. **grok-cli-as-doer reverses RFC 0004 with no recorded rationale** + stale docstrings (`cli_doer.py:226`, `cli.py:944`) — the RFC's "read-only mode" precondition is only half-met; the TIOCSTI + /etc findings are exactly what it guarded. **#228.**
5. **Doer egress opt-out reads a caller string (default `""`), not repo state** (`cli_doer.py:495,548,604`) — latent. **#229.**
6. **filelock non-atomic stale-steal + token-release** (`filelock.py:80,97`) — real, **narrow/near-impossible** in practice (downgraded on likelihood). **#230.**
7. **Agentic loop has no overall wall-clock deadline** (`xai_agentic.py:440`, ~90 min worst case). **#231.**
8. **Dashboard a11y** — color-only sparkline (`dashboard.js:436`), no `prefers-reduced-motion`, sub-AA `--faint` text. **#232.**
9. **grok CLI-doer unusable on ordinary/own repos** — 5 MB wire cap vs full checkout + secret-fixture false positives (usability; gates are correctly fail-closed). **#233.**

### Confirmed — business track (go-to-market)
- **PyPI name `cohort` is taken by a same-category competitor** — `pip install cohort` installs a rival; name not locked (no publish step). **#234.** Other GTM findings (multi-IDE wedge shipped "experimental" vs a 38k-star marketplace + AGENTS.md standard; ~8-step onboarding; office-vs-dev-workflow wedge untested on a real user; no adoption channel) are **owner decisions**, not eng tickets — in the report.

### Struck / downgraded (round 2)
- Nothing struck — the Fable/Opus panel was evidence-disciplined and the coordinator re-verified the HIGH/convergent set against code. Two **downgraded**: filelock races → narrow/near-impossible for ms-scale JSON rewrites; grok-under-`~/`-unreachable → conditional on install layout (fails closed, so safe).

### Notable controls verified sound (evidence-based negatives)
CLI routing never reaches the xAI API after a gate fires (`_run_grok_cli_review_or_exit` exits on every branch); grok is never run unsandboxed and its diff is never auto-applied; update is FF-only to a pinned SHA with fail-closed signature/pinned-key gates (`update.py:688,764`); `patch.py` write-containment refuses symlink traversal; install `apply`/`reverse` is crash-consistent (atomic writes + LIFO reverse); worktree cleanup is on every error path; quarantine + manifest RMW cycles are serialized under one lock anchor per store; `_UpdateCache` is exemplary TTL-caching.

### Dimension coverage update
critical-path · security · concurrency · resilience · performance · docs · accessibility · go-to-market → **swept r2 (2026-07-31)**. Still stale: **naming, ops, privacy, business-ops, dead-ends** (never); correctness/honesty/tests/supply-chain (r1) — next rotation.

## Run 1 — 2026-07-29

Panel: 7 Claude subagents (one/dimension) + 1 ChatGPT cross-vendor pass on the exec/egress
surface. Coordinator (Opus) verified every finding against the code before recording.

### Confirmed — HIGH
1. **Grok/CLI-doer inherits the full host environment + unrestricted network** — `cli_doer.py:167` (`--share-net`, no `--clearenv`) + `:217` (`subprocess.run` with no `env=`). An untrusted grok-cli (60 tool-rounds) can read every exported secret and POST it anywhere; the "network only for the xAI API" docstring is prose the code doesn't build. Codex spared (its sandbox disables network). **Convergence: critical-path + security + ChatGPT (2 vendors).** Shipped in #204.
2. **Agentic patch-proposer skips the code-enforced secret scan** — `patch_proposal.py:451` runs only `require_egress_allowed`; the outbound task + `project_context.md` POST to xAI unscanned, unlike the one-shot/`/consult-grok`/`cli_doer`/`ratchet` paths (all run `assert_no_secrets`). Contradicts its own docstring + RFC 0004. **Convergence: honesty + ChatGPT.** Shipped in #188.

### Confirmed — MEDIUM
3. **CLI-doer egresses committed files with no content secret-scan** (`cli_doer.py:249,295`) — scans only the task string; the vendor CLI reads+sends committed files unscreened. Convergence: critical-path + security.
4. **No cross-process file lock on any JSON state** — registry (`project.py`, non-atomic `write_text` + dashboard-poll RMW → a concurrent `cohort init` silently lost), manifest (`manifest.py` per-op persist → dropped op record → broken reversibility), quarantine (`quarantine.py` RMW → gate bypass). Root cause: no `fcntl.flock`.
5. **`--force` JSON-hook restore duplicates a user-edited entry** (`merge.py:162-177`) → the hook fires twice; asymmetric with the block path (`upsert_block` replaces).
6. **Office-quarantine adopts the just-pulled state as trusted baseline on first run** (`quarantine.py:416`, wired at `update.py:754`) — a shared-remote artifact folds into "trusted" unreviewed on the first post-feature update.
7. **`tomllib` breaks the Python 3.10 floor** — `parity.py:13` unconditional import (unlike `project.py`/`update.py` which guard it), reached eagerly from the entrypoint → `cohort` crashes at import on 3.10 (`requires-python=">=3.10"`); CI only tests 3.12.
8. **`jsonschema>=4.20` declared but imported nowhere** — dead runtime dep pulling 5 packages incl. a Rust wheel; contradicts the stdlib-only claim.
9. **Signed-update verifies only the tip commit, not `HEAD..tip`** (`update.py`) — policy-scope nuance (a signature commits to ancestry, so not a crypto bypass; the wording implies more).
10. **Test gap: codex/cursor advisory-strip `is_doer` backstop unpinned** — the fail-open mutation survives green (Claude/Copilot have the killing test).
11. **Test gap: grok bwrap confinement kernel test is skip-gated in CI** (`test_cli_doer.py:187`) — confinement regressions stay green where `bwrap` is absent.

### Confirmed — LOW/INFO
- No lockfile; floating deps (`PyYAML`/`jsonschema`/`pytest` unbounded).
- Egress opt-out fails open on ≥4-space indentation; crashes if the policy file is present-but-unreadable (should fail closed).
- Stale docstring at `compile.py:253` claims the office-quarantine gate is unwired ("safe no-op") when `update.py:754` wires it (docs-honesty).
- Dangling-symlink uninstall (`executor.py:136`) removes a user-repointed link even when it no longer matches the recorded target.
- `.git` sensitive-class match is exact — dodgeable by a trailing-dot segment on Windows.
- Secret-scanner code-shaped exemption is broad (`str("secret")` evades).
- `session_recall` can mislabel a human `cohort snapshot` as "auto-captured".
- `preserve`-absent pre-migration manifest → a team file can be removed on non-purge deinit.
- External CLIs (codex/grok/bwrap) presence-guarded but not version-guarded.
- Marker check-then-act races (`session_recall`, `staleness_check`, `working_capture`) → at-most-cosmetic double-fire; `working_capture` writes its dedup marker before the record.
- Lint `_ORCH_CAP_RE` misses a hyphenated "in-flight" restatement.

### Struck (Round 2)
- Correctness "office-quarantine gate is inert / unwired" — **struck**: `update.py:754` wires `record_office_delta`; the claim rested on a stale `compile.py` docstring (recorded as a LOW docs-honesty item instead).

### Notable controls verified sound (evidence-based negatives)
Patch write-containment refuses symlink traversal & resolves within-root (`patch.py:246-292`); sensitive-class override can't launder across classes (`gates.py:519`); executor reverse re-verifies ownership before removing and never unconditionally deletes user data; apply-time TOCTOU re-check; signed-commit *identity* matching is whole-token not substring; git transport is default-deny with `ext::`/`fd::` banned; the advisory/doer leak-guard holds across all synced-layer compile call sites; API keys never on argv. EOL-agnostic hashing keeps merges idempotent/removable.

### Theme
Most HIGH/MEDIUM findings cluster in the **external-engine (Grok) egress surface** — code shipped *this session* (#204 grok doer, #188 agentic proposer). The deterministic install/merge/trust core audited well. Fix priority: the two HIGH egress items first.

## Remediation — 2026-07-30 (`/crew`, coordinator Opus)

Two-phase fix. Coordinator verified every diff and re-ran the suite as the integration check.

**Phase 1 — HIGH + one MED (PR #212, `fix/audit-high`):** consult-gpt cross-examined.
- #1 grok/CLI-doer full env + network → `_scrubbed_env()` (minimal env: PATH/HOME/LANG/TERM + per-engine key passthrough + TLS-CA passthrough), base-URL userinfo guard. **Fixed.**
- #2 agentic proposer skips secret scan → `propose_patch_agentic` now runs `gates.preflight(...)` like the one-shot path. **Fixed.**
- #3 CLI-doer egresses committed files unscanned → `_assert_worktree_files_have_no_secrets()` (git ls-files → binary-safe read → `scan_for_secrets`, fail-closed on unreadable). **Fixed.** (consult-gpt caught my first cut was fail-OPEN.)
- #10 advisory-strip test gap → killing tests for codex/cursor in `test_phase7.py`. **Fixed.**

**Phase 2 — MED + LOW (PR #214, `fix/audit-rest`, merged):**
- #4 no cross-process state lock → new `filelock.py` (portable `O_EXCL` lock-file + token + 15s stale-steal + 30s `LockTimeout`); wraps RMW in `quarantine.py` (registry+office), `project.py` (registry), `manifest.py` (`refresh_*`). **Fixed for those callers**; the remaining manifest writers closed in round 3 below.
- #5 `--force` hook restore duplicates → `merge_hooks` force path now replaces (bounded to `len(diverged)`). **Fixed.**
- #6 office-quarantine trusts just-pulled state → `seed_office_baseline_if_absent` records the pre-pull tree as baseline before the FF. **Fixed.**
- #7 `tomllib` breaks 3.10 floor → guarded import + minimal `[[gaps]]` fallback parser; CI matrix adds 3.10. **Fixed.**
- #8 `jsonschema` dead runtime dep → moved to dev extras (confirmed zero runtime imports). **Fixed.**
- #9 signed-update tip-only → docstring clarified: FF-only means the signed tip's hash chain commits to `HEAD..tip`, so tip verification is sound (doc fix, not a vuln). **Fixed.**
- LOW: egress fail-open on ≥4-space indent (`^[ \t]*`), stale `compile.py`/`quarantine.py` "no-op" docstrings, dangling-symlink uninstall (readlink-string match only), `.git` Windows trailing-dot dodge (`rstrip(". ")`), `session_recall` mislabel + `working_capture` marker-before-record, `preserve`-absent SCAFFOLD → `True`, lint hyphenated `in-flight`. **Fixed.**

**Round 3 — the tracked follow-ups (`fix/audit-followups`, `/crew` 3-worker fan-out + a completion pass):**
- **#215 — DONE.** Locked the racy main manifest writers (`do_install` conditional bootstrap guard, `do_install_project`, `do_uninstall` slice, `adopt` ×2); `reverse_full` deliberately unlocked (it sweeps `state/`). Neutralize-the-lock bite proof passes.
- **#4 FULLY CLOSED.** The #215 worker surfaced 4 further unlocked RMW sites the issue hadn't enumerated (`office_setup.persist_roster`, `roster.do_add_agent` office-extend, `specialists.do_remove_specialist`, `project.do_init` bootstrap-conditional). All locked in a completion pass — finding #4 now holds across **every** manifest RMW call site.
- **#216 — verify-then-fix.** (c) wire-byte cap → `gates.assert_total_wire_bytes` + fail-closed worktree byte accounting before both doers (**FIXED**, default 5 MB, configurable). (a) codex reads outside worktree → **CONFIRMED but infeasible** (no read-scoping flag; Landlock backend can't; a version-specific fix fails open) → documented residual, not a placebo. (b) user-local grok not bwrap-jailed → **REFUTED** (grok is always bwrap-wrapped; no direct-exec path).
- **#217 — DONE.** Pinned floating deps (`PyYAML<7`, `pytest<10`, `jsonschema<5`); added `requirements.lock` (runtime closure); installed `bubblewrap` in Linux CI so the confinement test (audit #11) runs instead of skip-gating.

**Still open (new discovery, tracked):** none from this round — the 4 extra manifest sites were folded into the #4 closure rather than deferred.

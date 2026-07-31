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

## Dimension coverage

`Tiers seen` records which model tier reviewed the dimension on each run, so the FP rate
below can be read **per tier** rather than as one blended number — the point of the model
rotation in `/audit` §3. A dimension only ever seen by one tier has that tier's blind spots
baked into its history.

| Dimension | Last run | Tiers seen | FP rate | Notes |
|---|---|---|---|---|
| critical-path | 2026-07-31 (r3) | opus (r1), fable (r3) | 0/5 | swept every run; r3 caught two scanner regressions by *executing* both versions |
| security | 2026-07-29 (r1) | opus (r1) | 0/6 | |
| correctness | 2026-07-29 (r1) | opus (r1) | 1/8 struck (#7 office-quarantine "inert" — wrong, it's wired) | |
| concurrency | 2026-07-29 (r1) | opus (r1) | 0/7 | |
| honesty | 2026-07-29 (r1) | opus (r1) | 0/2 | |
| tests | 2026-07-29 (r1) | opus (r1) | 0/4 | |
| supply-chain | 2026-07-29 (r1) | opus (r1) | 0/5 | |
| performance | 2026-07-31 (r3) | grok-4.3 (r3) | **whole review struck** | **treat as UNREVIEWED** — cheap tier reported "no findings" resting on a false claim about freshly-changed code; re-run at a higher tier |
| resilience | 2026-07-31 (r3) | grok-4.5 (r3) | 0/6 | strongest external review of the run |
| naming | 2026-07-31 (r3) | gpt-codex (r3) | 0/2 | |
| docs | — never | grok-4.20-0309-reasoning (r3, **no answer**) | — | **still UNREVIEWED** — the reasoning tier hit max_iterations and returned nothing |
| ops | 2026-07-31 (r3) | grok-4.5 (r3) | 1/7 downgraded (#5 quarantine "contradiction" is a false *comment*, not a runtime bug — grok correctly hedged) | |
| accessibility | 2026-07-31 (r3) | gpt-codex (r3) | 0/6 | |
| vendor-reachability | 2026-07-31 (r3) | opus (r3) | 0/6 | **always-on**; added after grok was found unreachable despite correct code |
| go-to-market | 2026-07-31 (r3) | grok-4.5 (r3) | — | always-on; single-vendor this run — **protocol requires two, not met** |
| business-ops | 2026-07-31 (r3) | fable (r3) | 0/8 | first run; found the business context itself is undeclared |

### Tier signal so far

Early, but two data points worth carrying forward: **grok-4.5 produced the strongest
external reviews** (resilience, ops — dense, line-accurate, correctly hedged). **grok-4.3
produced a false negative** on code changed hours earlier, and **grok-4.20-0309-reasoning
could not finish** a normal review within the default iteration budget. Do not route a
dimension to `cheap` when the target includes recently-changed code, and do not route to
`reasoning` at all until the iteration budget is raised.

## Model phase

`/audit` advances the routing phase each run — `balanced` → `complex-heavy` →
`simple-heavy` → `balanced` — so no dimension is permanently bound to one tier's blind
spots. Record the phase each run and read the *next* one from here.

| Run | Phase |
|---|---|
| r1 (2026-07-29) | balanced (retroactive — routing predates the rotation) |
| r3 (2026-07-31) | complex-heavy — 50% grok / 25% GPT / 25% Claude, all grok API-direct |
| **next run** | **simple-heavy** — but keep `performance` and `docs` at a capable tier; both were lost this run to cheap/reasoning tiers (see above) |

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

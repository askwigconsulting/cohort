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

| Dimension | Last run | FP rate | Notes |
|---|---|---|---|
| critical-path | 2026-07-29 (r1) | 0/5 | swept every run |
| security | 2026-07-29 (r1) | 0/6 | |
| correctness | 2026-07-29 (r1) | 1/8 struck (#7 office-quarantine "inert" — wrong, it's wired) | |
| concurrency | 2026-07-29 (r1) | 0/7 | |
| honesty | 2026-07-29 (r1) | 0/2 | |
| tests | 2026-07-29 (r1) | 0/4 | |
| supply-chain | 2026-07-29 (r1) | 0/5 | |
| performance | — never | — | **deferred to r2** |
| resilience | — never | — | **deferred to r2** |
| naming | — never | — | **deferred to r2** |
| docs | — never | — | **deferred to r2** |
| ops | — never | — | **deferred to r2** |
| accessibility | — never | — | **deferred to r2 (dashboard UI)** |

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

**Phase 2 — MED + LOW (PR TBD, `fix/audit-rest`):**
- #4 no cross-process state lock → new `filelock.py` (portable `O_EXCL` lock-file + token + 15s stale-steal + 30s `LockTimeout`); wraps RMW in `quarantine.py` (registry+office), `project.py` (registry), `manifest.py` (`refresh_*`). **Fixed for those callers.** *Residual: `executor.apply`/`install.py`/`adopt.py` global-scope manifest writes are still unlocked — out of footprint (follow-up).*
- #5 `--force` hook restore duplicates → `merge_hooks` force path now replaces (bounded to `len(diverged)`). **Fixed.**
- #6 office-quarantine trusts just-pulled state → `seed_office_baseline_if_absent` records the pre-pull tree as baseline before the FF. **Fixed.**
- #7 `tomllib` breaks 3.10 floor → guarded import + minimal `[[gaps]]` fallback parser; CI matrix adds 3.10. **Fixed.**
- #8 `jsonschema` dead runtime dep → moved to dev extras (confirmed zero runtime imports). **Fixed.**
- #9 signed-update tip-only → docstring clarified: FF-only means the signed tip's hash chain commits to `HEAD..tip`, so tip verification is sound (doc fix, not a vuln). **Fixed.**
- LOW: egress fail-open on ≥4-space indent (`^[ \t]*`), stale `compile.py`/`quarantine.py` "no-op" docstrings, dangling-symlink uninstall (readlink-string match only), `.git` Windows trailing-dot dodge (`rstrip(". ")`), `session_recall` mislabel + `working_capture` marker-before-record, `preserve`-absent SCAFFOLD → `True`, lint hyphenated `in-flight`. **Fixed.**

**Tracked follow-ups (filed, not in either PR):**
- `executor.apply` global-manifest lock (completes #4 for the main manifest writer).
- #11 bwrap-confinement test is CI-skip-gated where `bwrap` absent.
- Deferred consult-gpt items: codex reads outside its worktree; a *user-local* grok binary path isn't bwrap-jailed; no wire-byte cap on egress payload.
- Floating deps remain (`PyYAML`/`pytest` unbounded; no lockfile) — supply-chain, next rotation.

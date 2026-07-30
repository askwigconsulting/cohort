# Cohort — Design history & rationale

The "why" the code can't carry: the architectural spine, the cross-phase decisions
(referenced as `[J]`–`[S]` and a few inline ones), the verified environment facts,
and the one through-line that makes the safety story real. Read this before
proposing changes — several of these were arrived at by catching a guarantee that
was *asserted* rather than *proven*, and the fix each time was to make the property
structural and testable.

## The spine

- **Canonical is law.** Every artifact (agent/skill/command/hook/memory/context) is
  authored once as schema-validated markdown-with-frontmatter under `canonical/`.
  Per-IDE adapters compile it; **compiled outputs are derived and never hand-edited.**
- **compile → stage → ops.** Renderers turn the IR into byte-stable files staged under
  `~/.cohort/compiled/<ide>/`; the Phase-1 executor *places* them (link/copy/merge/
  scaffold). Renderers are pure producers; the executor is the only mutator.
- **IR is the shared contract.** All three IDE renderers consume the unchanged IR — a
  missing field is an IR change reviewed separately, never a per-adapter hack. This is
  what made Codex/Cursor "one more descriptor each" (Phase 7).
- **The executor is base-parameterized.** `CohortPaths(base=…)` is `$HOME` for the
  global office and the repo root for a project — the same incremental, fsync'd,
  ownership-checked machinery runs at either scope.
- **Determinism everywhere on the compile path.** Byte-stable output, golden tests,
  recompile idempotency, and the merge ownership model all depend on it. Runtime LLM
  steps are therefore banned on the compile path (see [N]/optimizer below).

## Cross-phase decisions

- **[J] Shared-canonical mode is fixed at first install.** The global `canonical`
  artifact's op type (link vs copy) is set once; a later `--copy` applies only to new
  per-IDE ops and never re-flips the shared artifact (which would self-clobber). The
  manifest `mode` is informational; **per-op type governs reversal**, never `mode`.
- **[K] Ownership identity is a content hash, never an in-file marker.** Merge blocks
  record a `block_hash`; key-merged JSON entries record per-entry `entry_hash`. Cohort
  recognizes its own content by hash, so nothing non-schema is injected into a user's
  file, and **divergence detection falls out for free**: a user edit changes the hash,
  so reverse/re-merge can't claim it → skip + warn. (Chosen over a `_source: cohort`
  marker, which would have assumed extra-key tolerance the docs don't guarantee.)
- **[L] Delete-if-only-ours.** A Cohort-*created* file is removed on reverse only if
  removing Cohort's content leaves nothing. A file the user added their own content to
  is kept (Cohort's block/entries removed). Never delete user data.
- **[M] Project context is wired via `@import`.** Verified that Claude `@import` works
  at project scope; `init` writes a one-line managed `@import ../.cohort/project_context.md`
  block into `<repo>/.claude/CLAUDE.md` (path relative to that file). The corpus stays
  the editable shared file; the user-file footprint is one line. Inline fallback if a
  scope ever lacks `@import`.
- **[N] Divergence is recoverable via an explicit `--force`.** Passive runs respect a
  user's edit/removal of a managed block or entry (skip + warn, never re-add/overwrite);
  `--force` (`init`/`context refresh`/`recompile`) deliberately re-asserts Cohort's
  content. **Every divergence warning names its own restore command** — an accidental
  deletion is recoverable, not a dead end. Phase-7 adapters inherit this.
- **[O] ChiefOfStaff project-awareness — delivered and verified.** The mechanism is
  a "Project specialists" roster kept current in each repo's `project_context.md`
  (`managed_context_block`, #24), `@import`ed into `<repo>/.claude/CLAUDE.md` by
  `init` — **not** a per-repo clone/override of the global generalist. The load-bearing
  assumption (that a subagent actually *receives* that project memory) was
  asserted-not-proven; it is now **verified against the Claude Code docs**: a custom
  subagent inherits every level of the memory hierarchy the main conversation loads —
  user + project `CLAUDE.md` and its `@import`s — *except* Explore and Plan, which skip
  it. So ChiefOfStaff (a custom subagent) receives the project roster at spawn. Its body
  reflects this with a confident routing rule (project specialists are first-class and
  override a same-named global one for that repo), locked by a test so the wording can't
  regress to a no-op instruction. See the verified-facts entry below.
- **[P] The global scope is two layers: the office and my office (#84).** Personal
  content lives in the machine-local overlay `~/.cohort/my/canonical/`, merged
  *additively* over the source clone's canonical at compile time — a `(kind, name)`
  collision refuses; override-by-name is deferred to an explicit `cohort personalize`.
  The overlay is passed to `compile_ide` explicitly by callers, never derived from
  `Path.home()` inside compile (in-process goldens stay hermetic); the project tier
  never receives one. The roster subset filters the **office layer only** — a personal
  agent was opted in by authoring it, so an update-driven recompile with a tailored
  roster can never prune it. `update` and `uninstall` never touch `my/`. Precedence is
  honest: my-over-office is resolved at compile into one flat `~/.claude/agents/`;
  only project-over-user is resolved by Claude Code at runtime. This is the
  machine-local overlay #42 deferred — it ends personal authoring dirtying the clone.
- **[Q] Pulled auto-activating artifacts are quarantined until reviewed (#107).**
  `my-office sync` fast-forward-pulls the overlay from a Git remote; on a shared
  remote, a pushed **hook** (runs on IDE events) or **memory** (loads into every
  session) is a code/prompt-injection sink. Sync records the `(kind, name,
  content-hash)` identity of every gated artifact a pull introduced under
  `~/.cohort/state/quarantine.json`, and the *single compile chokepoint* withholds
  exactly those identities from **every** recompile — not just sync's — so no later
  `update`/`add-agent`/`edit` silently activates them. Content-addressed, so
  approving pins the reviewed bytes (a re-pull with new content re-quarantines);
  the pull-delta window excludes local authoring (committed after the merge). The
  quarantine is derived from the overlay's sibling `state/` dir, preserving compile
  hermeticity. Cleared by an explicit `cohort my-office review` + `approve`. Skills
  are out of scope (advisory text, not an execute-on-event sink); agents/commands
  likewise. Supersedes the incomplete "exclude hooks at sync time" of #103/PR #106.
- **[R] Model tier is an abstract cost/latency hint, resolved per renderer (#143).**
  Canonical agents may declare `model: fast|default|top`; canonical never names a
  concrete model ID, so it stays IDE-agnostic and doesn't rot when model names
  change. Each renderer owns exactly one tier→model table: Claude's maps
  `fast→haiku`, `top→opus`, and **omits the field for `default`** (Claude's own
  behavior with no `model:` key is to inherit the conversation's model, which is
  what "default" means, so there's nothing to spell out). Codex/Cursor have no
  doc-verified per-agent model key yet, so both omit the field gracefully rather
  than guess — no compile break, consistent with every other doc-cited mapping in
  those renderers. `cohort adopt`/import maps a concrete model name found in the
  wild (`opus|sonnet|haiku`, substring-matched) to its nearest tier and drops
  anything unrecognized; it never emits a value outside the schema's enum, so
  adoption can't produce a schema-invalid artifact. Purely a hint — no behavioral
  semantics, fail-closed like every other enum (`_check_enum`, E020). The concrete
  tier → model mapping (both this agent field and `/crew`'s routing tiers) is
  documented in one place — `docs/model-tiers.md` — and `cohort lint` fails if that doc
  drifts from `_MODEL_MAP` or lists an orchestration tier the canon no longer uses.
- **[S] Multi-model orchestration is instruction-level, coordinator-verified, and
  vendor-extensible.** `/crew` (with the `model-orchestration` + `fable-mode`
  memories) is prose in canonical artifacts, not a CLI code path — Cohort adds no
  scheduler; the coordinating Claude session *is* the scheduler. Two invariants keep it
  safe. **A coordinator tier floor:** orchestration runs only on Fable (preferred) or
  Opus, never below, because decomposition/routing/adversarial-signoff are exactly the
  judgments a lower tier gets wrong; an Opus coordinator escalates a genuinely
  Fable-suited task to the human rather than absorbing it. **Coordinator verification is
  the gate:** every worker's output — Claude subagent or external model — is an untrusted
  claim the coordinator re-verifies (re-run tests, read the diff) before signoff, and the
  human's commit/PR review is unchanged. Cross-vendor collaboration enters here on the
  office's terms: `/consult-gpt` runs ChatGPT read-only and advisory (Codex CLI), an
  opinion to verify, never instructions to execute. Extending non-Claude models from
  advisors to *orchestrated doers* (producing worktree-isolated, coordinator-verified
  diffs) is deliberately gated behind **RFC 0004 (#171)** because it crosses the
  "advisory by default" invariant — an engine registry, not hardcoded vendors, is the
  intended shape. These orchestration invariants — the ≤10-in-flight cap, worker
  footprint-disjointness, honest signoff — are coordinator discipline plus the human PR
  gate *by design*, not runtime-enforced, because each binds live execution (how many
  agents are actually running, which files a worker really writes, whether signoff
  re-ran the tests) and the only artifacts Cohort compiles are static. A declared
  orchestration graph was evaluated and rejected: a static validator's cap check is
  vacuous (nodes are protocol roles, not live instances), its footprint check is
  impossible (a task's real writes are unknowable before it runs), and its signoff check
  only duplicates an existing wording-lock — and a `PreToolUse` hook counter was rejected
  too (install-global, so it throttles legitimate non-crew parallelism, and its state
  goes stale into deadlock on a crashed subagent). Enforcing them at all would require
  Cohort to become the scheduler it deliberately isn't. What *is* mechanically enforced
  is the one thing that can be without a runtime: `cohort lint` single-sources the cap
  *number* so the canon cannot drift on it (`docs/model-tiers.md`).

## Other load-bearing rules

- **Advisory is enforced at render time, per IDE** — Claude tool-strip (read-only tool
  set), Codex `sandbox_mode = "read-only"`, Cursor `readonly: true`. The safety
  invariant becomes a property of the *compiled* agent, not just the canonical one; the
  renderer strips mutating tools even if canonical requests them — **except a
  `scope: project` doer** (`advisory: false`), which keeps its write/exec tools. The
  relaxation is gated in one place, `is_doer(ir)` (`scope == "project" AND advisory is
  False`), which all three renderers call, so they cannot drift and a synced (`global`)
  tier can never emit a doer. The schema enforces the same rule fail-closed
  (`advisory: false` rejected unless `scope: project`; a doer must declare `tools`);
  `promote` refuses to lift a doer into a synced tier (defense-in-depth, not the schema's
  transitive re-check alone); and `do_install_project` discloses placed write-capable
  agents (flagging `Bash`) so a doer is never a silent surprise on a teammate's clone.
  Rationale: a project agent crosses no sync/trust boundary beyond "you already trust
  code in a repo you cloned"; a doer in a *synced* tier would be an RCE vector on every
  install that pulls it — the surface `signed_by` pinning and the my-office quarantine
  exist to contain.
- **Delegation is a derived invariant.** ChiefOfStaff's office directory is generated
  from the roster at compile time (the `<!-- cohort:office-directory -->` marker); a
  generalist missing it — or a specialist carrying it — is a compile error, so the
  wiring can't silently drift.
- **Parity is coverage, not byte-diff.** For each canonical kind targeting an IDE, it is
  rendered or a declared gap (`adapters/<ide>/parity-gaps.toml`); the check fails on an
  *undeclared* gap and on a *stale* declaration. Claude is the reference coverage set.
- **The optimizer is a dev-time aid, never a runtime step.** A runtime LLM optimizer
  would make output non-deterministic, breaking goldens *and* the [K] hash-based merge
  model (every recompile would read as divergence). The Steward's *proposal* synthesis
  may be LLM-driven precisely because it never touches the compile/merge path — and even
  there the CLI core is deterministic, with the LLM an optional enrichment seam.
- **The self-improvement loop is human-gated by construction.** `propose-improvement`
  and `submit-proposals` cannot edit `canonical/` (proven by tree-hash) and cannot merge
  or push a default branch (proven by a recording fake git/gh that fails on any merge or
  `main`/`master` push); PRs are drafts. Promotions and improvements share one
  `proposals/` format and one submit gate.
- **`distill` is the one memory-loop child that writes context — its shape is its safety
  model (#144).** It compounds recent `sessions/` + `feedback/` (never `reports/`) into
  project memory, and stays safe by construction, not by policy: (1) **append-only, dated,
  outside the managed block** — each run appends `## Distilled (YYYY-MM-DD)` at the end of
  `project_context.md`, after the block `refresh_project_context` regenerates *in place*, so
  distilled content survives `context refresh`; (2) **user-owned on write, not hash-owned** —
  append-only is what prevents clobbering, so a later hand-edit never forks (a `[K]`-style
  skip+warn would permanently fork on the first edit — the wrong semantic for memory meant to
  be edited); (3) **extractive, never rewritten** — every line quotes a source record and
  cites file + date, because `sessions/`/`feedback/` are contributor-writable *untrusted*
  input and the confirm diff (control-chars escaped so ANSI can't disguise a line) is the
  security gate; (4) **confirm-gated and fail-closed** — a real write needs an affirmative
  confirm; no confirm callback (an unattended/hooked path) never writes, and it is wired to no
  hook. Deterministic (no LLM, no network), preserving the compile/merge invariant; an
  LLM-written distill can still arrive interactively via `/snapshot`.
- **Generated text never breaks frontmatter.** All metadata writers emit frontmatter via
  `yaml.safe_dump` (safe by construction — quoting is the serializer's job, not a
  heuristic); `check_frontmatter_safety` is the CI lint, proven against a seeded bad
  fixture.
- **`update`'s trust boundary is the upstream repo, not the transport (#30).** A
  fast-forward runs the pulled commits' `pip install -e` and compiles their artifacts, so
  a *compromised upstream* — a malicious commit that is still a valid fast-forward — is
  the residual risk once git is non-interactive/credential-disabled and only a clean ff of
  a user-configured upstream is applied. Opt-in `[update] require_signed` gates the merge
  behind `git verify-commit` (an `unsigned` refusal, exit 1), fail-closed on any
  unverifiable/unsigned/error case. Three properties make it real rather than theatre:
  (1) the tip is resolved to a SHA *once* and that same SHA is summarized, verified, **and**
  merged — so a concurrent fetch can't slip an unverified child between check and apply
  (TOCTOU), and an option-like upstream from a tampered config can't be read as a flag;
  (2) *enablement* fails closed — a stdlib scanner (not `tomllib`, absent on the 3.10 floor)
  reads the flag so it can't silently no-op, and a present-but-unreadable config refuses
  rather than disabling the gate; (3) it is honestly scoped — `verify-commit` proves "signed
  by a key git trusts," not "signed by the maintainer," so `[update] signed_by = ["SHA256:…"]`
  (#105) is the strict tier: it additionally requires the tip's signing key to match a pinned
  fingerprint (a whole-token match against the *signing-key field* of `verify-commit --raw`,
  never signer-controlled free text), and a non-empty list implies `require_signed`. The docs still steer users to also pin `gpg.ssh.allowedSignersFile`.
  Default-off keeps clone-and-go unchanged.

## Verified environment facts (doc-cited; re-confirm on drift)

- Claude subagent `tools` is a comma-separated string; omitting it inherits **all** tools
  (so advisory must emit an explicit read-only set). Read-only: `Read, Grep, Glob,
  WebFetch, WebSearch`.
- Claude `@import` works in global and project memory; relative-to-file; recursive depth 4.
- Claude settings hooks: `{hooks: {<Event>: [{matcher, hooks:[{type:"command",command}]}]}}`.
- **Project-level subagents override user-level** on a name collision (priority 3 > 4) —
  the basis for the shadow-name warning.
- **A custom subagent inherits the full memory hierarchy** the main conversation loads —
  user + project `CLAUDE.md` and its `@import`s — *except* Explore and Plan, which skip
  CLAUDE.md (and git status) for speed. This is what makes ChiefOfStaff project-aware ([O]):
  the repo's `@import`ed `project_context.md` reaches the subagent's context at spawn. (Docs:
  code.claude.com/docs sub-agents "what loads at startup".)
- Codex: per-file subagents `.codex/agents/*.toml` with `sandbox_mode`; skills under
  `.agents/skills/`; `config.toml` is TOML (managed-block), `hooks.json` is JSON
  (key-merge). Custom prompts are deprecated → `command` is a declared gap.
- Cursor: per-file subagents `.cursor/agents/*.md` (`readonly`); `.cursor/rules/*.mdc`;
  `hooks.json` JSON.

## Platform support (Windows)

The CLI is cross-platform (pathlib, `Path.home()`); three POSIX assumptions are handled
explicitly so native Windows works:

- **Placement mode.** Symlinks are the POSIX default but need Developer Mode/admin on
  Windows, so `resolve_mode()` (`install_model.py`) defaults to **copy** on `os.name == "nt"`.
  Copy-mode is a full functional substitute — it never exercises a symlink code path.
- **Directory fsync.** `manifest.persist()` keeps the file fsync but skips the POSIX
  directory fsync on Windows (`os.open` on a directory fails there; it's a no-op concept).
- **Installer.** `installer/bootstrap.ps1` is the PowerShell counterpart to `bootstrap.sh`
  (Windows venv layout `\.venv\Scripts\`). `.gitattributes` forces LF checkout so the
  byte-stable golden trees hold cross-platform. CI runs the suite on `windows-latest`.
- **Reach.** The office is Claude Code subagents; the Claude **Desktop chat** app reads only
  the compiled *skills*, not subagents — Cohort ships `office-guide` so that path is not
  empty — Windows users get the full office via Claude Code.

## The through-line

The data-safety model that began as the installer's **clobber rule** (refuse to
overwrite a foreign file; back up under `--force`) is, ten phases later, the same model
that structurally stops the office from rewriting itself. It generalized: clobber refusal
→ ownership-checked reverse ([K]/[L]) → divergence-respecting re-merge with an explicit
restore path ([N]) → the human gate on self-improvement. The office can't rewrite itself
not because anyone promised it wouldn't, but because a test fails if it tries.

## Tracked execution gaps (outside the design)

1. **No git remote — RESOLVED (2026-06).** The repo lives at
   `github.com/askwigconsulting/cohort`; CI (ubuntu + windows) runs on every push/PR and
   the loop's draft PRs are live. Kept as context for gap 2.
2. **Codex/Cursor golden-lock (#23).** The renderer *structure* is tested and the bytes
   are regression-locked against the renderers' own output. As of 2026-07-06 the Cursor
   layout/formats (rules `.mdc` + `alwaysApply`, plain-markdown commands,
   `skills/<name>/SKILL.md`, `agents/` with a real `readonly`) and the canonical→
   Codex/Cursor hook-event names are **doc-verified against the current official docs**
   (cursor.com/docs, developers.openai.com/codex) and locked by `test_hook_events`; the
   Codex map was corrected from Cursor-style camelCase to Codex's PascalCase vocabulary.
   The Codex layout is now doc-verified too: subagents `.codex/agents/<name>.toml`
   (MATCH), skills `.agents/skills/<name>/SKILL.md` (MATCH), `.codex/hooks.json` schema
   (corrected to Codex's matcher-group form, no `version`), and `.codex/AGENTS.md`
   (correct for the global tier — resolves to `~/.codex/AGENTS.md`, Codex's global
   instructions path; a project-tier codex install would need repo-root `AGENTS.md`,
   not a current path). All formats now rest on official docs rather than being
   doc-cited-and-possibly-stale; a **real install** would only add byte-level
   belt-and-suspenders. Codex/Cursor stay experimental; shipped hooks/memories target
   `[claude]` only, so those paths are latent until a codex/cursor artifact is authored.

The fitting first trip through Cohort's own loop is the proposal that closes gap (2).

## Withdrawn RFCs

- **RFC 0003 (Personal agentic OS / life)** was withdrawn in v0.7.0, superseded by a dedicated standalone app. The life project feature has been removed from Cohort; do not restore it to the codebase.

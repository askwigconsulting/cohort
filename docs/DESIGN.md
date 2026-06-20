# Cohort — Design history & rationale

The "why" the code can't carry: the architectural spine, the cross-phase decisions
(referenced as `[J]`–`[O]` and a few inline ones), the verified environment facts,
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
- **[O] ChiefOfStaff project-awareness is deferred, mechanism anchored.** Project
  specialists are directly invocable and shown by `status`, but not yet in the global
  generalist's routing. When added, inject a "Project specialists" list into the
  already-loaded project memory (the Phase-3 directory injection at project scope) —
  **not** a per-repo clone/override of the global generalist.

## Other load-bearing rules

- **Advisory is enforced at render time, per IDE** — Claude tool-strip (read-only tool
  set), Codex `sandbox_mode = "read-only"`, Cursor `readonly: true`. The safety
  invariant becomes a property of the *compiled* agent, not just the canonical one; the
  renderer strips mutating tools even if canonical requests them.
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
- **Generated text never breaks frontmatter.** All metadata writers emit frontmatter via
  `yaml.safe_dump` (safe by construction — quoting is the serializer's job, not a
  heuristic); `check_frontmatter_safety` is the CI lint, proven against a seeded bad
  fixture.

## Verified environment facts (doc-cited; re-confirm on drift)

- Claude subagent `tools` is a comma-separated string; omitting it inherits **all** tools
  (so advisory must emit an explicit read-only set). Read-only: `Read, Grep, Glob,
  WebFetch, WebSearch`.
- Claude `@import` works in global and project memory; relative-to-file; recursive depth 4.
- Claude settings hooks: `{hooks: {<Event>: [{matcher, hooks:[{type:"command",command}]}]}}`.
- **Project-level subagents override user-level** on a name collision (priority 3 > 4) —
  the basis for the shadow-name warning.
- Codex: per-file subagents `.codex/agents/*.toml` with `sandbox_mode`; skills under
  `.agents/skills/`; `config.toml` is TOML (managed-block), `hooks.json` is JSON
  (key-merge). Custom prompts are deprecated → `command` is a declared gap.
- Cursor: per-file subagents `.cursor/agents/*.md` (`readonly`); `.cursor/rules/*.mdc`;
  `hooks.json` JSON.

## The through-line

The data-safety model that began as the installer's **clobber rule** (refuse to
overwrite a foreign file; back up under `--force`) is, ten phases later, the same model
that structurally stops the office from rewriting itself. It generalized: clobber refusal
→ ownership-checked reverse ([K]/[L]) → divergence-respecting re-merge with an explicit
restore path ([N]) → the human gate on self-improvement. The office can't rewrite itself
not because anyone promised it wouldn't, but because a test fails if it tries.

## Tracked execution gaps (outside the design)

1. **No git remote.** CI is committed but can't run; there's no backup. Standing item:
   `gh repo create cohort --private --source=. --remote=origin --push`, then push all
   branches. This also unblocks the loop's live PRs.
2. **Codex/Cursor golden-lock.** The renderer *structure* is tested; the concrete bytes
   are doc-cited, not locked against a real install. Field-level `‹verify›` remaining:
   the canonical→Codex/Cursor hook-event names and the exact Cursor frontmatter/skills
   dir. CI runs coverage/structure/stability until the bytes lock.

The fitting first trip through Cohort's own loop is the proposal that closes gap (2).

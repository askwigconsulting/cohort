# Changelog

All notable changes to Cohort are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While Cohort is pre-1.0, a minor bump may include breaking changes.

> Note: the `version:` field on a *canonical artifact* is a separate, per-artifact
> schema concept (it defaults to `0.1.0`) and is unrelated to these package releases.

## [Unreleased]

### Security
- `cohort my-office sync` no longer auto-activates a pulled hook or memory. A
  sync now quarantines every gated artifact (**hooks**, which run on IDE events,
  and **memories**, which load into every session's corpus) that the pull
  introduced or changed, recording its `(kind, name, content-hash)` identity under
  `~/.cohort/state/`. The withhold is durable and IDE-agnostic — *every*
  `compile_ide` (not just the sync recompile) holds those exact artifacts back
  until you clear them with `cohort my-office review` + `cohort my-office approve`.
  Closes the shared/multi-writer-remote RCE path where a teammate's pushed hook or
  prompt-injecting memory would otherwise activate on your next sync with no
  review. Locally-authored artifacts are never quarantined (they are committed
  after the pull, outside its delta). (#107)
- `cohort update` gains opt-in signed-commit verification: `[update]
  require_signed = true` in the global `cohort.toml` gates the fast-forward behind
  `git verify-commit` on the resolved upstream tip, refusing (`unsigned`, exit 1)
  unless the commit is signed by a key git trusts. Closes the residual
  compromised-upstream risk once transport and local config are trusted. Default
  stays off — the common clone-and-go flow is unchanged. (#30)
- `cohort update` adds an identity-pinned tier: `[update] signed_by = ["SHA256:…"]`
  additionally requires the upstream tip's *signing key* to match a pinned
  fingerprint (matched against `git verify-commit --raw`), not merely any key git
  trusts — closing the "signed by someone I trust ≠ signed by the maintainer" gap.
  A non-empty `signed_by` implies `require_signed`; fail-closed throughout. (#105)

### Fixed
- Codex hook-event names were Cursor-style camelCase (`sessionStart`, `preToolUse`),
  which Codex does not recognize. Corrected to Codex's PascalCase vocabulary
  (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, …), verified against the
  official docs and locked by tests. Also fixed the Cursor `post_command` mapping
  (`afterFileEdit` → `afterShellExecution`). Latent until now — shipped hooks target
  `[claude]` only — but wrong for any codex/cursor-targeted hook. (#23)

### Added
- `cohort dashboard` — a lightweight, loopback-only web dashboard (stdlib HTTP
  server, zero new dependencies) showing wiring & health (IDE placement, source-link
  health, canonical↔compiled parity, version vs upstream), the roster, and the
  improvement loop (signals, feedback, proposals, sessions). Actions (feedback,
  prune specialist, propose improvement, snapshot) call the same human-gated
  command functions as the CLI; every `/api` call requires a per-launch token and
  a loopback Host header. (#49)
- `cohort remove-specialist` — prune a project specialist: canonical source,
  compiled output, placed artifact, and its manifest records, with the executor's
  ownership checks (a user-repointed link is never clobbered). (#49)
- `cohort setup` — a guided first-run interview (company Cohort repo as the
  office's upstream, IDE selection, roster subset), fully flag-driven for
  scripted installs. A tailored roster persists on the manifest and survives
  `cohort update` recompiles; `--agents all` restores the full office. (#51)
- `/office-setup` and `/project-setup` — compiled interview commands. The first
  tailors the global office (office-context memory + human-reviewed custom
  agents); the second interviews the team about a repo, fills
  `project_context.md`, and scaffolds specialists with real content. (#51)
- `add-specialist --body-file` — supply the agent body (e.g. from an interview)
  instead of the "_edit me_" template; frontmatter stays generated so
  `advisory: true` and project scope cannot be overridden. (#51)
- Stale placed-artifact cleanup, scoped to the compile-then-install callers
  (`recompile`/`setup`/`update`): artifacts a fresh compile no longer produces (an
  agent dropped from a tailored roster, or one deleted upstream) are reversed
  (ownership-checked) and pruned from the manifest. Plain `install` never prunes,
  a dry-run reports the removals it would make, and a `--force` backup displaced at
  a pruned dest is restored rather than stranded. (#51)
- `cohort my-office sync` — back the personal layer (`~/.cohort/my`) with a Git
  remote so personal agents/skills/settings follow you across machines. It
  reconciles with the remote before committing local changes (fast-forward only,
  so a fresh machine adopts the shared history and a diverged one is refused for
  you to reconcile), pushes, and recompiles so anything pulled is placed. `cohort
  status` now surfaces each tier's source remote (office / my office / project). (#101)

### Security
- Agent/specialist scaffolds emit frontmatter through the safe YAML serializer and
  reject control characters in the display fields, closing a frontmatter-injection
  that could append `advisory: false` + write tools and escape the read-only
  advisory sandbox; `add-specialist` now validates before staging (fail-closed). (#51)

## [0.3.0] — 2026-06-27 · Self-update

Cohort can now keep itself current and learn from the projects that use it.

### Added
- Advisory update-check on session start — a throttled (once/UTC-day), read-only
  "N commits behind" notice. Never blocks a session and always exits 0. (#10, #28)
- `cohort update` and the `/update` command — fast-forward the clone, reinstall the
  package only when its dependencies change, and recompile every installed IDE.
  Refuses a dirty or diverged tree; `--dry-run` previews; nothing is applied
  silently, and only a clean fast-forward is ever taken. (#29)
- Cross-project upstream learning — a generality heuristic flags project-agnostic
  proposals as upstream candidates, and `cohort submit-proposals --upstream` opens
  sanitized draft PRs back to the upstream Cohort repo. (#32)

### Security
- `git fetch`/`push` calls that consume a config-supplied remote are guarded with a
  `--` end-of-options separator, closing an argument-injection / RCE vector via a
  tampered `cohort.toml`. (#10, #32)
- Upstream proposals are scrubbed of project markers (repo slug, project specialists,
  user-home paths, emails, secret-shaped tokens) before any PR; the human PR review
  remains the publish gate. (#32)

## [0.2.0] — 2026-06-26 · Platform & hygiene

### Added
- Native Windows support for the Claude install path — copy-mode default, UTF-8
  console safety, and CI on `windows-latest`. (#3, #4)
- OSS hygiene — `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, and issue/PR
  templates. (#2, #27)

### Changed
- Hardened `submit-proposals` and the self-improvement loop's safety boundary. (#2)

## [0.1.0] — 2026-06-23 · Initial harness

The first working Cohort: a portable, multi-IDE "agentic office" you install into a
repo, compiled from a single canonical source.

### Added
- Canonical artifact schema and `cohort validate` (Phase 0).
- Install engine with IDE selection — idempotent, reversible, `--dry-run` (Phase 1).
- Compile pipeline and a byte-stable Claude reference adapter (Phase 2).
- Office roster v1 with ChiefOfStaff directory injection (Phase 3).
- Project home, sessions, and staleness tracking (Phase 4).
- Commands and reporting — `add-agent`, `status`, weekly/monthly reports (Phase 5).
- Project specialists and the project/global isolation boundary (Phase 6).
- Codex and Cursor adapters behind a parity gate (Phase 7).
- Self-improvement loop (Steward) — feedback → propose → draft PR; never auto-merges,
  never edits canonical (Phase 8).
- Design notes (`docs/DESIGN.md`), a worked example, CI, and end-to-end tests (Phase 9).

[Unreleased]: https://github.com/askwigconsulting/cohort/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/askwigconsulting/cohort/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/askwigconsulting/cohort/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/askwigconsulting/cohort/releases/tag/v0.1.0

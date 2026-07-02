# Changelog

All notable changes to Cohort are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While Cohort is pre-1.0, a minor bump may include breaking changes.

> Note: the `version:` field on a *canonical artifact* is a separate, per-artifact
> schema concept (it defaults to `0.1.0`) and is unrelated to these package releases.

## [Unreleased]

### Added
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
- Stale placed-artifact cleanup on install: artifacts that leave staging (an
  agent dropped from a tailored roster, or deleted upstream) are now reversed
  (ownership-checked) and pruned from the manifest instead of dangling. (#51)

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

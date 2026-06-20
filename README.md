# Cohort

A portable, self-improving, multi-IDE **agentic office** — a virtual organization of
company-function specialist agents (HR, Legal, Compliance, Security, Finance, IT, Comms,
Procurement, Privacy, Program, cloud architects, and a Chief-of-Staff orchestrator) you drop into any
repository. Authored once in an IDE-agnostic **canonical** form, then compiled into **Claude, Codex,
and Cursor** layouts at install time.

- **Canonical is law.** Every agent/skill/command/hook/memory/context is authored once and
  schema-validated; per-IDE adapters compile it into each IDE's native files. Never hand-edit a
  compiled output — edit canonical and recompile.
- **Advisory by default.** Every office agent is read-only and advisory: it recommends; a human
  decides. The renderer enforces this (Claude tool-strip, Codex `sandbox_mode = "read-only"`, Cursor
  `readonly: true`).
- **Two scopes.** A **global** office roster installed once per machine, plus **project specialists**
  isolated to a single repo.
- **Self-improving, human-gated.** Cohort observes its own usage and *proposes* changes to itself as
  **draft PRs a human reviews and merges** — it structurally cannot edit or merge the harness
  unattended.

## Quickstart

Clone the harness, then walk the new-team journey — each line is a real command:

```bash
git clone <your-cohort-remote> cohort && cd cohort
cohort install --ide claude,codex,cursor
cohort init
cohort add-specialist --name data-modeler --display-name DataModeler --department Data --description 'Schema and data modeling.'
cohort snapshot
cohort weekly-report
cohort feedback --rating up --agent data-modeler
cohort propose-improvement
cohort submit-proposals
```

`install` places the office roster into each selected IDE; `init` scaffolds the per-repo shared context
(`<repo>/.cohort/`) and wires it into project memory; `add-specialist` adds a repo-local advisor;
`snapshot`/`weekly-report` capture and roll up sessions; `feedback` → `propose-improvement` →
`submit-proposals` is the human-gated self-improvement loop (proposals become **draft PRs** — you
review and merge). This command sequence is the project's executable quickstart: it is run verbatim by
the full-system end-to-end test, so the docs cannot drift from what the tool does.

## Scope model

| | Global (office roster) | Project (this repo) |
|---|---|---|
| Lives in | `~/.cohort/` + `~/.claude` / `~/.codex` / `~/.cursor` | `<repo>/.cohort/` + `<repo>/.claude` etc. |
| Holds | the 15-agent roster, hooks, memories | `project_context.md`, `sessions/`, `reports/`, project specialists, `proposals/`, `feedback/` |
| Git-tracked | the Cohort source repo | the consuming repo (except `state/`, `compiled/`) |

## Commands

`validate` · `install` / `uninstall` · `compile` / `recompile` · `init` / `deinit` · `add-agent`
(global) / `add-specialist` (project) · `promote` · `snapshot` · `context refresh` · `status` ·
`weekly-report` / `monthly-report` · `feedback` / `propose-improvement` / `submit-proposals`. Every
command supports `--dry-run`; installs/compiles are idempotent and reversible.

## Stack

CLI/compiler in Python (Typer); bootstrap installer in POSIX sh. Targets macOS / Linux / WSL.

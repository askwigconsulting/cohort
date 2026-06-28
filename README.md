# Cohort

[![CI](https://github.com/askwigconsulting/cohort/actions/workflows/ci.yml/badge.svg)](https://github.com/askwigconsulting/cohort/actions/workflows/ci.yml)

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
git clone https://github.com/askwigconsulting/cohort cohort && cd cohort
python3 -m venv .venv && . .venv/bin/activate   # isolated environment
pip install -e .                                 # puts the `cohort` CLI on PATH
cohort recompile --ide claude,codex,cursor       # compile the roster + place it into each IDE
cohort init
cohort add-specialist --name data-modeler --display-name DataModeler --department Data --description 'Schema and data modeling.'
cohort snapshot
cohort weekly-report
cohort feedback --rating up --agent data-modeler
cohort propose-improvement
cohort submit-proposals
```

`recompile` compiles the office roster from canonical and places it into each selected IDE (it's
compile-then-install; plain `install` only places already-compiled output); `init` scaffolds the per-repo shared context
(`<repo>/.cohort/`) and wires it into project memory; `add-specialist` adds a repo-local advisor;
`snapshot`/`weekly-report` capture and roll up sessions; `feedback` → `propose-improvement` →
`submit-proposals` is the human-gated self-improvement loop (proposals become **draft PRs** — you
review and merge). The `cohort …` command sequence above is asserted equal to the steps the
full-system end-to-end test executes, so the *journey* can't drift from the tool; the `git clone` /
venv / `pip install -e .` setup lines put the `cohort` CLI on your PATH first.

### Windows (PowerShell)

The office is built on **Claude Code's native subagents** (`~/.claude/agents/`), which the
**Claude Code** CLI reads — install it first (`winget install Anthropic.ClaudeCode`, or
`irm https://claude.ai/install.ps1 | iex`). Note: the **Claude Desktop chat app does not read
subagents** — it only sees Cohort's compiled *skills*. To get the full office, use Claude Code.

A stock Windows PowerShell blocks local scripts (`Activate.ps1`, `bootstrap.ps1`) by default, so
allow them once for your user first:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned   # one-time, per user
git clone https://github.com/askwigconsulting/cohort cohort; cd cohort
py -m venv .venv; .\.venv\Scripts\Activate.ps1   # isolated environment
pip install -e .                                  # puts the `cohort` CLI on PATH
cohort recompile --ide claude                     # compile the roster + place it into Claude Code
```

Or run the one-shot bootstrap: `powershell -ExecutionPolicy Bypass -File .\installer\bootstrap.ps1 --ide claude`
(the `-ExecutionPolicy Bypass` avoids the script-blocking prompt). On Windows, Cohort places
**copies** instead of symlinks by default (symlinks need Developer Mode/admin), so no elevation is
required. The remaining journey (`cohort init`, `snapshot`, the feedback loop, …) is identical to the
sequence above.

## Scope model

| | Global (office roster) | Project (this repo) |
|---|---|---|
| Lives in | `~/.cohort/` + `~/.claude` / `~/.codex` / `~/.cursor` | `<repo>/.cohort/` + `<repo>/.claude` etc. |
| Holds | the 15-agent roster, hooks, memories | `project_context.md`, `sessions/`, `reports/`, project specialists, `proposals/`, `feedback/` |
| Git-tracked | the Cohort source repo | the consuming repo (except `state/`, `compiled/`) |

## Commands

`validate` · `install` / `uninstall` · `compile` / `recompile` · `update` · `init` / `deinit` ·
`add-agent` (global) / `add-specialist` (project) · `promote` · `snapshot` · `context refresh` ·
`status` · `weekly-report` / `monthly-report` · `feedback` / `propose-improvement` /
`submit-proposals`. Every command supports `--dry-run`; installs/compiles are idempotent and
reversible. `cohort --version` prints the release.

## Versioning

Releases follow [Semantic Versioning](https://semver.org/) and are recorded in
[CHANGELOG.md](CHANGELOG.md). The session-start update-check advises when a clone falls behind;
`cohort update` (or `/update`) applies a clean fast-forward.

## Stack

CLI/compiler in Python (Typer); bootstrap installer in POSIX sh. Targets macOS / Linux / WSL.

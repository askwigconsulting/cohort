# Cohort

[![CI](https://github.com/askwigconsulting/cohort/actions/workflows/ci.yml/badge.svg)](https://github.com/askwigconsulting/cohort/actions/workflows/ci.yml)

A portable, self-improving, multi-IDE **agentic office** — a virtual organization of
company-function specialist agents (HR, Legal, Compliance, Security, Finance, IT, Comms,
Procurement, Privacy, Program, cloud architects, Engineering reviewers, and a Chief-of-Staff that triages requests to the
right specialist) you drop into any repository. Authored once in an IDE-agnostic **canonical** form, then compiled into **Claude, Codex,
and Cursor** layouts at install time. Claude Code is the reference target; **Codex/Cursor support is
experimental** — the renderers are complete but doc-cited, not yet locked against live installs
(pass `--ide codex,cursor` to opt in).

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

**The guided path** (recommended): clone, run `./installer/bootstrap.sh --ide claude` (venv +
install + compile in one step), then `cohort setup` — it interviews you: *is there a company Cohort
repo to point to* (your org's fork becomes the office's upstream for updates and proposals), *which
IDEs*, and *which agents* (a tailored subset persists across updates; `--agents all` restores the
full roster). Every question has a flag (`--ide`, `--agents`, `--company-url`, `--non-interactive`)
so scripted installs skip the interview. Then, inside your IDE, `/office-setup` (Claude/Cursor)
tailors the office to what you actually do (office context + custom drafted agents,
human-reviewed), and `/project-setup` interviews the team about a repo — filling
`project_context.md` and scaffolding specialists with real content via
`add-specialist --body-file`.

**The scripted journey** — each line is a real command:

```bash
git clone https://github.com/askwigconsulting/cohort cohort && cd cohort
python3 -m venv .venv && . .venv/bin/activate   # isolated environment
pip install -e .                                 # puts the `cohort` CLI on PATH
mkdir -p ~/.local/bin && ln -sf "$PWD/.venv/bin/cohort" ~/.local/bin/cohort  # durable PATH (hooks need it)
cohort recompile --ide claude                    # compile the roster + place it into Claude Code
cohort init
cohort add-specialist --name data-modeler --display-name DataModeler --department Data --description 'Schema and data modeling.'
cohort snapshot
cohort weekly-report
cohort feedback --rating up --agent data-modeler
cohort propose-improvement
cohort submit-proposals
```

The `ln -sf` line matters beyond this shell: Cohort's session-start hooks run `cohort` from inside
your IDE, so the CLI must be durably invocable — a venv activated in one terminal isn't. Any
equivalent works (`pipx install .`, or adding the venv `bin/` to your shell rc).

`recompile` compiles the office roster from canonical and places it into each selected IDE (it's
compile-then-install; plain `install` only places already-compiled output); `init` scaffolds the per-repo shared context
(`<repo>/.cohort/`) and wires it into project memory; `add-specialist` adds a repo-local advisor;
`snapshot`/`weekly-report` capture and roll up sessions; `feedback` → `propose-improvement` →
`submit-proposals` is the human-gated self-improvement loop (proposals become **draft PRs** — you
review and merge; on a fresh clone of the public repo, submitting needs push access or your own
fork: `cohort submit-proposals --repo <you>/cohort`, see CONTRIBUTING). The `cohort …` command
sequence above is asserted equal to the steps the full-system end-to-end test executes, so the
*journey* can't drift from the tool.

### Windows (PowerShell)

The office is built on **Claude Code's native subagents** (`~/.claude/agents/`), which the
**Claude Code** CLI reads — install it first (`winget install Anthropic.ClaudeCode`, or
`irm https://claude.ai/install.ps1 | iex`). Note: the **Claude Desktop chat app does not read
subagents** — it sees only Cohort's compiled *skills* (currently the `office-guide` skill,
which explains the office and points at Claude Code). To get the full office, use Claude Code.

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

## Dashboard

`cohort dashboard` serves a local web view of the office at `http://127.0.0.1:8787`
(`--port` to change, `--no-open` to skip the browser, Ctrl-C to stop). It shows what
the office is wired to (IDE placement, source-link health, canonical↔compiled parity,
version vs upstream), who is on the roster, and what it has recently done (sessions,
feedback, proposals). Buttons for pruning a specialist, rating an agent, proposing an
improvement, and snapshotting a session call the exact same human-gated commands as
the CLI — the dashboard adds no new write paths, and submitting proposals as draft
PRs deliberately stays in the terminal. It is loopback-only, token-guarded per
launch, built on the Python stdlib (no extra dependencies), and dies with Ctrl-C —
no daemon.

## Scope model

| | Global (office roster) | Project (this repo) |
|---|---|---|
| Lives in | `~/.cohort/` + `~/.claude` / `~/.codex` / `~/.cursor` | `<repo>/.cohort/` + `<repo>/.claude` etc. |
| Holds | the 17-agent roster, hooks, memories | `project_context.md`, `sessions/`, `reports/`, project specialists, `proposals/`, `feedback/` |
| Git-tracked | the Cohort source repo | the consuming repo (except `state/`, `compiled/`) |

Project specialists are invoked directly by name; the global Chief-of-Staff routes only the global
roster for now (project-awareness routing is tracked in #24).

## Vocabulary

- **canonical** — the IDE-agnostic source artifacts under `canonical/`; the only thing you edit.
- **compiled / staged** — the per-IDE files rendered from canonical (`~/.cohort/compiled/<ide>/`);
  derived output, never hand-edited.
- **placed** — a staged file linked or copied into the IDE's own directory (`~/.claude/…`).
- **manifest** — the per-tier record of everything Cohort placed (`state/manifest.json`); what makes
  installs reversible.
- **scope** (a.k.a. **tier** in code and PRs) — where an artifact lives: `global` (the machine-wide
  office) or `project` (one repo).
- **kind** — what an artifact is: agent, skill, command, hook, memory, or context.
- **roster** — the set of installed office agents; a tailored subset persists across updates.
- **topology** — `specialist` (advises on one function) or `generalist` (the single ChiefOfStaff,
  which carries the office directory and triages).
- **department** — a display label grouping agents in the office directory.

## Commands

`validate` · `setup` · `install` / `uninstall` · `compile` / `recompile` · `relink` · `update` ·
`init` / `deinit` · `add-agent` / `add-memory` / `adopt` (global) · `add-specialist` /
`remove-specialist` (project) · `promote` · `snapshot` · `context refresh` · `status` · `dashboard` ·
`weekly-report` / `monthly-report` · `feedback` / `propose-improvement` / `submit-proposals`. Every
command supports `--dry-run` (`dashboard`, a read-mostly server, and `relink`, a repair command,
excepted); installs/compiles are idempotent and reversible. `cohort --version` prints the release.

Daily life happens in the IDE — `/feedback`, `/snapshot`, and `/update` wrap the same human-gated
commands; the `cohort` CLI is the plumbing and scripting layer; the dashboard is a viewer.

## Versioning

Releases follow [Semantic Versioning](https://semver.org/) and are recorded in
[CHANGELOG.md](CHANGELOG.md). The session-start update-check advises when a clone falls behind;
`cohort update` (or `/update`) applies a clean fast-forward.

## Stack

CLI/compiler in Python (Typer); bootstrap installer in POSIX sh. Targets macOS / Linux / WSL.

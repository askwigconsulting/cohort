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
- **Advisory by default.** Office agents are read-only advisors: they recommend; a human decides.
  The renderer enforces this (Claude tool-strip, Codex `sandbox_mode = "read-only"`, Cursor
  `readonly: true`). The one exception is a **project-scoped doer**: an agent authored in a repo
  (reviewed via PR, travelling with the repo — no sync boundary crossed) may set `advisory: false`
  to keep write/exec tools. Every **synced** tier — the shared office and your my-office — stays
  advisory-only, so a synced agent can never carry write access.
- **Three levels of config.** **The office** — the shared roster from the Cohort repo (or your
  company's fork); it changes only via `cohort update` and pull requests. **My office** — your
  personal overlay at `~/.cohort/my/`: agents and memories you add for yourself; updates never touch
  it and proposals never include it. **This project** — specialists and context that live in one
  repo (`<repo>/.cohort/`) and travel with it.
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
fork: `cohort submit-proposals --repo <you>/cohort`, see CONTRIBUTING).

The three roll-ups differ by where their output lands: `weekly-report`/`monthly-report` write a
**human report** under `.cohort/reports/`; `propose-improvement` drafts a **harness proposal** the
Steward can turn into a PR; `distill [--days N]` compounds recent sessions **and** feedback into
**durable project memory** — an append-only, dated `## Distilled` section at the end of
`project_context.md` (outside the Cohort-managed block, so `context refresh` never drops it, and
yours to hand-edit once written). Drafting is extractive and deterministic — every proposed line
quotes a source record and cites it (no LLM, no rewriting into instructions). Because `sessions/`
and `feedback/` are git-tracked and **contributor-writable — untrusted input** — `distill` applies
nothing until you confirm a unified diff (control characters escaped so embedded ANSI can't disguise
a line): **the confirm diff is the security gate — review provenance before approving.** The `cohort …` command
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

`cohort dashboard` serves a local mission-control view of the office at
`http://127.0.0.1:8787` (`--port` to change, `--no-open` to skip the browser, Ctrl-C to
stop). It shows *how the office works* — the canonical → compiled → placed → in-session
pipeline with live health at each stage — plus the roster (agents grouped by department)
and a full inventory of every artifact (skills, commands, hooks, memories) across the
office, my office, and this project, each layer-badged. Two office-wide views aggregate
across every initialized project: a recent-activity feed of session records, and
per-agent scorecards — up/down feedback counts, net score, and a last-30-day trend
(ratings are binary, so there is no numeric average) — Cohort's lightweight answer to
agent benchmarking. Both are read-only, pulled from disk at request time, and rendered
as text nodes only. Day-to-day operations run from
the UI: **update**, **recompile**, **re-init**, **creating and editing** any artifact
(agents, skills, commands, hooks) in your personal layer, adding/removing project
specialists, rating agents, snapshots, and improvement proposals. The dashboard has no
mutation logic of its own — every button invokes the same human-gated CLI command behind
a confirm (authoring defaults to *my* office; touching the shared office is an explicit
choice); submitting proposals as draft PRs deliberately stays in the terminal. It is
loopback-only, token-guarded per launch, built on the Python stdlib (no extra
dependencies), and dies with Ctrl-C — no daemon.

## Scheduled research (opt-in)

Cohort ships nothing that runs unattended, but your IDE's own scheduling can consult the
read-only office while you're away — see [docs/scheduled-research.md](docs/scheduled-research.md)
for the permission-scoped recipes, why the output lands in a gitignored, untrusted
`.cohort/reports/research/` folder, and why this is a user-configured IDE exception rather than
a new Cohort write path.

## Scope model

| | The office | My office | This project |
|---|---|---|---|
| Lives in | the source clone's `canonical/` (placed via `~/.cohort/` + `~/.claude` etc.) | `~/.cohort/my/canonical/` | `<repo>/.cohort/` + `<repo>/.claude` |
| Holds | the 17-agent roster, hooks, memories, skills | personal agents/memories (`add-agent`, `add-memory`, and `adopt` land here by default) | `project_context.md`, `sessions/`, project specialists, project memories, `proposals/`, `feedback/` |
| Git-tracked | the Cohort source repo | no — yours to `git init` if you want history | the consuming repo (except `state/`, `compiled/`) |
| Touched by update | fast-forwarded | never | never |

**A project memory travels with the repo.** `cohort add-memory --to project` authors a
`scope: project` memory into `<repo>/.cohort/canonical/memories/`; it compiles to the repo's own
corpus and is `@import`ed into `<repo>/.claude/CLAUDE.md`, so it loads in **every session in that
repo** — and, once committed, in every clone. That makes it louder than a project specialist, so
Cohort **surfaces its git state** rather than gating it: *tracked* means changes are reviewable
(history, PRs); *untracked* (or no git at all) means there's no audit trail. Which is acceptable is
your call — Cohort's job is to make it visible.

A my-office artifact whose `(kind, name)` collides with an office artifact is refused at compile —
unless it is a deliberate override created with `cohort personalize <kind> <name>`, which copies the
office artifact into my office with an override marker (and `status` flags the override if the
office version later changes or disappears). A tailored roster subset filters the office layer only
— your own agents always install.

Project specialists are first-class. When invoked inside a repository, ChiefOfStaff
names project specialists alongside global ones, and they override a same-named
global specialist for that repo. Project specialists can also be invoked directly
by name at any time.

## Vocabulary

- **canonical** — the IDE-agnostic source artifacts under `canonical/`; the only thing you edit.
- **compiled / staged** — the per-IDE files rendered from canonical (`~/.cohort/compiled/<ide>/`);
  derived output, never hand-edited.
- **placed** — a staged file linked or copied into the IDE's own directory (`~/.claude/…`).
- **manifest** — the per-tier record of everything Cohort placed (`state/manifest.json`); what makes
  installs reversible.
- **scope** (a.k.a. **tier** in code and PRs) — where an artifact lives: `global` (the machine-wide
  office) or `project` (one repo).
- **layer** — within the global scope, whether an artifact comes from **the office** (the shared
  source clone) or **my office** (`~/.cohort/my/`, the personal overlay).
- **kind** — what an artifact is: agent, skill, command, hook, memory, or context.
- **roster** — the set of installed office agents; a tailored subset persists across updates.
- **topology** — `specialist` (advises on one function) or `generalist` (the single ChiefOfStaff,
  which carries the office directory and triages).
- **department** — a display label grouping agents in the office directory.

## Commands

`validate` · `lint` · `setup` · `install` / `uninstall` · `compile` / `recompile` · `relink` · `update` /
`rollback` · `init` / `deinit` · `add-agent` / `add-memory` / `add-skill` / `add-command` / `add-hook` /
`adopt` / `personalize` / `edit` / `try` (global) · `add-specialist` /
`remove-specialist` (project) · `promote` · `snapshot` · `distill` · `context refresh` · `status` · `dashboard` ·
`projects` · `weekly-report` / `monthly-report` · `feedback` / `propose-improvement` / `submit-proposals` ·
`engine consult` / `engine propose` · `my-office sync` / `my-office review` / `my-office approve`. Every
command supports `--dry-run` (`dashboard`, a read-mostly server, and `relink`, a repair command,
excepted); installs/compiles are idempotent and reversible. `cohort --version` prints the release.

Daily life happens in the IDE — `/feedback`, `/snapshot`, and `/update` wrap the same human-gated
commands; the `cohort` CLI is the plumbing and scripting layer; the dashboard is a viewer. `/plan`
can end with an opt-in offer to file its decomposed tasks as GitHub issues — nothing is created
without an explicit confirmation naming the target repo, and a `[tracker]` table in
`.cohort/cohort.toml` (`project_owner`, `project_number`) optionally adds them to a project board.
The dev-workflow commands live here too: `/plan` · `/spec` · `/build` (the inner
implement–test–verify loop) · `/test` · `/review` · `/ship`, and `/goal <issue>` — the issue-driven
outer loop that builds on a branch, has an independent judge verify each acceptance criterion
(max 3 rounds), and ends at a **draft** PR a human reviews. `/orchestrate` is the fan-out loop for
larger work: a coordinator-tier session (Fable preferred, Opus a full coordinator too — never
below Opus) researches and plans, routes each task to the cheapest capable model tier
(fable/opus/sonnet/haiku, max 10 agents in flight), and verifies every task itself before signoff.
`/consult-gpt` brings a second vendor's model into the room — an advisory, read-only ChatGPT
opinion via the OpenAI Codex CLI, cross-examined against Claude's own analysis, never executed
blindly. `/code-simplify` reviews recently changed code for reuse, simplification, and
maintainability — reducing complexity without changing behavior.

## Versioning

Releases follow [Semantic Versioning](https://semver.org/) and are recorded in
[CHANGELOG.md](CHANGELOG.md). The session-start update-check advises when a clone falls behind;
`cohort update` (or `/update`) applies a clean fast-forward. If an update misbehaves, `cohort
rollback` returns the office to the version before it (or `cohort rollback --to <tag>` to a specific
release) and recompiles — reversible, since a later `cohort update` restores whatever a rollback
discarded.

## Stack

CLI/compiler in Python (Typer); bootstrap installer in POSIX sh. Targets macOS / Linux / WSL.

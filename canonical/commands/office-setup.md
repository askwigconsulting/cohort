---
name: office-setup
kind: command
scope: global
description: Interview the user and tailor the global office — wiring, context, and custom agents.
targets: [claude, cursor]
invocation: office-setup
dry_run: true
---
Tailor this machine's Cohort office to its owner. You are conducting a short
interview, then driving the `cohort` CLI with the answers. Every change is
shown to the user and approved before it lands; you never write silently.

## 1 — Wiring (skip what `cohort status --json` shows is already done)

Ask, one question at a time:

1. **Company office?** "Does your organization maintain a shared Cohort repo
   (a fork with your company's roster)?" If yes, collect its URL and default
   branch — updates and improvement proposals will flow to it.
2. **IDEs.** Which of claude / codex / cursor they use.
3. **Roster.** Show the roster (`ls <source>/canonical/agents/`) with one-line
   descriptions and ask which agents match their work. A solo developer rarely
   needs the full office; `chief-of-staff` should stay unless they insist.
   `cloud-architect` covers AWS, Azure, and GCP in one advisor — include it only
   if the user runs on a cloud at all (a user on none needs it not); it asks
   which vendor when consulted.

Then run the flags form (never the bare interactive form — you are the interview):

    cohort setup --ide <ides> --agents <subset|all> [--company-url <url> --company-branch <branch>]

After it lands, run `cohort status` and look for `! unmanaged:` lines — pre-existing
agents/commands sitting loose in `~/.claude/`. For each, ask whether to adopt it into
the office (`cohort adopt <path>` — the original is backed up, and adopted agents
become advisory read-only) or leave it unmanaged and invisible to ChiefOfStaff's
directory. Never adopt without asking.

## 2 — Office context (who the advice is for)

Interview briefly: role, domain/industry, primary stack, team size, and any
constraints that should color every agent's advice (regulated industry,
open-source, solo founder, …). Then draft the personal context memory — it
belongs to *my office* (`~/.cohort/my/`), never the shared clone, and its name
is reserved so it can never mask a company-shipped `office-context`:

    cohort add-memory --name my-office-context --description 'Who this office advises.' --body-file <draft.md>

with a body of 5–10 distilled lines (no secrets, no personal data beyond role).
Show the draft and apply their edits before running the command.

## 3 — Custom global agents (optional)

If their domain needs an advisor the roster lacks (e.g. trading-compliance,
clinical-data), draft it: name, department, description, and a four-part body
(Role / Advises on / Boundaries / Escalation). On approval:

    cohort add-agent --name <slug> --display-name <Name> --department <Dept> --description '<desc>'

It lands in my office (`~/.cohort/my/canonical/agents/<slug>.md`) — replace the
scaffolded body there with the approved draft (keep the generated frontmatter —
agents stay `advisory: true`), then `cohort recompile`. Only pass `--to office`
if the user explicitly wants the agent in the shared clone for the whole org.

## 4 — Close out

Tell the user: their personal setup lives in `~/.cohort/my/` — updates never
touch it, proposals never include it, and `git init ~/.cohort/my` gives it
history if they want that. To share an agent with their org later: author it
with `--to office` on a company fork and open a PR. Never commit or push for
them.

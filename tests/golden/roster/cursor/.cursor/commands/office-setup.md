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
open-source, solo founder, …). Then draft
`<source>/canonical/memories/office-context.md`:

    ---
    name: office-context
    kind: memory
    scope: global
    description: Who this office advises — role, domain, stack, constraints.
    targets: [all]
    ---
    <the distilled context, 5–10 lines, no secrets, no personal data beyond role>

Show the draft, apply their edits, then `cohort validate` and `cohort recompile`.

## 3 — Custom global agents (optional)

If their domain needs an advisor the roster lacks (e.g. trading-compliance,
clinical-data), draft it: name, department, description, and a four-part body
(Role / Advises on / Boundaries / Escalation). On approval:

    cohort add-agent --name <slug> --display-name <Name> --department <Dept> --description '<desc>'

then replace the scaffolded body in `<source>/canonical/agents/<slug>.md` with
the approved draft (keep the generated frontmatter — agents stay `advisory: true`),
and `cohort validate` + `cohort recompile`.

## 4 — Close out

The clone is now dirty; `cohort update` refuses a dirty tree. Tell the user to
commit the new canonical artifacts (their clone is theirs), or — on a company
fork — to open a PR so the whole org benefits. Never commit or push for them.

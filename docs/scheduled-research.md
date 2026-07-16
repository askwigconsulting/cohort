# Scheduled research recipes (opt-in, permission-scoped)

Part of [#139](https://github.com/askwigconsulting/cohort/issues/139) — the "runs while you
sleep" layer, daemon-free and honestly framed. Read the framing below before setting anything
up; every recipe depends on it.

## The framing, up front

**Cohort ships nothing that runs unattended.** Office agents are read-only advisors, and the
renderer enforces that by stripping write tools at compile time — Researcher, Steward,
Compliance, and every other specialist are compiled with no `Write` tool. An office agent
cannot save a file to disk, scheduled or not.

So when this page says "the Researcher writes a morning brief," that's shorthand for something
more specific: **the unattended actor is your own scheduled top-level IDE session.** You
configure the IDE (not Cohort) to start a session on a timer; that session consults a read-only
office agent for its judgment, and the *session* — using its own tools, under whatever
permissions you gave it — creates the file. The agent never touches disk. This is a
user-configured IDE feature exception, not a Cohort-owned write path, and it doesn't change the
epic's invariant: **no new unattended write paths.** Cohort's own mutating commands
(`weekly-report`, `propose-improvement`, `submit-proposals`, …) still require a human to invoke
them.

That framing only holds if the scheduled session is actually boxed in. Each recipe below
requires a **restricted permission profile**:

- **Write allowed only under `.cohort/reports/research/`.**
- **Bash, git, and `gh` mutation denied** — the session can read and search, and it can write
  markdown into that one folder; it cannot run commands, commit, push, or open PRs.

**If your scheduling platform can't enforce that restriction, don't run the recipe.** A
schedule that can't be permission-boxed is not a safer version of these recipes — it's a
different, riskier feature. Verify the restriction actually holds (see
[Setting the permission profile](#setting-the-permission-profile)) before trusting a recipe to
run while you're away.

**`.cohort/reports/research/` holds untrusted content.** Anything a scheduled session pulls from
the web and writes there is unreviewed, machine-drafted, web-derived material — treat it like
you'd treat an email from a stranger with a link in it. Concretely:

- **Never `@import` it.** Nothing under `research/` should ever be wired into
  `project_context.md` or any `CLAUDE.md` the way `init` wires the managed project context —
  that would let untrusted web content load into every session automatically.
- **Never paste it into `project_context.md` unreviewed.** If a finding is worth keeping, a
  human reads it, decides what's true, and writes *that* into the tracked context by hand.
- **`cohort distill` (#144) excludes it by design.** Distill compounds session records into
  project-context proposals; report output isn't a session record and isn't in its input set.

**These recipes assume a local install.** They need the office that `cohort recompile` placed
at `~/.claude/agents/` (or your IDE's equivalent) and your project's `.cohort/` on disk. A cloud
sandbox session starts from a fresh clone with no local `~/.claude/`, so the office isn't there
to consult — see [Local install only](#local-install-only) below for which specific feature that
rules out.

## Which scheduling feature to use

Verified against [code.claude.com/docs](https://code.claude.com/docs) (Claude Code's scheduling
comparison table, July 2026). There are three native ways to run Claude Code on a timer, and
they are not interchangeable:

| | [Cloud Routines](https://code.claude.com/docs/en/routines) | [Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks) | [`/loop`](https://code.claude.com/docs/en/scheduled-tasks) |
|---|---|---|---|
| Runs on | Anthropic cloud | Your machine | Your machine |
| Requires machine on | No | Yes | Yes |
| Requires an open session | No | No | Yes |
| Access to local files | **No — fresh clone** | **Yes** | Yes |
| Permission control | **No — runs autonomously, no prompts** | **Configurable per task** | Inherits from session |
| Created via | `/schedule` in the CLI, or the web/Desktop UI | Desktop app → **Routines** → **New routine** → **Local** | `/loop <interval> <prompt>` in a CLI session |

These recipes use **Desktop scheduled tasks** specifically, for two reasons pinned by the
framing above:

### Local install only

Cloud Routines clone the repository fresh into an Anthropic-managed sandbox — there's no
`~/.claude/agents/` there, so there's no office to consult. Confusingly, `/schedule` in the CLI
creates a **Cloud Routine**, not a Desktop scheduled task, so don't use `/schedule` for these
recipes even though it's the command you'd reach for first. Create the task from the **Desktop
app** instead: **Routines** in the sidebar → **New routine** → **Local**.

### Permission scoping

Cloud Routines run "autonomously... there is no permission-mode picker and no approval prompts
during a run" (per the docs above) — you cannot restrict them to a single write path, which
fails this page's core requirement outright. Desktop scheduled tasks, by contrast, have a
per-task permission mode *and* respect the `allow`/`deny` rules in `settings.json`, which is
what makes the restricted profile below possible.

`/loop` doesn't fit either: it requires an open CLI session (or a backgrounded one) rather than
firing on a schedule independent of anything else running, and it inherits whatever permissions
the parent session already has instead of taking its own scoped profile. It's a fine tool for
"babysit this while I'm at my desk," not for "run this while I'm asleep."

### Codex and Cursor

Neither has a scheduling feature that fits this page's local-install requirement today:

- **Cursor Automations** run cloud agents on a schedule — always in the cloud, always Max Mode,
  billed separately. Like Claude's Cloud Routines, there's no locally-placed office to consult.
- **OpenAI Codex's Scheduled Tasks** live in the ChatGPT desktop app and ChatGPT web, not the
  Codex CLI or IDE extension — the Codex docs say so explicitly. If you use them, the same
  "requires the app to stay running to touch local files" constraint applies as Claude's Desktop
  scheduled tasks, but you'd be reimplementing this page's permission-scoping guidance yourself,
  since it's specific to Claude Code's `settings.json` rules.

If Codex or Cursor add local, permission-scoped scheduling to their CLI/IDE surface, this page
should be revisited — until then, these recipes are Claude Code Desktop-only.

## Setting the permission profile

Restrict the task with a `permissions` block in `settings.json` (project `.claude/settings.json`
recommended, so the restriction travels with the recipe rather than living only in your global
config):

```json
{
  "permissions": {
    "defaultMode": "dontAsk",
    "allow": [
      "Write(.cohort/reports/research/**)",
      "WebFetch",
      "WebSearch"
    ],
    "deny": ["Bash"]
  }
}
```

How this works, per [the permissions docs](https://code.claude.com/docs/en/permissions): rules
evaluate deny → ask → allow, first match wins, and **a broad deny cannot carry allow-rule
exceptions** — so don't try to write `deny: ["Write"]` with an allow "opening the path back up";
a bare-tool-name deny removes the tool from the session's context entirely and the allow never
gets evaluated. The boxing-in instead comes from two pieces working together:

- `deny: ["Bash"]` removes the shell tool outright, which is what denies git and `gh` mutation —
  both run through Bash.
- `"defaultMode": "dontAsk"` closes everything else: it auto-denies any tool call not
  pre-approved by an allow rule. The only approvals the session then holds are writes under
  `.cohort/reports/research/` (path relative to the task's working folder) and the web tools the
  research itself needs. Read-only tools (Read, Grep, Glob) never require approval, so
  consulting the read-only office agents still works.

When creating the Desktop task, set the task's own permission-mode picker to the same
deny-by-default mode — the per-task mode and the settings rules should agree, not fight.

One dependency to know about the project-settings recommendation: allow rules in a project's
`.claude/settings.json` **grant** capability, so Claude Code applies them only after you accept
the workspace trust dialog for that folder — until then they are read but not applied (`deny`
rules, which only restrict, are unaffected). Desktop prompts you to trust the working folder
before it will save the task, so accepting that prompt is what puts the project-level allow rule
into effect; it is not unconditional.

**Verify it before trusting it unattended.** Permission-rule precedence has version-specific
edge cases. After creating the task, click **Run now** and confirm in the transcript that a
write outside `.cohort/reports/research/` or any Bash invocation was actually blocked, not just
unprompted. If you can't confirm the restriction holds, that's the "platform can't enforce it"
case from the framing above — don't leave the task scheduled.

## Recipes

Each recipe below assumes the permission profile from the previous section and a Desktop local
scheduled task pointed at your project's working folder. One timing caveat: local tasks only
fire while the Desktop app is open and the machine is awake — a missed run gets a single
catch-up when the machine wakes — so for an overnight morning brief either enable **Keep
computer awake** in Desktop settings or expect the brief to arrive as a wake-time catch-up run.

### 1. Morning brief

- **Schedule:** Daily, e.g. 7:00 AM local.
- **Agent consulted:** Researcher.
- **Instructions (task prompt):** "Consult the Researcher agent for overnight news and
  developments relevant to this project (check `.cohort/project_context.md` for what the
  project is). Write a dated summary with sources to
  `.cohort/reports/research/morning-brief-<YYYY-MM-DD>.md`. Do not run any other commands."
- **Output location:** `.cohort/reports/research/morning-brief-<date>.md`.
- **What the human does with it:** Skim over coffee. If something is worth acting on or worth
  keeping as project context, copy the *specific fact*, verify it yourself, and add it to
  `project_context.md` by hand — the file itself stays untrusted and unreferenced by anything
  else.

### 2. Weekly steward review

- **Schedule:** Weekly, e.g. Monday 6:00 AM local.
- **Agent consulted:** Steward.
- **Instructions (task prompt):** "Consult the Steward agent over the past week's sessions
  under `.cohort/sessions/` and feedback under `.cohort/feedback/`. Draft an improvement
  proposal narrative covering friction signals and candidate gaps. Write it to
  `.cohort/reports/research/steward-weekly-<YYYY-MM-DD>.md`. Do not run `cohort
  propose-improvement` or any other command — drafting only."
- **Output location:** `.cohort/reports/research/steward-weekly-<date>.md`.
- **What the human does with it:** Read the draft with **extra review scrutiny** — more than
  you'd give a proposal Steward drafts interactively. Its inputs here are session records
  consulted unattended, without you in the loop to catch a misread signal in the moment. If the
  draft holds up, run `cohort propose-improvement --body-file <that draft>` yourself; that
  command — and the human-reviewed draft PR it produces — is unchanged and still required. The
  scheduled session never runs it for you.

### 3. Compliance watch

- **Schedule:** Weekly, e.g. Friday 8:00 AM local.
- **Agent consulted:** Compliance.
- **Instructions (task prompt):** "Consult the Compliance agent for regulatory or policy
  developments relevant to this project's domain (check `.cohort/project_context.md`). Flag
  anything with a proximity-to-limit concern. Write findings with sources to
  `.cohort/reports/research/compliance-watch-<YYYY-MM-DD>.md`. Do not run any other commands."
- **Output location:** `.cohort/reports/research/compliance-watch-<date>.md`.
- **What the human does with it:** Treat it as a pointer, not a determination — Compliance is
  advisory and explicitly defers formal determinations to a human owner even when consulted
  interactively. Verify anything material against the authoritative source before acting, and
  route anything binding to compliance leadership or counsel.

## `.gitignore` guidance

`.cohort/reports/` is not new — `cohort weekly-report` and `cohort monthly-report` already write
**tracked**, deterministic reports directly there (`.cohort/reports/weekly-<date>.md` and
friends), and tests assert that path is *not* git-ignored. Don't gitignore the whole directory,
or you'll silently untrack Cohort's own reports along with the untrusted research output.

Instead, scope the ignore rule to the subfolder these recipes use. Add to your project's
`.gitignore`:

```gitignore
# Untrusted, web-derived output from opt-in scheduled research recipes.
# See docs/scheduled-research.md. Never @import; never paste unreviewed
# into project_context.md.
.cohort/reports/research/
```

This is guidance for a project that opts into these recipes — Cohort doesn't add this rule for
you, the same way it doesn't create the scheduled task for you. Opting in is entirely yours to
do, and yours to undo by deleting the task and the ignore rule.

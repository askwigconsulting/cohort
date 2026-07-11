# Life project connectors: Gmail, Calendar, Drive (opt-in)

Part of [RFC 0003](rfcs/0003-personal-agentic-os.md) — the personal agentic OS. This page is for
a `template = "life"` project created with `cohort init --template life` (see `docs/DESIGN.md` /
the RFC for the scaffold this creates). Read it before running `/today`, `/briefing`, or `/triage`
against real mail and calendar data — every control described here is what makes those commands
safe to run at all.

## The framing, up front

Cohort never implements a Gmail/Calendar/Drive client, never runs an OAuth flow, and never stores
a token. **Connectors are MCP, configured not implemented** (RFC 0003 §2): Claude Code owns
transport, auth, and token lifecycle end to end. Cohort's job is narrower — it scaffolds an
example config naming Google's own official servers, a read-only permission profile, and this
guide. If you never copy `.mcp.json.example` to `.mcp.json` and authenticate, the life project
still works as a plain markdown day/week/goals system; connectors are additive, not required.

**The read-only OAuth scope is the real boundary — everything else is defense-in-depth.** A
permission file can deny a tool by name, but if the underlying OAuth grant has write access,
a renamed, parameterized, or newly-added tool on the same server could still write. Google's API
itself refusing the write is the layer that holds even when a permission file has a bug. Grant
`gmail.readonly`, `calendar.readonly`, and `drive.readonly` wherever the connector's consent
screen offers scope selection — not the broader read/write scopes some flows offer by default.

## Setup

1. **Add the official servers.** Copy `.mcp.json.example` to `.mcp.json` in the life project root.
   It names Google's official remote MCP servers (verified against Google's Workspace MCP docs,
   July 2026: Gmail 10 tools, Calendar 9 tools, Drive 8 tools — a name-complete list for the
   current API version). **Keep the canonical server keys** — `gmail`, `calendar`, `drive` —
   exactly as scaffolded. The permission-rule prefix is `mcp__<server-key>__`, so renaming a
   server key in `.mcp.json` silently makes every rule in `.claude/settings.json` and
   `.claude/settings.briefing.json` match nothing — every `allow`/`deny` entry becomes a no-op at
   once, and you'd have no signal it happened short of noticing a denied tool actually ran.
   `cohort status` warns when the profile references a server key no configured `.mcp.json`
   provides, but that's a presence check, not a content check — it can't catch every misconfiguration.
2. **Authenticate once, interactively.** In a Claude Code session inside the project, run `/mcp` →
   **Authenticate** for each server. This is the interactive OAuth consent screen — grant the
   read-only scopes named above. Claude Code caches the resulting token on local disk and reuses
   it for both interactive sessions and headless (`claude -p`) runs; Cohort never reads or touches
   this token.
3. **Accept workspace trust.** Claude Code's project-scope workspace-trust prompt for this folder
   is the consent gate for the whole `.claude/settings.json` permission file taking effect,
   connectors included.

**No community MCP server is named in the scaffold or recommended here** (RFC 0003 §2, resolved
question 1). Only the official servers give a static, name-complete tool set and auditable
read-only scopes — a renamed or community-added tool defeats name-based denial outright, and some
community servers expose a single tool name that spans both safe and unsafe behavior (e.g. a
`draft_email(send=true)` parameter). If you evaluate a broader-coverage community server anyway,
apply the same bar this guide holds Google's servers to before trusting it: a statically enumerated
tool set, read-only scope support, and a passing result on the verify-before-trust checklist below
— Cohort doesn't endorse one.

## The two permission profiles

Two profiles ship with the life template. Deny always wins over ask, and ask always wins over
allow (first match, evaluated deny → ask → allow) — **no tier uses a server wildcard**; every
allowed tool is named exactly, so a new tool Google adds to a server is denied by default until
someone deliberately adds it.

- **`.claude/settings.json`** (interactive) — the enumerated read tools (search/get/list across
  Gmail, Calendar, Drive) are allowed; every mutator (`create_draft`, `create_event`,
  `label_message`, `create_file`, …) plus `WebFetch`/`WebSearch` are denied outright. There is no
  `ask` tier for reads: with scopes already read-only, a per-read prompt protects nothing and
  trains reflexive approval that would blunt the signal on a genuinely risky call.
- **`.claude/settings.briefing.json`** (headless) — strictly less than interactive: a smaller
  read set (calendar + mail search/get only), `Bash`/`WebFetch`/`WebSearch` denied, writes confined
  to `Write(.cohort/reports/briefings/**)` only (never `days/**`/`weeks/**` — those are the
  *trusted* tier), and `defaultMode: dontAsk` so any unmatched tool call auto-denies rather than
  prompting a human who isn't there. This is the profile `/briefing` runs under, whether launched
  by a Claude Code Desktop scheduled task or a `cohort run` job.

**What relaxing a rule costs.** Every deny in the scaffold is a deliberate choice, not a default
left in place by accident — relaxing one has a specific, named cost:

| Relaxing… | Costs you… |
| --- | --- |
| `mcp__gmail__create_draft` | An injected prompt (a crafted email body, a poisoned calendar invite) can now plant drafts at scale. The official Gmail server has no `send_email` — a human still sends from Gmail's UI — but a pile of unwanted drafts is still an outbound-adjacent artifact and a cleanup burden. |
| Any `label_*`/`unlabel_*`/`create_label` tool | A session can reorganize your mailbox unattended — including hiding evidence of what it read or did by mislabeling/archiving-equivalent moves, if your mail client treats a label change as an archive signal. |
| `mcp__calendar__create_event` / `update_event` / `delete_event` | A session can add, move, or remove real commitments on your calendar — including a plausible-looking but fabricated event from an injected prompt. |
| `mcp__drive__create_file` / `copy_file` | A session can create Drive content unattended; combined with sharing defaults on your Drive, that content could become visible to others without a review step. |
| `WebFetch` / `WebSearch` (either profile, especially briefing) | The single most dangerous relaxation: a session that can read your mail **and** reach the open web can exfiltrate it — encode mailbox contents into a URL and fetch it, no send/draft tool required. This is why RFC 0003 principle 5 (egress isolation) denies both outright in any mail-reading profile, not just the briefing one. |
| Granting broader (non-`readonly`) OAuth scopes | Removes the fail-closed backstop underneath every rule above — a permission-file bug, a renamed tool, or a new tool Google adds later would no longer be caught by Google's API itself refusing the write. |

Relax a rule deliberately, one at a time, understanding exactly this cost — never by loosening a
whole tier (e.g. removing the `deny` block) to make a single tool work.

## Verify-before-trust checklist

Before your first real use of either profile — and especially before trusting the briefing profile
to run unattended — confirm each of these **in-transcript**, the way `docs/scheduled-research.md`
already asks you to verify its restricted profile actually holds:

1. **Granted OAuth scopes are read-only.** Check the consent screen you approved, or your Google
   Account → Security → third-party access, and confirm it says `readonly` for Gmail, Calendar,
   and Drive — not full read/write.
2. **A deliberate mutating call is blocked.** Ask the session to attempt `create_draft` (or
   `create_event`, or a label call) and confirm it is refused by the permission file, not merely
   left unprompted.
3. **`WebFetch`/`WebSearch`/`Bash` are denied**, especially under `.claude/settings.briefing.json`
   — ask the session to try one and confirm it's blocked.
4. **The briefing quarantine is gitignored.** Confirm `git check-ignore -v
   .cohort/reports/briefings/<any-file>` reports a match, so a briefing never lands in a commit by
   accident.
5. **The git remote is private.** `days/`, `weeks/`, and `inbox.md` hold summarized private
   content and are committed to git normally (not gitignored) — see the disclosure below for why
   that means a public remote is a permanent leak, not a recoverable mistake.

If any of these five doesn't hold, stop and fix the permission file or scope grant before trusting
either profile with real data — especially before leaving a scheduled briefing task running while
you're away.

## Disclosure (read this before connecting real accounts)

Plain language, for a non-technical reader — **the wording below is a draft; have counsel/privacy
confirm it before shipping to end users.** It is not a substitute for legal review.

1. Every email, calendar event, and document a session reads through these connectors is sent to
   Anthropic's API as part of that session, and handled under the terms of your Claude plan.
2. It also transits Google's remote MCP endpoint — a Google-hosted service, separate from both
   Anthropic and Cohort.
3. OAuth tokens are cached on your local disk by Claude Code. **Cohort never sees them.** To
   revoke access, go to your Google Account → Security → Third-party access and remove Claude Code
   — deleting local files does not revoke a cloud-side grant.
4. A headless briefing run (scheduled task or `cohort run` job) reads your mail and calendar with
   nobody watching in real time. The output is written to a quarantined, gitignored folder for you
   to review later — but the read itself happens unattended.
5. `days/`, `weeks/`, and `inbox.md` contain **summarized private content** (see Minimization,
   below) and, unlike the briefing quarantine, are **committed to git normally**. Pushing this
   repository to a remote copies that content there **permanently** — git history doesn't forget
   even if you later delete the file. Use a **private** remote you control. Never push a life
   project to a public remote.

**Minimization**, the discipline every rhythm command follows when writing to a tracked file:
reference mail as `sender — subject (date)`, never a body quote; never copy an attendee list, an
attachment, a phone number, or a meeting dial-in/link (a Zoom/Meet URL is a bearer credential);
agenda lines are event title + time only. A body excerpt, when one is genuinely needed, belongs
only in the gitignored briefing quarantine — never in `days/`, `weeks/`, or `inbox.md`.

## `cohort run` — the job runner

Mission control's dashboard (when it's focused on a `template = "life"` project) can enqueue a
rhythm command as a job by writing a bounded request file to `.cohort/jobs/` — but **the dashboard
itself never spawns a process.** A job only executes when you start the runner yourself, in a
terminal:

```bash
cohort run
```

This is a **human-started, foreground process** — the same "your own session is the actor"
discipline `docs/scheduled-research.md` already documents for scheduled tasks, extended to jobs
queued from the dashboard. `cohort run`:

- watches `.cohort/jobs/` for request files (`<command>-<timestamp>.json` — an allowlisted command
  name and a timestamp, **never a free-text prompt**);
- executes each request by spawning `claude -p "/<command>"` with a **constant** argv built from
  an exact-key command allowlist — a crafted or unexpected command name simply isn't in the
  allowlist and is refused before a process is ever spawned;
- pins `--settings` to the server-side profile for that command (`/briefing` runs under
  `.claude/settings.briefing.json`) — never a value the dashboard or job file supplies;
- runs with a minimal, curated environment (not your full shell environment), a pinned working
  directory, and a timeout;
- refuses to start a second run of the **same** command while one is already in flight
  (single-flight per command, rejected with a 409-style error rather than queuing a storm);
- terminates any child process it spawned when you stop it (Ctrl-C) — nothing outlives the
  terminal `cohort run` is running in.

**Only `/briefing` is currently scaffolded into the job allowlist** (see `canonical/commands/briefing.md`
— it is the one rhythm command whose entire text is written to be `claude -p`-clean, with no
mid-run interactive prompt anywhere in it). Treat every job's output the same way you'd treat a
scheduled briefing: it lands under `.cohort/reports/briefings/` as untrusted, connector-derived
content — read it, never `@import` it, never wire it into `project_context.md` unreviewed.

Stop the runner (Ctrl-C) when you're not actively expecting a job to run — like any Cohort
process, it is daemon-free by design and does nothing while it isn't running.

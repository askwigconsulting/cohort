# RFC 0003 — Personal agentic OS: the life project

- Status: **Draft** (office-reviewed 2026-07-10; revised per reconciled review; §4 reworked to
  interactive mission control after a second Security + Steward review — enqueue-and-run model)
- Author: Cohort maintainers
- Created: 2026-07-10
- Depends on: epic #139 (loop operating model — delivered), RFC 0001 (multi-install), docs/scheduled-research.md (#147)
- Reviewed by: Steward, SecurityEngineer, PrivacyOfficer, Researcher; reconciled by ChiefOfStaff

## Summary

Make Cohort able to run a **personal agentic OS**: a Cohort-managed project (canonically
`my_life`) that manages a person's day-to-day, week-to-week, and month-to-month — connected to
their Gmail, Calendar, and Docs — while holding every existing invariant: stdlib-only,
daemon-free, human-gated writes, external content untrusted, personal data never synced into
office tiers.

Cohort does **not** become a personal-data platform. It becomes able to *scaffold and operate*
a life project the same way it scaffolds a code project: canonical rhythm commands, an
advisory life chief-of-staff, a connector configuration surface (MCP — never Cohort-built API
clients), read-only life views in the dashboard, and user-owned scheduling recipes.

## Motivation

The maintainer wants "a UI that can manage my day to day, week to week, month to month," that
"connects to all of my things (gmail, calendar, docs)" and "helps me stay organized." Epic
#139 delivered the loop operating model for *code* work (goals, judged builds, compounding
memory, mission control). This RFC extends the same operating model to *life* work. The gap
analysis is small because #139 built most of the machinery:

| Need | Already exists | Gap |
| --- | --- | --- |
| A managed project | `cohort init`, registry, project context | a **life template** for non-code repos |
| Connect to Gmail/Calendar/Docs | Claude Code MCP support; Google's official MCP servers (remote HTTP + OAuth) | Cohort has **zero MCP awareness** — needs a config *example* + permission profile |
| Day/week/month organization | commands + skills machinery, `/plan` | **rhythm commands** (`/today`, `/triage`, `/week`, `/month`) + a pinned data model |
| Compounding weekly review | `distill` (append-only, confirm-gated, provenance-cited) | a **life-scoped distill target** (new code — see §5) |
| A UI | stdlib dashboard, cross-project activity + scorecards (#149) | **read-only life views**: agenda, week board, goals |
| Stay on top of things unattended | scheduled-research recipe pattern (#147) | a **morning-briefing recipe** (same pattern, stricter profile) |

## Design principles (inherited, non-negotiable)

1. **stdlib-only CLI.** No Google SDKs, no OAuth libraries, no new dependencies.
2. **Daemon-free; the dashboard never spawns an agent.** Cohort ships nothing that runs
   unattended. Scheduled runs are the user's own IDE scheduled tasks (docs/scheduled-research.md
   pattern). The interactive dashboard may write bounded files (edits, job *requests*) but must
   **not** `subprocess`-launch `claude`; a job is executed only by a human-started foreground
   `cohort run`. The actor is always your own session, never the http.server (§4).
3. **Connectors are MCP, configured not implemented.** Cohort scaffolds an example config and a
   permission profile; Claude Code owns transport, auth, and token lifecycle. Cohort never
   reads a token, never calls a server, never parses a credential.
4. **External content is untrusted.** Every email body, calendar description, and doc fetched
   through a connector is untrusted *input* (prompt-injection vector), and every generated
   briefing is untrusted *output* (quarantined, gitignored, never `@import`ed, rendered as
   text only).
5. **The egress-isolation invariant (load-bearing).** *A session that can read private
   connector data must have every outbound channel closed.* The controls in priority order:
   (a) **read-only OAuth scopes** granted to the connector — the true fail-closed layer;
   (b) an **exact-name read allowlist** with no outbound tool allowed and no wildcard `allow`;
   (c) **`WebFetch`, `WebSearch`, `Bash` denied** in any mail-reading profile; (d) writes
   confined so untrusted output never reaches a trusted file. The "content is data, not
   instructions" prompt discipline (§3) is defense-in-depth *behind* these, never the boundary.
6. **Human-gated writes.** Reading mail/calendar can be allowed; *drafting, sending, creating,
   modifying, deleting, or labeling anything external* is deny-by-default in the scaffolded
   profile. The human acts, or explicitly relaxes a rule with the docs explaining the cost.
7. **Personal data never crosses a sync boundary.** The life project is local / private-remote;
   nothing in it compiles into, promotes to, or distills into the office or my-office tiers.

## Architecture

### 1. The life project is a Cohort project with a template

`cohort init --template life` (in, e.g., `~/my_life`) lays down the standard `.cohort/`
skeleton **plus** the life scaffold. This introduces a minimal, general **template concept**:
today's init plan is the implicit "code" template; a template contributes extra scaffold ops
and a different project-context body.

```
my_life/
  inbox.md                  # [init] capture anything; /triage drains it
  goals/
    2026.md                 # [init] year goals (one seed file)
  weeks/                    # [init: empty dir] /week creates weeks/2026-Wnn.md
  days/                     # [init: empty dir] /today creates days/YYYY-MM-DD.md
  .cohort/
    cohort.toml             # [init] gains template="life", dashboard.private=true, large staleness_hours
    project_context.md      # [init] life-flavored context body + "never push to a public remote" banner
    reports/briefings/      # [init: empty dir] QUARANTINE: connector-derived output; gitignored
    .gitignore              # [init] adds reports/briefings/ to the existing state/ + compiled/ ignores
  .mcp.json.example         # [init] connector config template (Google-official servers), NOT active
  .claude/
    settings.json           # [init] read-only interactive permission profile (§3)
    settings.briefing.json  # [init] stricter headless profile for the scheduled briefing (§3)
    CLAUDE.md               # [init] managed @import, as today
```

**Init creates only non-dated files** (`inbox.md`, `goals/2026.md`, empty `weeks/`/`days/`
dirs). Dated files are created by the rhythm commands, never scaffolded (a scaffolded
`days/2026-07-10.md` is stale by construction and would be manifest-recorded — see purge
handling below).

**Purge safety.** Life data files (`inbox.md`, `goals/**`, `weeks/**`, `days/**`) are **not
SCAFFOLD manifest ops** — they are written by a one-shot template writer outside the reversible
op plan. `cohort deinit --purge` reverses the manifest and rmtree's `.cohort/`, so manifest-recorded
files would be deleted *including user edits*; a year of goals must never be reachable by purge.
`deinit` on a life project warns that life data under the repo root is left untouched and must be
removed by hand. This makes the non-goal "deleting `.cohort/` leaves a usable plain-text life
system" actually true.

**Re-init semantics.** The `template = "life"` marker is written only at first init. `cohort init
--template life` over a directory that already has a `cohort.toml` **refuses** (there is no TOML
merge strategy; a create-if-absent scaffold would silently no-op and never write the marker),
printing the one line to add by hand. `cohort.toml` gains, read fail-safe (absent = code project):

```toml
template = "life"

[dashboard]
private = true          # excluded from cross-project switcher, activity feed, scorecards
```

Config reads consolidate into one `read_project_config(paths) -> dict` helper (WS-A) rather
than a fifth inline `tomllib` reader.

### 1a. The data-model contract (pinned — this is a cross-workstream seam)

WS-A's template files, WS-B's parser, and WS-C's commands all depend on this exact format. It
is a contract, not a suggestion; unknown sections are preserved and ignored, but the parser
emits a diagnostic (surfaced in `cohort status` and the dashboard) when a **known** heading is
missing rather than silently rendering blank.

- **Filenames.** Days: `days/YYYY-MM-DD.md`. Weeks: `weeks/YYYY-Wnn.md` (ISO-8601 week, `nn`
  zero-padded). "Today"/"this week" resolve in the **user's local timezone**, computed once per
  command/parse and passed in (never `Date.now()` mid-logic) so the dashboard server and an
  interactive session agree across midnight/UTC.
- **Day file** headings, in order: `# YYYY-MM-DD`, `## Agenda` (calendar-derived, event
  `- HH:MM title` lines), `## Top 3` (`- [ ]` checklist, ≤3 items), `## Log` (freeform).
- **Week file** headings: `# YYYY-Wnn`, `## Plan` (`- [ ]` checklist), `## Review` (distill
  target — see §5).
- **Goals** files: `# <year|quarter> goals` then `## <goal>` sections with `- [ ]` progress
  checklists.
- **Checklist grammar:** GitHub-style `- [ ]` / `- [x]` at line start (one optional leading
  indent level tolerated). `[x]` = done; anything else = open.

### 2. Connectors: an example config, zero Cohort transport code

Google ships official MCP servers for Gmail/Calendar/Drive (remote HTTP; OAuth via the
interactive `/mcp` → Authenticate flow; tokens cached by Claude Code and reused headlessly).
Their tool sets are **statically enumerated** (verified against Google's Workspace MCP docs,
July 2026: Gmail 10, Calendar 9, Drive 8 tools) — so a scaffolded allow/deny list can be
name-complete for the current API version. Notably the official Gmail server has **no
`send_email`** (only `create_draft`; the human sends from Gmail's UI) and Drive has **no
`delete_file`**.

Cohort scaffolds `.mcp.json.example` (strict JSON cannot carry comments, so a commented
`.mcp.json` would be invalid and could break the workspace-trust flow) with the Google-official
server entries plus a setup guide. The user copies it to `.mcp.json`, completes OAuth once, and
Claude Code's project-scope workspace-trust prompt is the consent gate.

**Server naming matters for safety.** The permission-rule prefix is `mcp__<server-key>__`, where
`<server-key>` is whatever the user names the server in `.mcp.json`. The example pins canonical
keys `gmail`, `calendar`, `drive`; the setup guide REQUIRES the user keep those keys (a mismatch
makes every rule in the profile silently match nothing). `cohort status` warns when the profile
references a server key that no configured `.mcp.json` provides (presence-check only — Cohort
never reads server contents beyond entry keys).

**No community server is named in the scaffold or endorsed in the guide** (open question 1 →
No). Only the official servers give a static tool set and auditable read-only scopes; community
servers rename tools (defeating name-based denial), don't guarantee a stable key, and some
expose `draft_email(send=true)`-style tools where one name spans safe and unsafe behavior. The
guide may list *evaluation criteria* for a user who wants broader coverage (static tool
enumeration, read-only scope support, passes the send-blocked verification test in §3) without
recommending one.

### 3. The permission profile: read-only scopes first, allowlist second

Two profiles ship. Deny always wins; `ask` shadows `allow` (precedence deny→ask→allow, first
match), so **no tier uses a server wildcard** — every allowed tool is named exactly.

**Interactive** (`.claude/settings.json`) — enumerated read tools allowed, everything else
denied outright (no `ask` tier: there is no read tool worth prompting on once scopes are
read-only, and per-message prompts train reflexive approval that destroys the signal for
genuinely risky calls):

```json
{
  "permissions": {
    "allow": [
      "mcp__gmail__search_threads", "mcp__gmail__get_thread",
      "mcp__gmail__list_drafts", "mcp__gmail__list_labels",
      "mcp__calendar__list_events", "mcp__calendar__get_event",
      "mcp__calendar__search_events", "mcp__calendar__list_calendars",
      "mcp__calendar__suggest_time",
      "mcp__drive__search_files", "mcp__drive__read_file_content",
      "mcp__drive__download_file_content", "mcp__drive__get_file_metadata",
      "mcp__drive__get_file_permissions", "mcp__drive__list_recent_files"
    ],
    "deny": [
      "mcp__gmail__create_draft", "mcp__gmail__create_label",
      "mcp__gmail__label_message", "mcp__gmail__label_thread",
      "mcp__gmail__unlabel_message", "mcp__gmail__unlabel_thread",
      "mcp__calendar__create_event", "mcp__calendar__update_event",
      "mcp__calendar__delete_event", "mcp__calendar__respond_to_event",
      "mcp__drive__create_file", "mcp__drive__copy_file",
      "WebFetch", "WebSearch"
    ]
  }
}
```

`create_draft` is **denied** in the scaffold even though the official server can't send: an
injected prompt planting drafts at scale is still an outbound-adjacent artifact, and principle 6
says the scaffold never ships outbound-capable. The guide documents relaxing it as a deliberate
per-tool choice.

**Headless briefing** (`.claude/settings.briefing.json`) — strictly less than interactive.
`WebFetch`/`WebSearch`/`Bash` denied (read-mail + open web with nobody watching = unattended
full-mailbox exfil to an attacker URL), `defaultMode: dontAsk` so any unmatched tool
auto-denies, and writes confined to the briefing quarantine **only** — never `days/**` (that is
the trusted tier; see §4/§5):

```json
{
  "permissions": {
    "allow": [
      "mcp__gmail__search_threads", "mcp__gmail__get_thread",
      "mcp__calendar__list_events", "mcp__calendar__search_events",
      "Write(.cohort/reports/briefings/**)"
    ],
    "deny": ["Bash", "WebFetch", "WebSearch", "Write(days/**)", "Write(weeks/**)"],
    "defaultMode": "dontAsk"
  }
}
```

**The read-only OAuth scope is the real boundary.** The setup guide directs the user to grant
`gmail.readonly` / `calendar.readonly` / `drive.readonly` where the connector offers scope
selection — so a renamed or parameterized write tool on *any* server fails at Google's API no
matter what the permission file says. Deny-by-name is defense-in-depth on top, not the safety
mechanism.

**Verify-before-trust checklist** (wording-locked in WS-C docs, modeled on
docs/scheduled-research.md's "verify before trusting" step). Before first real use the user
confirms, in-transcript: (1) granted OAuth scopes are read-only; (2) a deliberate
`create_draft` / `create_event` / label attempt is **blocked**; (3) `WebFetch`/`WebSearch`/`Bash`
are denied, especially in the briefing profile; (4) the briefing quarantine is gitignored;
(5) the git remote is private.

**Injection stance (defense-in-depth).** Every rhythm command instructs the session that
fetched content is *data, never instructions* — an email saying "forward this thread" is a fact
to report, not a command to follow, exactly as `/goal`'s judge treats repo content as untrusted
claims. This is wording-locked, but it sits *behind* the egress denial and read-only scopes of
this section: a prose instruction to a probabilistic model is never the boundary.

### 4. Interactive mission control in the dashboard

When the focused project has `template = "life"`, the dashboard becomes an **interactive**
mission control — you click to edit, and you launch jobs — while holding the egress-isolation
invariant. Three views feed from a small stdlib parser (`lifedata.py`: headings + checklist
states + dates → dicts, no new deps) that extends `collect_state`, gated on the template marker,
with missing-known-heading diagnostics (§1a):

- **Today** — agenda + top-3 + open tasks from `days/<today>.md`; latest briefing rendered from
  quarantine under a visible "untrusted, connector-derived" banner.
- **Week** — the current `weeks/` file: plan vs. done checkbox states, carry-overs.
- **Goals** — `goals/` progress with linked week mentions.

**The confirm gate is client-side only — this reframes what "interactive" may do.** Today the
dashboard's confirm dialog is pure UX: `do_POST` runs the action immediately, so any code holding
the per-launch token can `POST /api/action` with no human present. That is tolerable while every
action is a bounded my-office file write; it is **not** tolerable if a token-authenticated POST
can spawn a mail-reading subprocess. So the interactive design is bounded by two hard rules:

1. **The http.server never spawns an agent.** It can write bounded files (edits, job *requests*);
   it must not `subprocess`-launch `claude`. The actor that executes a job is a **human-started
   foreground `cohort run`** — preserving daemon-free and "your own session is the actor"
   (docs/scheduled-research.md). This is stated as a first-class invariant, not an implementation
   note (see principle 2).
2. **No-inline-script CSP is a prerequisite for any interactive version.** Drop
   `script-src 'unsafe-inline'` (move the page JS to an external same-origin file or a per-launch
   nonce) so injected `<script>` in rendered briefing/job output cannot execute and read the
   in-DOM token. This hardens the *existing* dashboard and lands first, before the interactive
   verbs. `img-src 'none'` for connector-derived content (a rendered image is an exfil beacon).

**Editing (ships in v1).** Clicking a checkbox, setting your top-3, or adding a task dispatches a
new **`cohort life <verb>`** CLI function through the existing confirm bridge — the dashboard
gains **no mutation logic of its own** (the stated `run_action` invariant); it calls a `do_*`
function exactly as `do_snapshot` does. Verbs (`life toggle-task`, `life set-top3`,
`life add-task`, …) are deterministic markdown writes to `days/`/`weeks/`, target resolved by
**enumeration** (never a raw client path or a `name` with `..`/separators — the `read_artifact`
pattern), unit-testable at the `do_*` layer. These files are the *trusted* tier (auto-loaded into
future sessions), so the write endpoint is a higher-value injection sink than my-office authoring
— hence the enumerated-target rule and the CSP prerequisite both apply.

**Kick off jobs (enqueue-and-run, v1).** Clicking "Run /briefing" dispatches a `cohort life
enqueue <command>` verb that writes a **bounded job-request** file (`.cohort/jobs/<command>-<ts>.json`
— an allowlisted command name + timestamp + status; **never a free-text prompt**). A
human-started foreground **`cohort run`** watches `.cohort/jobs/`, executes each request by
spawning `claude -p "/command"` under the profile the *runner* pins per command (briefing/triage
under the egress-closed `settings.briefing.json`), and writes output to the quarantine; the
dashboard shows it live. The runner — not the browser — is the only thing that spawns a process,
and it dies with the terminal you started. Fail-closed construction (in `cohort run`): argv is a
**constant** built from an exact-key command allowlist (`_JOBS[command]`), `shell=False`, the
caller contributes **zero** argv tokens (a crafted name like `briefing --permission-mode=…` misses
the key and is refused), `--settings` is a server-side constant path never a client value, minimal
curated env (not `{**os.environ}`), cwd pinned from the resolved registry, a `timeout`,
**single-flight per command** (reject with 409 if already running, never queue-storm), and child
PIDs terminated on shutdown so nothing outlives the terminal. All job stdout is untrusted
quarantine content: rendered `textContent`-only, never `@import`ed, never auto-loaded.

**Ask questions — deferred to v2.** A conversational Q&A box has the same spawn-a-session cost as
jobs; it rides on the proven enqueue model and is a separate milestone. When built, it routes to
the advisory read-only life-chief-of-staff under a strictly egress-closed profile (no web, no
send/write/exec; calendar read from a **local cache**, not a live networked call — a live MCP call
is itself outbound reachability the invariant forbids for a mail-adjacent session).

**Rendering discipline (all connector/job-derived content).** `textContent`/DOM-construction only,
never `innerHTML`/`insertAdjacentHTML` — extend `dashboard.html`'s stated "no disk/subprocess-derived
string reaches innerHTML" invariant to cover live job stdout. The untrusted banner is a human
signal, not a control; the CSP fix is the control.

**Privacy.** `dashboard.private = true` is the **fail-safe default** for the life template
(absent key ⇒ private). Private means the project is excluded from `state["projects"]` (the
switcher / `resolve_registered` refuses to focus it), `cross_project_activity`, and
`cross_project_scorecards` — not merely the feed. Session titles and author name+email can carry
email subjects and names; none of it appears in a work dashboard. Opt-*out* (`private = false`)
is the deliberate act. On a shared machine the loopback+token dashboard does **not** protect
against another OS user on the same login — the docs say so plainly and recommend a separate OS
account and not leaving the dashboard running.

### 5. Rhythms: four commands, one briefing command, one advisory agent

All `scope: global`, `targets: [claude]`, `dry_run: true`, human-invoked; none is a doer; each
embeds the §3 injection stance and follows house-style conventions (graceful degradation, and a
closing offer of `/feedback` on the life-chief-of-staff so the dashboard scorecard loop sees
life usage).

- **`/today`** (interactive) — read calendar + `inbox.md` + this week's plan; consume the
  latest briefing if one exists; draft `days/<date>.md` (agenda, proposed top-3); user confirms
  the write. Degrades to inbox + week file when no connector is configured or auth expired
  (like `/goal` without `gh`).
- **`/briefing`** (headless-safe — the only command runnable under `settings.briefing.json`) —
  read calendar + unread-mail *summaries*, write a briefing to `.cohort/reports/briefings/`
  only. Its entire text is egress-safe; `/today` consumes its output with the human present.
- **`/triage`** — drain `inbox.md` + unread-mail summaries into proposed dispositions
  (reply-needed / becomes-task / becomes-event / drop); the confirm shows source citations
  (extractive, like distill) so quarantine content can't launder into the week file through one
  "yes"; approved tasks land in the week `## Plan`. Never sends, drafts, archives, or labels — it
  proposes.
- **`/week`** — review last week's file (shipped vs. carried); run the life-scoped distill
  (below) into the week `## Review`; draft next week's `## Plan` from goals + carry-overs.
- **`/month`** — roll `weeks/` up against `goals/`. **Reads no connectors at all** (it doesn't
  need them); proposes a goal edit the user accepts or rejects.

One canonical agent: **life-chief-of-staff** (advisory, read-only tools), aware of the §1a file
layout; the routing brain for "what should I focus on?" It recommends; the main session and the
human act.

**Life-scoped distill (new code, not "reuse as-is").** Today `do_distill` writes only to
`project_context.md` from `sessions/`+`feedback/`. In a `template = "life"` project it must
target the current week file's `## Review` section and **refuse** `project_context.md` — else
connector-derived text enters the `@import`ed corpus loaded into every future session (a
permanent injection channel and a privacy leak). Input stays `sessions/` records only, never the
briefing quarantine. It keeps its safe properties: deterministic, extractive (every line cites
its source), control-char-escaped, confirm-diff.

**Minimization (wording-locked in the command texts).** Reference mail as `sender — subject
(date)`; never quote bodies into `days/`, `weeks/`, or `inbox.md` (a one-line paraphrase only
when a disposition needs it; body excerpts go only to the gitignored briefing quarantine). Never
copy recipient/attendee lists, attachment contents, phone numbers, or meeting dial-ins/links —
Zoom/Meet URLs are bearer credentials. Agenda = event title + time only.

### 6. Scheduling: the morning-briefing recipe (docs only)

Extends docs/scheduled-research.md with a "morning briefing" recipe: a Claude Code Desktop
scheduled task running **`/briefing`** (not `/today`) under `settings.briefing.json`, output to
`.cohort/reports/briefings/`. Documented plainly: OAuth must be completed interactively once
before headless runs; the cached grant is long-lived (revoke at the Google account console, not
by deleting files — pair with read-only scopes so a replayed token still can't write); token
expiry symptoms. Nothing Cohort ships runs unattended; Codex/Cursor remain declared unfit
(unchanged from #147).

### 7. Disclosure (WS-C setup guide, plain language)

Stated for a non-technical reader: (1) every email/event/doc the session reads is sent to
Anthropic's API and handled under the user's Claude plan terms; (2) it also transits Google's
remote MCP endpoint; (3) OAuth tokens are cached on local disk by Claude Code — Cohort never
sees them — revoke at Google Account → Security → third-party access; (4) headless briefing runs
read mail with nobody watching; (5) `days/`, `weeks/`, `inbox.md` contain summarized private
content and are committed to git — **pushing the repo copies it to the remote permanently
(history); use a private remote you control, never a public one.** (Legal adequacy of this
wording is out of scope for the office review — have counsel/privacy lead confirm before ship.)

## What Cohort explicitly does NOT do (non-goals)

- No Cohort-implemented API clients, OAuth flows, or token storage — MCP or nothing.
- No daemon, no background sync, no polling loop.
- No autonomous outbound actions, and the scaffold never *ships* outbound allowed (even
  `create_draft` is denied by default) — the user relaxes `settings.json` themselves.
- No personal-data sync into office/my tiers; no promotion path for life artifacts. `cohort
  adopt --to my|office` must read the source project's `template = "life"` marker and refuse to
  lift a life-project agent into a synced tier (the marker lives in cohort.toml, not the
  artifact, so adopt must check it — mirroring the doer-promotion guard).
- **The http.server never spawns a `claude` process.** Interactive editing and job *requests*
  are bounded file writes; job *execution* is only ever a human-started foreground `cohort run`.
- No conversational Q&A box in v1 (deferred to v2, rides the enqueue model).
- No task-manager lock-in — the data model is plain markdown in the user's repo; removing
  `.cohort/` leaves a usable plain-text life system.

## Workstreams (three, parallel, contract-pinned)

The file layout + format (§1, §1a), both permission profiles (§3), the `cohort life`/`cohort run`
CLI contract (§4), and command names (§5) are the shared contract; each workstream implements
against this RFC, not against another's branch.

### WS-A — Fable: template engine, connector surface, boundaries, life/run CLI

Introduce the template concept in `project.py` (per-template init-plan contributions;
`template`/`dashboard.private` read via one shared `read_project_config`; first-init-only marker
with refusing re-init semantics); the life scaffold (non-dated files + example config +
**both** permission profiles + gitignored briefing quarantine); life data written **outside** the
SCAFFOLD manifest so `deinit --purge` can't delete it; connector-*presence* reporting + the
server-key-mismatch warning in `cohort status`; the sync-boundary refusals (distill life-target +
`project_context.md` refusal; `adopt` template-marker check); large `staleness_hours` so the
daily rhythm doesn't trigger the snapshot nag. **Plus the interactive CLI surface WS-B dispatches
to:** a `life.py` module with `cohort life <verb>` deterministic markdown writers (`toggle-task`,
`set-top3`, `add-task`, …; enumerated targets, `do_*`-testable) and `cohort life enqueue <command>`
(writes a bounded job-request); and the **`cohort run`** foreground runner (watches `.cohort/jobs/`,
constant-argv command allowlist `_JOBS`, `shell=False`, dashboard-pinned `--settings`, minimal env,
pinned cwd, `timeout`, single-flight per command, child-PID reaping on shutdown). Tests: template
init idempotence/refusal, purge leaves life data, profile-content locks (outbound + WebFetch/
WebSearch/Bash denied, no server wildcard), distill refuses `project_context.md`, adopt refuses a
life agent, `cohort life` enumerated-target rejection (`..`/paths), `cohort run` argv-allowlist
fail-closed (crafted command name refused, no caller flag reaches argv), single-flight 409.

### WS-B — Opus: interactive mission control

`lifedata.py` (stdlib parser: the §1a format → dicts, missing-known-heading diagnostics,
timezone passed in); `collect_state` extension gated on the marker; Today/Week/Goals views in
dashboard.html. **Interactive:** edit controls (checkbox toggle, set-top3, add-task) that
dispatch `cohort life` verbs through the confirm bridge (no mutation logic in the dashboard);
"Run <command>" buttons that dispatch `cohort life enqueue`; a live job-output panel reading the
quarantine. **Security-load-bearing (all in WS-B's file territory, land the CSP fix first):**
drop `script-src 'unsafe-inline'` (external same-origin JS or per-launch nonce) + `img-src 'none'`
for connector/job content; briefing **and** job stdout rendered **`textContent`-only** under an
untrusted banner (extend the stated no-`innerHTML` invariant to job output); `dashboard.private`
honored fail-safe across switcher + feed + scorecards. Tests: parser round-trips (user-edited
files, unknown sections preserved, missing-heading diagnostic), timezone boundary, no-`innerHTML`
lock on briefing + job output, no-inline-script CSP assertion, private-flag exclusion from all
three surfaces, edit/enqueue actions dispatch the CLI verb (no inline write).

### WS-C — Sonnet: rhythms, briefing, agent, recipes, docs

Canonical `/today`, `/briefing`, `/triage`, `/week`, `/month` command texts (each embedding the
injection-stance paragraph and minimization rules — wording-locked like `/goal`'s push
discipline; `/briefing` entirely egress-safe; `/month` reads no connectors; `/triage` never
sends/drafts/labels). **Every command runnable as an enqueued job must be `claude -p`-clean /
headless-safe — no mid-run interactive prompt** (a stronger property than `/goal`'s graceful
degradation); wording-lock it. The life-chief-of-staff agent; docs: MCP connector setup guide
(Google official servers, OAuth read-only scopes, canonical server keys, what each relaxation
costs, the verify-before-trust checklist), the §7 disclosure, the morning-briefing scheduled
recipe, and the `cohort run` runner usage; README section; goldens regenerated. Tests:
wording-locks for the injection stance, the never-sends rules, the disclosure/checklist presence,
`/month`-reads-no-connectors, and headless-safety (no interactive-confirm text on job commands).

### Dependencies

The interactive design adds a second cross-workstream seam beyond the `template = "life"` marker:
the **`cohort life`/`cohort run` CLI contract** (§4), owned by WS-A and consumed by WS-B's action
dispatch. So WS-B now depends on WS-A for the interactive verbs (it can still build read views +
the CSP fix against a §1a fixture first, then wire edit/enqueue once WS-A lands). WS-C gains a
soft dependency: its job-runnable commands must be headless-clean before `cohort run` executes
them. **Suggested order:** WS-A lands the template marker + `cohort life`/`run` early → WS-B wires
interactivity; WS-C proceeds in parallel (canonical + docs, no code dependency). **Resolve
blockers in the contract sections (this document) before spawning workstreams** — every blocking
review finding sits inside the shared contract.

## Resolved review decisions (was: open questions)

1. **Community server in scaffold — No.** Google-official only; accept the Docs/Sheets/Tasks
   coverage gap for v1. The guide states evaluation criteria without endorsing one.
2. **`/triage` mail reads — allow (enumerated), not ask.** With read-only OAuth scopes and all
   actuators denied, a read prompt protects nothing and per-read prompts train reflexive
   approval. The scopes are the control.
3. **`dashboard.private` — default true for the life template, fail-safe (absent = private)**,
   scope widened to switcher + activity feed + scorecards. Opt-out is the deliberate act.
4. **Interactive mission control — job execution model is enqueue-and-run, not shell-from-browser**
   (maintainer decision, both reviewers concur). The dashboard writes a bounded job-request; a
   human-started foreground `cohort run` executes it. The http.server never spawns `claude`.
   Editing ships in v1 via `cohort life` verbs; the no-inline-script CSP is a prerequisite and
   hardens the existing dashboard regardless.
5. **Conversational Q&A — deferred to v2.** Same spawn-a-session cost as jobs; rides the proven
   enqueue model; when built, routes to the advisory read-only life-chief-of-staff under an
   egress-closed profile with a local-cache (not live) calendar read.

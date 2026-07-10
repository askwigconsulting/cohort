---
description: Break work into small verifiable tasks with acceptance criteria and dependency ordering
---

Invoke the agent-skills:planning-and-task-breakdown skill.

Read the existing spec (SPEC.md or equivalent) and the relevant codebase sections. Then:

1. Enter plan mode — read only, no code changes
2. Identify the dependency graph between components
3. Slice work vertically (one complete path per task, not horizontal layers)
4. Write tasks with acceptance criteria and verification steps
5. Add checkpoints between phases
6. Present the plan for human review
7. Offer to file the tasks as GitHub issues — opt-in, see below

Save the plan to tasks/plan.md and task list to tasks/todo.md.

## Filing tasks as GitHub issues (opt-in)

After the human reviews the plan, ask: "File these N tasks as GitHub issues?" Nothing is
filed without an explicit "yes" — a reviewed plan is not consent to file.

Resolve the target repo first (`gh repo view --json nameWithOwner`, or parse `git remote get-url
origin`). If the remote state is ambiguous (no `origin`, or multiple candidate remotes), ask the
user which repo to target — never guess. Then check `.cohort/cohort.toml` for a `[tracker]` table
(see below). The confirmation prompt must echo the resolved target repo, and — only if `[tracker]`
is present and valid — the board owner/number, before anything is created.

**gh hygiene (binding):**
- Every issue body is written to a temp file first and passed with `gh issue create --body-file
  <tempfile>` — plan text is never composed into an inline `--body` string or any other shell
  string.
- Titles are quoted as a single argument.
- The target repo is always explicit: `gh issue create --repo <owner>/<name> ...` — never rely on
  `gh`'s inference from an ambiguous multi-remote checkout.

**Issue body convention** — a convention, not a reference to Cohort's own
`.github/ISSUE_TEMPLATE/` (consumer repos won't have it). If the target repo has its own issue
templates, prefer those instead:

    ## Summary
    <one paragraph>

    ## Acceptance criteria (Done when)
    - ...

    ## Design notes
    <anything that doesn't fit above>

Cross-reference dependency order and a parent/epic issue in the body when one exists (e.g.
"Depends on #N", "Part of #M — see the plan for full context").

**Optional board add.** If `.cohort/cohort.toml` has a `[tracker]` table with `project_owner` and
`project_number`, add each filed issue to that project board (`gh project item-add
<project_number> --owner <project_owner> --url <issue-url>`). Validate first, fail closed:
`project_number` must parse as an integer and `project_owner` must match a strict GitHub
login/org pattern (`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`). If either check fails, skip the board add
and warn why. If the `[tracker]` table is absent entirely, silently skip the board add — it is
simply not configured, not an error.

**Graceful degradation.** If `gh` is missing, or `gh auth status` reports not authenticated, skip
issue creation entirely and print the issues as markdown instead, so the plan is still usable.

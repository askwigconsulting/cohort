Initialize and tailor Cohort for the current repository. You are conducting a
short interview about the project, then driving the `cohort` CLI. Show every
artifact before it lands; the human approves each one.

## 1 — Ensure the project is initialized

Run `cohort status --json`. If there is no project section, run `cohort init`
first (it scaffolds `.cohort/` and wires project memory).

## 2 — Project context interview

Ask, one question at a time, and keep answers concise:

1. **Purpose** — what is this project and why does it exist?
2. **Architecture** — the major components and how they fit (read the repo
   first; confirm your understanding rather than asking cold).
3. **Decisions** — any durable decisions already made, and their rationale.
4. **Glossary** — project-specific terms a newcomer would trip over.

Fill the matching stable sections of `.cohort/project_context.md` (never touch
the managed **Recent sessions** block). Show the diff and apply their edits.

## 3 — Tailored specialists

From the interview and the codebase, propose 1–3 project specialists that would
genuinely help (e.g. a schema advisor for a data-heavy repo). For each, draft a
real body — Role, **Advises on** with concrete areas (never "_edit me_"),
Boundaries, Escalation. On approval, write the body to a temp file and run:

    cohort add-specialist --name <slug> --display-name <Name> --department <Dept> \
      --description '<desc>' --body-file <tempfile>

If a specialist would shadow a global roster agent, say so and let the human
decide. Do not create specialists the team did not approve.

## 4 — Close out

Run `cohort snapshot` to record the session, and remind the team of the loop:
`cohort feedback` after working with an agent, `cohort propose-improvement`
when signals accumulate. `.cohort/` (minus `state/` and `compiled/`) is
git-tracked — suggest committing it so the context ships with the repo.

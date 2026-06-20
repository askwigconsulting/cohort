# Cohort example — a consuming repo

This walks the full Cohort journey in one place, so a newcomer can see the distinctive features
working together. Run it from inside a git repo after installing the office roster:

```bash
cohort init                       # scaffold <repo>/.cohort/ + wire project memory
cohort add-specialist --name data-modeler --display-name DataModeler \
    --department Data --description 'Schema and data modeling.'
cohort snapshot                   # write a dated session entry (conflict-free)
cohort context refresh            # roll sessions into project_context.md
cohort weekly-report              # generate <repo>/.cohort/reports/weekly-<date>.md
cohort feedback --rating up --agent data-modeler
cohort propose-improvement        # Steward drafts an improvement proposal
cohort submit-proposals           # → draft PR (human reviews + merges)
cohort status                     # see roster, specialists, staleness, wiring
```

What you'll see afterwards:

- `<repo>/.claude/agents/data-modeler.md` (and `.codex/` / `.cursor/` equivalents) — the project
  specialist, **isolated to this repo** and invisible to others.
- `<repo>/.cohort/sessions/<UTC>-<id>.md` — one file per snapshot (no merge conflicts).
- `<repo>/.cohort/reports/weekly-<date>.md` — a deterministic dated report.
- `<repo>/.cohort/proposals/` — a `kind: improvement` proposal staged for a human-reviewed draft PR.

`cohort deinit --purge` returns the repo to clean. The global office roster in `~/.claude` etc. is
never touched by anything you do in a project — that's the isolation boundary.

This journey is exercised end-to-end by `tests/test_phase9.py::test_full_system_e2e_all_three_ides`.

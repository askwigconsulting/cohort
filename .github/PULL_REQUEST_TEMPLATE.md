<!--
Cohort PR checklist. Keep the invariants in mind — canonical is law, renderers are
pure, the loop never edits canonical/ or auto-merges. See CONTRIBUTING.md.
-->

## What & why

<!-- What this changes and the reason. Link the issue it closes. -->

Closes #

## How it was verified

<!-- Commands run, behaviour observed. -->

- [ ] `python -m pytest` passes
- [ ] `cohort validate ./canonical` passes
- [ ] Behavioural test added/updated for new or changed surface
- [ ] Parity respected — rendered, or declared in `adapters/<ide>/parity-gaps.toml`
- [ ] Compiled outputs not hand-edited (they're derived from `canonical/`)

## Notes

<!-- Invariants touched (decisions [J]–[O]), risks, follow-ups, or "n/a". -->

# Governance

How changes get into Cohort — the office harness itself, and the shared office
roster it ships.

## The default: changes land by reviewed pull request

Every change to `canonical/` (the roster and other office artifacts) or to the
harness code is expected to land through a **pull request that a contributor
approves**. This is the norm the project is built around:

- **The self-improvement loop is human-gated by construction.** `cohort
  propose-improvement` + `cohort submit-proposals` can only ever open **draft
  PRs** — Cohort structurally cannot edit `canonical/` in place or merge/push a
  default branch (proven by tests). A human reviews and merges every proposal.
- **CI must pass.** The Ubuntu + Windows matrix, schema lint, and the full test
  suite gate every PR; branches must be up to date with `master` before merge.
- **Canonical is law.** Review focuses on the canonical source, never the
  compiled output (which is regenerated).

## The single-maintainer exception (current state)

Cohort currently has **one maintainer**. Requiring a *second* person's approval
before any merge would halt all progress, so while there is a single maintainer:

- **The maintainer may self-approve and merge their own PRs.** Being the sole
  contributor, they act as both author and approver — the PR + green CI is the
  record, and self-merge is acceptable.
- This is an explicit, temporary relaxation of the default, not a new default.

**When a second maintainer joins, this exception ends**: PRs then require an
approving review from a maintainer *other than the author*, and self-merge of
one's own changes stops. Contributions from non-maintainers always require a
maintainer's approving review.

## Maintainers

- Jonathan Askwig (`@askwigconsulting`) — sole maintainer.

To propose a new maintainer, open an issue; existing maintainers decide by
consensus. Add the new maintainer here and drop the single-maintainer exception
above in the same PR.

## Contributing content back (the office roster)

The roster is shared. A company running its own fork is its own office; changes
flow **down** via `cohort update` (fast-forward only) and **up** via draft PRs.
Personal customizations live in *my office* (`~/.cohort/my/`) and never enter a
PR unless the author explicitly promotes them (`cohort promote --to office` /
`--to office` authoring). See [CONTRIBUTING.md](CONTRIBUTING.md) for the mechanics.

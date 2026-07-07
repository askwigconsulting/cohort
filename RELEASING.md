# Releasing Cohort

Cohort follows [Keep a Changelog](https://keepachangelog.com) and [SemVer](https://semver.org).
Features land under `CHANGELOG.md`'s `## [Unreleased]`; a **release** dates them
and bumps the version. Pre-1.0, a **minor** bump (`0.4 → 0.5`) may include
breaking changes; use a **patch** bump (`0.4.0 → 0.4.1`) for fixes only.

The version is single-sourced in two files kept in lockstep —
`pyproject.toml` and `cli/cohort/__init__.py` — and `tests/test_version.py`
fails if they disagree or if the current version has no CHANGELOG section.

## Cut a release

`scripts/release.py` does the mechanical steps atomically (it edits files only —
it never commits, pushes, or tags, so you stay in the loop):

```bash
# 1. Make sure everything you want is under [Unreleased] and the suite is green.
python -m pytest -q; echo "exit=${PIPESTATUS[0]}"      # NOT `| tail` — that hides the code

# 2. Cut the version bump + CHANGELOG roll (pick the version and a one-line theme).
python scripts/release.py 0.5.0 --title "Org profiles"

# 3. Review the diff, then follow the git commands it prints:
git checkout -b release/0.5.0
git add -A && git commit -m "Release 0.5.0 — Org profiles"
#    open a PR, let CI pass, merge to master, THEN tag the merged commit:
git tag -a v0.5.0 -m "Release 0.5.0 — Org profiles" && git push origin v0.5.0
```

The script bumps both version files, moves the `[Unreleased]` backlog into a
dated `## [0.5.0] — <today> · Org profiles` section (leaving a fresh empty
`[Unreleased]`), and updates the compare links at the foot of the CHANGELOG.

## Guardrails

The script fails closed on the mistakes that have actually bitten:

- **Empty `[Unreleased]`** → refuses (nothing to release).
- **Version not greater than current**, or not `X.Y.Z` → refuses.
- **A `## [X.Y.Z]` section already exists** → refuses.
- Version files are always written together, so they can't drift.

To verify the repo is release-consistent at any time (e.g. in a pre-push hook):

```bash
python scripts/release.py --check
```

## Why this exists

0.3.0 sat unreleased while ten merged PRs piled up under `[Unreleased]` — the
version never moved because the release step is manual and easy to forget. This
script makes the step one command and the invariants enforceable.

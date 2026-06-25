# Contributing to Cohort

Thanks for your interest. Cohort's design rule is **canonical is law**: the files
under `canonical/` are the single source of truth, and everything else is
*compiled* from them. Keep that invariant in mind for any change.

## Development setup

```bash
git clone https://github.com/<you>/cohort.git
cd cohort
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest        # full suite must pass
cohort validate ./canonical
```

On **Windows**, first allow local scripts once
(`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`), then use
`py -m venv .venv; .\.venv\Scripts\Activate.ps1`, and set
`PYTHONUTF8=1` before running the suite (`$env:PYTHONUTF8="1"`) so file/subprocess
text I/O defaults to UTF-8 instead of the console codepage. CI sets this for you.
Symlink-mechanics tests are skipped on Windows (Cohort uses copy-mode there).

## Ground rules

- **Tests alongside code.** Test behaviour, not implementation. New CLI surface
  needs a behavioral test.
- **Parity is render-or-declared-gap.** If an IDE can't express something,
  declare it in `adapters/<ide>/parity-gaps.toml` — don't silently drop it.
- **Renderers are pure.** They produce byte-stable output; placement is the
  executor's job.
- **The self-improvement loop never edits `canonical/` and never auto-merges.**
  Preserve those invariants (they're enforced by tests in `tests/test_phase8.py`).

## Submitting proposals through the loop

`cohort submit-proposals` opens a **draft** PR per entry in `.cohort/proposals/`.
By default it pushes a feature branch to the *source repo's* `origin` and opens
the PR there — which works if you have push access to that remote.

**If you cloned Cohort and don't have push access to the upstream**, fork it
first, then target your fork explicitly:

```bash
cohort submit-proposals --repo <you>/cohort
```

If `gh`/the remote is unavailable, the command degrades cleanly: your proposals
stay as files in `.cohort/proposals/` for manual PR creation, and your working
tree is always restored to the branch you started on.

## Commit conventions

- Imperative mood, ≤50-char subject (`Add X`, not `Added X`).
- One logical change per commit.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) — please don't open a
public issue for it.

Update this Cohort install to the latest upstream version.

First run `cohort update --dry-run` to preview the incoming commits and changed
artifacts without touching anything. Then run `cohort update` to fast-forward the
clone, reinstall the package if its dependencies changed, and recompile every
installed IDE.

Cohort refuses to update a dirty or diverged working tree — commit, stash, or
reconcile first. Updates are never silent: nothing changes until you run the
command, and only a clean fast-forward is ever applied.

**Trust boundary.** A fast-forward still runs the pulled commits' build/setup code
(`pip install -e`) and compiles their artifacts, so the residual risk is a
*compromised upstream*: a malicious commit that is nonetheless a valid
fast-forward. To gate against it, set `require_signed = true` under `[update]` in
`~/.cohort/cohort.toml` — Cohort then verifies the upstream tip with
`git verify-commit` before merging and refuses (exit 1) unless the commit is
signed by a key your git trusts (configure `gpg.ssh.allowedSignersFile` or a GPG
keyring). The flag is off by default, so the common clone-and-go flow is unchanged.

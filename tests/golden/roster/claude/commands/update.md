---
description: Update Cohort to the latest upstream version and recompile your IDEs.
---

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
`git verify-commit` before merging and refuses (exit 1) unless the commit carries
a signature git accepts.

The gate is only as strong as *which* key you trust, and this is on you to
configure: `git verify-commit` answers "signed by a key my git trusts," not
"signed by the Cohort maintainer." For it to mean anything, **pin the signer** —
set `gpg.ssh.allowedSignersFile` to a file containing only the maintainer's SSH
public key (recommended), or import and fully trust their GPG key. Note plain GPG
verification passes for *any* good signature from a key in your keyring regardless
of ownertrust, so an unpinned keyring is a weak bar. `~/.cohort/cohort.toml` is
therefore security-sensitive — it selects the update source and toggles this
enforcement; keep it user-owned and not world-writable. The flag is off by
default, so the common clone-and-go flow is unchanged.

For a stronger, identity-pinned tier, set `signed_by` to a list of trusted key
fingerprints — the update then additionally requires the tip's *signing key* to
match one you pinned (not merely any key git trusts), and a non-empty `signed_by`
implies `require_signed`:

```toml
[update]
signed_by = ["SHA256:nuK/x67qH8e3I0UWKQQOTG5ggGCHcWrIfbVy810dHto"]
```

Get an SSH key's fingerprint with `ssh-keygen -lf key.pub` (the `SHA256:…` field),
or a GPG key's *full* fingerprint with `gpg --fingerprint` (a short/long key-id is
not enough — pin the whole fingerprint). Give it as a TOML array of strings, on one
line or several.

# RFC 0002 — Trust bootstrap & signing-key rotation (design spike)

Status: **Draft spike (revised after SecurityEngineer review)** · Depends on:
RFC 0001 (#121) · Tracks: #125 · Gates: #124 · Owner: maintainer

> **Review correction (load-bearing):** the v1 draft claimed an offline root would
> stop a compromised *release* key from rotating pins. With a flat `signed_by`
> array that is **false** — any pinned key can rewrite the pin set. Delivering that
> containment property **requires a key role in the data model** (a distinct root
> set), and rotation must be a **verified chain**, not a single `git log -1`. Both
> are now in the design (§3, §4). Freeze/rollback (§3/§6) and the out-of-band
> channel's integrity (§1) are the documented residuals.

## Why this spike

RFC 0001's review found that the one blocking part of `--profile enterprise` is
the **trust bootstrap**: how the *first* signing pin arrives, and how pins
**rotate** and **revoke** across a fleet. This spike turns that into an
implementable design. It is security-critical and deliberately small in code —
most of the mechanism already exists.

## What already exists (the good news)

`cli/cohort/update.py` already implements the hard cryptographic part:

- **Multi-key pins.** `signed_by` is a TOML array of fingerprints (SSH
  `SHA256:…` or full GPG), read single- or multi-line, comments stripped
  (`_signed_by`, `_update_array_text`). A pinned *set*, not a single key, is the
  existing shape — so day-to-day multi-key verification needs no new data model.
  **But** distinguishing *who may rotate pins* from *who may sign office commits*
  (§4) does need a new field — a `root_keys` set — because a flat array gives every
  pinned key equal authority to rewrite the pin set.
- **Whole-token match against the real signer.** `_signing_key_fingerprints`
  parses `git verify-commit --raw` and matches the **actual signing key**
  fingerprint against the pinned set — a user-id embedding a pinned string can't
  impersonate the key (#105).
- **Verify-then-merge, fail-closed.** The tip is verified *before* fast-forward
  (TOCTOU-closed); `require_signed` returns `True` on an unreadable/locked config,
  so a corrupt file refuses unsigned updates rather than degrading.
- **3.10-safe reader.** Pins are read with a stdlib scanner, not `tomllib`
  (absent on 3.10) — the same treatment `org.toml` will need.

**Where pins live today:** the *local* global `~/.cohort/cohort.toml`. That file
is the operative trust root the client already trusts. This spike's job is only
to (a) establish that root **out-of-band at first run**, and (b) let it **change
safely** over time.

## The gap this spike closes

1. **Bootstrap** — nothing establishes the first pin for a fresh enterprise
   install; today a human hand-edits `cohort.toml`.
2. **Pin propagation / rotation** — `org.toml` (office repo, RFC 0001) will carry
   the org's recommended pins, but a pin *change* arriving in the repo must not be
   trusted just because it fast-forwards; it must be signed by an already-trusted
   key.
3. **Key material** — `git verify-commit` needs the signer's public key trusted
   locally (GPG keyring / `gpg.ssh.allowedSignersFile`). The fingerprint pin says
   *which* key; the key *material* is the true anchor and is not yet distributed.

## Design

### 1. Two things must arrive out-of-band, together

The onboarding channel (MDM / IdP page / signed onboarding doc — **not** the
office repo) delivers, as one unit:

- the **allowed-signers material** (the public key(s), as an
  `allowedSignersFile`), and
- the **pin fingerprint(s)** for `signed_by`.

```
cohort setup --profile enterprise \
  --company-url git@github.com:acme/cohort-office.git \
  --signers-file ./acme-cohort-signers   # public keys, out-of-band
  # fingerprints are derived from the signers file; --signed-by SHA256:… may
  # additionally pin a subset, or assert the expected fp for a belt-and-suspenders check
```

`--signers-file` is the key material; `--signed-by` is the fingerprint assertion.
For `--profile enterprise`, **`--signed-by` is required** (not merely "at least
one") — see §5/§6: the fingerprint assertion is what makes a stale or
over-broad signers file safe, so it is the load-bearing input, not optional.

**Channel integrity (the true root of trust).** MDM, an IdP page, and a "signed
onboarding doc" are **not** interchangeable, and the whole system rests on this
channel:

- **MDM / device management** — pushes file + fingerprint under device trust;
  acceptable integrity. Preferred.
- **IdP page over TLS** — a phished or MITM'd page substitutes *both* the signers
  file and the fingerprint together, owning trust undetectably. Weaker; only
  acceptable if the fingerprint is delivered by a *different, higher-integrity*
  path than the material.
- **"Signed onboarding doc"** — signed by *what* key? Needs a pre-existing root,
  so it cannot itself be the bootstrap anchor.

Required property: **the `--signed-by` fingerprint must arrive with strictly
higher integrity than the key material.** If both travel the same channel, the
fingerprint assertion adds nothing (an attacker who tampers the material tampers
the fingerprint too). This is the crux; it must be specified in the enterprise
onboarding runbook before `--profile enterprise` ships.

### 2. Verify-before-parse (the bootstrap sequence)

```
1. clone --no-checkout, hardened (see below), at the requested branch
2. SHA = git rev-parse <branch>            # pin to one concrete SHA, like do_update
3. configure gpg.format=ssh + gpg.ssh.allowedSignersFile = <installed signers file>
4. git verify-commit SHA                   # against the out-of-band key material
5. parse `git verify-commit --raw SHA`; if fail OR (signer fp ∉ asserted pins) → ABORT
6. read org.toml atomically from the verified object:
       git show SHA:org.toml   (never `checkout` — see below)
7. apply [update] pins/root_keys to local cohort.toml
```

Two corrections from review, both load-bearing:

- **Read the verified object, never the working tree.** Do *not* `checkout`.
  Resolve to one SHA (step 2) and read `git show SHA:org.toml`. This is atomic with
  the verification (no TOCTOU between verify and read — the same bug `do_update`
  already avoids by SHA-pinning), and it sidesteps `.gitattributes` smudge/eol
  transformation on the very file we're about to trust. Verifying only the tip *is*
  sufficient for content integrity: the signature covers the tree SHA, so
  `org.toml` read from the verified SHA is Merkle-bound to it; a replaced blob
  breaks the signature. Unsigned ancestor history is irrelevant.
- **Harden the clone** so steps 1–6 can't be subverted by a hostile office repo:
  no submodule recursion (CVE-2024-32002-class RCE), `protocol.file.allow=never`,
  `core.symlinks=false`, no local hook injection (`core.hooksPath` to an empty
  dir; `init.templateDir=`), and `gpg.format=ssh` pinned so `verify-commit` can't
  fall through to the local GPG keyring instead of the allowed-signers file.

The invariant: **no byte of `org.toml` is trusted until the commit carrying it is
verified against an out-of-repo anchor, and it is read from that verified SHA, not
from a mutable working tree.**

### 3. Pin change = a verified rotation *chain*, authorized only by root keys

The v1 "the commit that last changed `signed_by`, signed by any current key" rule
had three holes (all from review): committer date is attacker-set so "last
changed" is forgeable; a single-hop check lets a behind/fresh client jump to a new
set without verifying the intermediate steps; and a merge can reintroduce an
old-but-signed change. The corrected model:

**`org.toml` carries a monotonic `[trust].version` integer and, for each version,
the `signed_by` set + `root_keys` set (§4).** On `cohort update`, after the office
repo's *ff-only, refuse-diverged* fetch (same discipline `do_update` already
enforces, `update.py` 599–604/676 — the office repo must get it explicitly so a
force-push can't rewrite rotation history):

```
walk every commit that changed [trust] between the client's recorded version
and the verified tip, in first-parent topological order:
  for each step  vN → vN+1  (introduced by verified commit C):
    require  C signed by a key in vN.root_keys        # ROOT authority, not any pin
    require  vN+1.version == vN.version + 1            # no skips, no reorders
  # each step is checked against the set in force AT THAT POINT — a real chain
if the whole chain verifies → adopt vTip's sets into local cohort.toml, loudly:
    "signing trust v{old}→v{new}: pins <…>→<…>  (each step authorized by a root key)"
else → keep the current local sets; refuse; warn
reject any org.toml whose [trust].version < the client's recorded version   # rollback floor
```

This is genuine TUF-style root rotation: every `N → N+1` transition is verified,
each by the root set in force before it, so authority can only move through an
unbroken chain of root signatures. Normal rotation of a *release* key: bump
`version`, change `signed_by`, sign the commit with a **root** key; clients walk
and adopt. The `version` floor blocks rollback to a superseded set; first-parent
topological order kills the merge-reintroduction and date-forgery tricks.

Correction to the v1 wording: this does **not** merely "grow" the trusted set —
a step can shrink it. What is monotone is `version`, and only a root key can
advance it.

### 4. A key *role* in the data model — not a convention

For the containment property to be real, "who may rotate pins" must be a
*different, enforced* set from "who may sign office commits". So `org.toml` (and
local `cohort.toml`) gain a distinct **`root_keys`** field alongside `signed_by`:

```toml
[trust]
version   = 7
root_keys = ["SHA256:<offline-root-fp>"]              # may ONLY rotate [trust]; kept offline
signed_by = ["SHA256:<release-fp>", "SHA256:<offline-root-fp>"]  # may sign office commits
```

- **Day-to-day office commits** (what `do_update` verifies before fast-forward)
  are checked against `signed_by` — the existing whole-token match, unchanged.
- **A `[trust]` change** (§3) is checked against `root_keys` — a *separate*
  membership test.

Now a compromised **release** key can sign malicious office commits (still gated
by the normal verify-then-merge and any project review), but it is **not in
`root_keys`**, so a `[trust]` rewrite it signs is refused by §3 — it cannot lock
the fleet onto an attacker pin. Recovery from release-key compromise stays in-band:
bump `version`, rotate `signed_by`, sign with the offline root. Only compromise of
a **root** key forces the out-of-band re-bootstrap of §6 — which is why the root is
kept offline and used only for rotations.

This is the "new data model" the v1 draft wrongly said was unnecessary: small (one
field) but it is what makes the containment real rather than decorative. Enterprise
setup **refuses** an `org.toml` with empty `root_keys` — a single key serving both
roles gives no containment at all.

### 5. Fail-closed everywhere in the enterprise path

- No `--signers-file`/`--signed-by`, or a missing/parse-erroring/`[trust]`-less
  `org.toml`, or empty `root_keys` → **abort** enterprise setup (never a degraded
  ~individual install).
- **`--signed-by` is required for `--profile enterprise`. No TOFU.** The v1
  "omit `--signed-by`, confirm the fingerprint interactively" degrade is
  **removed** for enterprise (it is trust-on-first-use over whatever the remote
  serves — unsafe during a freeze/compromise window; see §6). The interactive-TOFU
  path is reserved for the *individual* profile only.
- `org.toml` is read with the stdlib scanner (reuse `_update_array_text` /
  `_update_table_value`), so 3.10 doesn't silently skip the security keys — **but
  the reuse must not inherit their fail-open default.** `_signed_by` returns `[]`
  on unreadable/partial input and `do_update` treats `[]` as "fall through to the
  weaker `require_signed` tier" (`update.py` 642–662); an *unterminated* array
  returns a partial buffer (133–134). For enterprise bootstrap, "scanner returned
  empty / partial / error" must be a hard **abort**, not a downgrade to `[]` or to
  the weaker tier. The new setup code checks this explicitly.

### 6. Revocation, freshness & compromise recovery

There is no negative pin ("key X is revoked") and signatures never expire.
Consequences and mitigations (from review):

- **Freeze / rollback (documented residual, mitigated).** Signatures don't expire,
  so an attacker controlling the remote/mirror/transport can **withhold newer
  commits** and serve a stale tip — a fresh or behind client never learns a
  rotation happened, and a key rotated *away* for compromise stays a valid signer
  of the old tip. Mitigations, both in the design: the `[trust].version` **rollback
  floor** (§3 — refuse any `org.toml` with a lower version than last recorded), and
  **ff-only, refuse-diverged** office fetch so a force-push can't rewrite history.
  These stop rollback for a client that has *ever* seen the newer version; a
  *brand-new* install during a freeze window is still exposed — hence §1's channel
  and mandatory `--signed-by` (a fresh install pins to the asserted fingerprint,
  not to whatever the frozen remote serves). Expiry/timestamping is the fuller TUF
  answer and is out of scope for v1; the version floor is the cheap real mitigation.
- **Signers-file never shrinks (documented residual).** Rotation changes the
  fingerprint *pins*; it does **not** update the out-of-band `allowedSignersFile`
  *material*. `verify-commit` still cryptographically accepts a removed key if its
  material remains in the file — the pin membership check is what actually rejects
  it. So removing a key's authority requires **re-distributing the signers file**,
  and the mandatory `--signed-by` assertion (§2 step 5) is what keeps a fresh
  install safe against a stale signers file in the meantime.
- **Release-key compromise** → rotate via §3, signed by the offline root. In-band.
- **Root-key compromise** → **out-of-band re-bootstrap** via §1's channel. No
  in-band recovery once the only rotation authority is the attacker's — which is
  why the root is offline and single-purpose.

## Code impact (small, mostly new-at-the-edges)

The cryptographic primitive is reused; the new code is at the edges. The review
did move two items from "reuse" to "new": the **`root_keys` role** and the
**verified rotation chain** are the substance, not glue.

| Area | Change |
|------|--------|
| `update.py` `signed_by` verify | **reuse** — multi-key whole-token verify, unchanged |
| `root_keys` + `[trust].version` | **new field + reader** — separate rotation-authority set and rollback counter |
| `setup --profile enterprise` | **new** — hardened clone, SHA-pin, verify tip, `git show SHA:org.toml`, write `[trust]` |
| `cohort update` trust step | **new** — walk the verified `vN→vN+1` chain (root-signed each step), rollback floor, loud print |
| `org.toml` reader | **new** — fail-closed (abort on empty/partial), stdlib scanner shared with update.py |
| office fetch | **explicit ff-only / refuse-diverged** (mirror `do_update` 599–604/676) |
| CLI | `--signers-file` (required material), `--signed-by` (required assertion); optional `cohort trust show` |

No change to the compile/merge path; no new write path for artifacts.

## Test plan

- **Bootstrap:** tip signed by the anchor → proceeds; tip unsigned or signer ∉
  asserted pins → aborts, nothing placed; `org.toml` read via `git show SHA:` only,
  never on the abort path; no `--signed-by` under `--profile enterprise` → abort
  (no TOFU).
- **Role enforcement (§4):** a `[trust]` change signed by a `signed_by`-but-not-
  `root_keys` (release) key → **refused**; signed by a `root_keys` key → applied.
  Empty `root_keys` → setup aborts.
- **Chained rotation (§3):** valid `vN→vN+1` chain each root-signed → adopted; a
  step signed by a non-root key → refused; a skipped version / reordered / merge-
  reintroduced change → refused; forged committer-date "latest" → refused (walk is
  version+topology, not date).
- **Rollback floor:** an `org.toml` with `version` < recorded → refused.
- **Fail-open guard:** unterminated/partial/empty `signed_by`|`root_keys` from the
  scanner → abort, never a downgrade to the weaker `require_signed` tier or `[]`.

## Open questions for the maintainer

1. **`root_keys` threshold?** v1 assumes "≥1 root signature per rotation step."
   Worth a *threshold* (e.g. 2-of-N root sigs, real TUF) for large orgs, or is
   single-root-signature enough for v1?
2. **`--signers-file` distribution** — ship as a file path (assumes MDM drops it),
   or also allow an out-of-band URL fetched then verified against `--signed-by`?
   (Note §1: only meaningful if the fingerprint has higher integrity than the URL.)
3. **Do pins flow from `org.toml` at all, or stay purely local?** Local-only is
   simplest (rotation = MDM pushes a new `cohort.toml`) but loses git-native
   propagation and drops all of §3's chain machinery. Trade git-native rotation for
   less code — a real fork worth deciding before implementing #124.
4. **`cohort trust` surface** — a read-only `trust show` (current pins, root_keys,
   version, last rotation) for v1, or defer?
5. **Expiry/freshness** — accept the freeze residual with the version floor (v1), or
   invest in a timestamp/expiry mechanism (fuller TUF, more moving parts)?

## Prior art referenced

`update.py`: `_signed_by`, `_signing_key_fingerprints`, `_require_signed`,
`_commit_signer_allowed`, `_update_array_text`/`_update_table_value` (stdlib
scanner), and the SHA-pinning + ff-only discipline in `do_update` (599–628/676).
[TUF](https://theupdateframework.io) root rotation (chained, threshold-signed root
metadata; timestamp/snapshot roles for freshness) is the direct analogue for §3/§6.

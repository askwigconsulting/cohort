# RFC 0002 — Trust bootstrap & signing-key rotation (design spike)

Status: **Draft spike** · Depends on: RFC 0001 (#121) · Tracks: #125 · Gates: #124
Owner: maintainer

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
  existing shape — so "pin ≥ 2 keys" needs no new data model.
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

`--signers-file` is the anchor; `--signed-by` (RFC 0001) is the fingerprint
assertion. At least one must be present for `--profile enterprise`, or setup
refuses (fail-closed — see §5).

### 2. Verify-before-parse (the bootstrap sequence)

```
1. clone --no-checkout the office repo at the requested branch
2. configure gpg.ssh.allowedSignersFile = <installed signers file>
3. git verify-commit <tip>           # against the out-of-band key material
4. if fail OR (signer fp ∉ asserted pins) → ABORT, place nothing
5. only now: checkout, parse org.toml, apply [update] pins to local cohort.toml
```

The invariant: **no byte of `org.toml` is trusted until the commit carrying it is
verified against an anchor that came from outside the repo.** This reuses the
existing verify path; the new part is doing it *before* reading org.toml, inside
setup.

### 3. Pin change = chained rotation (TUF-style root rotation)

Pins live in local `cohort.toml`; `org.toml` is the *recommended* source. On
`cohort update`, after the normal verified fast-forward:

```
if org.toml[update].signed_by  !=  local cohort.toml signed_by:
    let C = the commit that last changed org.toml's signed_by
    if C is signed by a key in the CURRENT (pre-change) local pin set:
        apply the change to local cohort.toml
        print:  "signing pins rotated: <old fps> → <new fps>  (authorized by <signer fp>)"
    else:
        refuse the change; keep the old pins; warn loudly
```

So a new key is only ever trusted because an **already-trusted** key vouched for
it in a signed commit. Normal rotation:

1. add the new key's fingerprint to `org.toml` `signed_by`, in a commit signed by
   an **existing** pinned key → clients adopt it (set now has old + new);
2. overlap window (both valid);
3. remove the old fingerprint, in a commit signed by a still-valid key → clients
   drop it.

### 4. Two keys, one offline root

Convention (documented, enforced only by refusing single-key enterprise pins if
we choose to): pin **≥ 2** keys —

- an **offline root**, used *only* to sign `org.toml` / pin-rotation commits, kept
  offline; and
- a **release key**, used for day-to-day office commits.

Rationale: a compromised *release* key can sign malicious office commits, but
**cannot rotate the pins** to lock you out, because pin changes are only honored
from the still-safe offline root — recovery stays in-band (rotate the release key,
signed by the root). Only compromise of the *root* forces §6.

### 5. Fail-closed everywhere in the enterprise path

- No `--signers-file`/`--signed-by`, or a missing/parse-erroring/`[update]`-less
  `org.toml` → **abort** enterprise setup (never a degraded ~individual install).
- `org.toml` is read with the stdlib scanner (reuse `_update_array_text` /
  `_update_table_value`), so 3.10 doesn't silently skip the security keys.
- If `--signed-by` is omitted, degrade *explicitly*: show the tip's fingerprint,
  require interactive confirmation, record it, refuse later silent pin changes.

### 6. Revocation & compromise recovery

There is no negative pin ("key X is revoked") and no expiry — deliberately, to
keep the model a simple monotone "trusted set changed by trusted keys." Recovery:

- **Release-key compromise** → rotate it via §3 (offline root signs the change).
  In-band, no re-bootstrap.
- **Offline-root compromise** (or sole-key compromise if the ≥2 convention was
  ignored) → **out-of-band re-bootstrap** through the same channel as §1. There is
  no in-band recovery once the only trusted key is the attacker's — which is
  exactly why the offline root exists and why §1's channel must exist.

## Code impact (small, mostly new-at-the-edges)

| Area | Change |
|------|--------|
| `update.py` verify | **reuse** — multi-key whole-token verify already there |
| `setup --profile enterprise` | **new** — clone-verify-before-parse, install signers file, write pins |
| `cohort update` | **new step** — detect `org.toml` pin change, apply iff chained; loud print |
| `org.toml` reader | **new** — fail-closed, stdlib-scanner (share the update.py scanner) |
| CLI | `--signers-file`, `--signed-by` (RFC 0001); optional `cohort trust show` |

No change to the compile/merge path; no new write path for artifacts.

## Test plan

- Bootstrap: tip signed by the anchor → proceeds; tip unsigned or signer ∉ pins →
  aborts, nothing placed; `org.toml` never read on the abort path.
- Chained rotation: change signed by an old-pinned key → applied; change signed by
  a non-pinned key → refused, old pins kept.
- Offline-root model: release-key-signed pin change → refused (release key can't
  rotate); root-signed → applied.
- Fail-closed: missing signers/`org.toml`/`[update]` → abort; 3.10 reader path
  refuses unsigned.
- No silent pin change: omitted `--signed-by` requires confirmation.

## Open questions for the maintainer

1. **Enforce ≥ 2 keys?** Refuse a single-key enterprise pin, or just document the
   offline-root convention? (Refusing is safer; slightly more onboarding friction.)
2. **`--signers-file` distribution** — ship as a file path (assumes MDM drops it),
   or also allow an out-of-band URL fetched then verified against `--signed-by`?
3. **Do pins flow from `org.toml` at all, or stay purely local?** Local-only is
   simplest (rotation = MDM pushes a new `cohort.toml`) but loses git-native
   propagation. This spike assumes org.toml-propagation-with-chaining; the
   local-only alternative drops §3 entirely.
4. **`cohort trust` surface** — is a read-only `trust show` (print current pins +
   last rotation) worth it for v1, or defer?

## Prior art referenced

`update.py`: `_signed_by`, `_signing_key_fingerprints`, `_require_signed`,
`_commit_signed`, `_update_array_text`/`_update_table_value` (stdlib scanner).
TUF root rotation (chained, threshold-signed root metadata) is the direct analogue
for §3.

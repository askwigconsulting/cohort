# RFC 0001 — Multi-install: individual vs enterprise, org config, and org sync

Status: **Draft (revised after CloudArchitect + SecurityEngineer review,
reconciled by ChiefOfStaff)** · Target: post-0.4.0 · Owner: maintainer

## Summary

Package Cohort's already-distributed architecture into two supported install
**profiles** — *individual* and *enterprise* — and a company-owned `org.toml`
that carries policy and defaults. Add lightweight org-level **visibility** and
**signals aggregation** on the existing git substrate. Defer a centralized
**server** behind a decision framework: if one is ever built, it is a read-model
/ coordination service over the same git source of truth — never a second write
path, and never a distributor of trust roots.

The architecture (git-native hub-and-spoke, no second write path) is endorsed by
both reviews. The one thing that must be right before `--profile enterprise`
ships is the **trust bootstrap**: the first signing pin must arrive out-of-band
(not from the repo it verifies), pins must be multi-key with an offline root and
rotate through a chained/loud flow, and enterprise setup must fail closed. One
present-tense code gap (the `ext::`/`fd::` transport ban missing from
`update.py`'s git wrapper) is pulled out as a **pre-Phase-1 fix**, because
`org.toml` is what weaponizes it.

## Motivating goal

*A group of people, each running their own Cohort instance, collaborating in one
office* — with an enterprise story (private office, signed updates, provisioned
sync, curated rosters) that is a **profile and policy layer**, not a new
architecture.

## Background: Cohort is already distributed (git at every tier)

Nothing here invents a sync system. Cohort already synchronizes through git at
each of its three tiers ([P] in DESIGN.md):

| Tier | Where it lives | How it syncs | Shared with |
|------|----------------|--------------|-------------|
| **Company** (office) | the source repo clone | `cohort update` fast-forwards it (opt-in signed) | the whole org |
| **User** (my office) | `~/.cohort/my` | `cohort my-office sync` (ff-pull → commit → push) | just you, across machines |
| **Project** | `<repo>/.cohort/` | rides in the repo's own git | anyone who clones the repo |

So "each person has their own instance, all in one office" is **today's model**.
The 0.4.0 work built exactly the primitives an enterprise deployment needs:

- **`update` trust boundary** — `require_signed` + `signed_by` pins the office
  upstream to the org's release key (a compromised-upstream, still-a-valid-ff
  commit is refused). This is the enterprise "the office you pull is really ours"
  guarantee.
- **my-office quarantine** — a pulled hook/memory on a *shared / multi-writer*
  remote is withheld until reviewed. This is precisely the multi-writer safety
  story for a provisioned team remote.
- **Reviewed-PR governance** — `submit-proposals` opens draft PRs; nothing
  auto-merges or edits canonical. This is the enterprise contribution path.

**The trust model is why git-native matters.** Git already provides sync,
offline operation, three-way merge, full history, signatures, and host-level
access control with zero server to operate. Every supply-chain guarantee we
just shipped rests on that. A bespoke sync protocol would recreate git badly and
reopen all of it.

## Enterprise vs individual is a profile, not an architecture

| Dimension | Individual (today's default) | Enterprise profile |
|-----------|------------------------------|--------------------|
| Office upstream | public repo / personal fork | company's private Cohort repo |
| Update trust | off (clone-and-go) | `require_signed` + `signed_by` **on**, pinned to the org key |
| my-office remote | optional, self-owned | provisioned on the company git host; quarantine (already default) covers multi-writer |
| Roster | full / self-tailored | org-curated per-role subset (`setup --agents`) |
| Contribution | ad-hoc PRs | `submit-proposals` → draft PR to the company repo |
| Governance | single maintainer | CODEOWNERS / per-directory review in the company-repo template |

Almost every enterprise knob already exists as a `setup` flag or config value.
The gap is **packaging** (a named profile + a company-owned config file), not new
machinery.

## Proposal

### `org.toml` — the company-owned policy/defaults file

Lives at the root of the **company office repo** (so it travels with `update`,
version-controlled, signed alongside the rest). Read-only from the client's
perspective; it *configures* the install, it does not grant it new powers.

```toml
[org]
name        = "Acme Corp"
office_url  = "git@github.com:acme/cohort-office.git"
office_branch = "main"

[update]                 # applied at install; still user-overridable locally
require_signed = true
# Pin AT LEAST TWO keys, one an OFFLINE ROOT used only to sign rotations. A single
# pinned key is fatal on compromise (the attacker signs a valid ff that rotates the
# pin to their own key). See "Trust bootstrap, key material, and rotation".
signed_by = [
  "SHA256:<acme-offline-root-fp>",   # offline root — signs only org.toml/pin rotations
  "SHA256:<acme-release-key-fp>",    # day-to-day release signing key
]

[roster]                 # role -> agent subset (extends `setup --agents`)
default = ["chief-of-staff", "counsel", "security-engineer", "cloud-architect"]
[roster.roles]
engineering = ["chief-of-staff", "security-engineer", "cloud-architect", "code-reviewer"]
finance     = ["chief-of-staff", "finance-analyst", "compliance"]

[my_office]              # optional provisioned personal-layer remote template
remote_template = "git@github.com:acme/cohort-my-{user}.git"
```

Precedence: **local user config wins over org defaults** — org sets *defaults and
recommended policy*, it does not lock the machine. Client-side "locked" config is
theater (the user owns the machine); real enforcement exists **only server-side**
(host branch protection, provisioned-repo ACLs, access revocation). So the posture
is **detection over prevention**: `[update]` values are applied to the local
`cohort.toml` at install and remain user-editable, but any divergence below org
policy is made **loud and observable** — `cohort update` warns on every run while
below policy, and `org-report` (Phase 2) surfaces per-install `require_signed` /
pin-match / office version (self-reported; it defeats drift and mistakes, not a
determined insider).

**Fail closed on enterprise setup.** A missing, unparseable, or `[update]`-less
`org.toml` must **abort** `--profile enterprise` — never silently degrade to a
near-individual install (which would drop `require_signed` from every new machine,
a fail-open an attacker could trigger by merely deleting/corrupting `org.toml`).
This must hold on the Python 3.10 floor too: `tomllib` is 3.11+, so `org.toml`'s
`[update]` keys need the same stdlib-scanner treatment as `update.py`'s
`_update_table_value`, or the enterprise profile hard-gates the Python version.

### `cohort setup --profile individual|enterprise`

- `--profile individual` — current defaults (clone-and-go, full roster, no
  provisioned remote).
- `--profile enterprise --company-url <repo> --signed-by SHA256:<fp>` — clone the
  office, **verify the cloned tip against the out-of-band `--signed-by` pin BEFORE
  parsing `org.toml`**, then apply `[update]` pins, install the role roster, and
  optionally provision the my-office remote. One URL + one pin → configured install.
- `--role <name>` selects a `[roster.roles]` subset.

Enterprise onboarding collapses to: **`cohort setup --profile enterprise
--company-url … --signed-by SHA256:… --role engineering`** — the URL and the first
pin distributed together via MDM / IdP / the onboarding one-liner.

**Why the `--signed-by` is not optional.** Without it the flow is circular TOFU:
setup would clone an *unverified* repo and read `signed_by` *from that clone*, so
an attacker controlling the transport or host **at onboarding** serves an
`org.toml` pinning **their** key — and every later `cohort update` then "verifies
successfully" against attacker pins, manufacturing false assurance. The first pin
is the trust anchor and must arrive out-of-band. If `--signed-by` is omitted,
setup must degrade *explicitly* (display the tip's fingerprint, require interactive
confirmation, record it, and refuse subsequent silent pin changes) — never
bootstrap pins invisibly.

### Trust bootstrap, key material, and rotation (blocking)

This is the one part of the RFC that must be right before `--profile enterprise`
ships; both the security and architecture reviews converge here.

- **The real anchor is key *material*, not the pin fingerprint.** `git verify-commit`
  needs the signer's public key trusted locally — the GPG keyring or
  `gpg.ssh.allowedSignersFile` (`update.py` already relies on it). The `signed_by`
  fingerprint only says *which* trusted key must have signed. So the true
  first-run trust distribution is **key material + fingerprint, out-of-band**
  (MDM/IdP/onboarding), and it **must not be the office repo itself**.
- **Verify before parse** (see `setup --profile enterprise`): the cloned tip is
  checked against the out-of-band pin *before* `org.toml` is read.
- **Pin at least two keys, one an offline root.** A single pinned key is
  unrecoverable on compromise: the attacker signs a valid fast-forward that rotates
  the pin to their own key, and clients that never pull a fix trust it forever.
  Keep an **offline root** whose only job is to sign `org.toml`/pin changes; the
  day-to-day release key signs ordinary commits.
- **Rotation is chained and loud** (TUF-style root rotation): a commit that changes
  `[update].signed_by` must itself be signed by an *already-pinned* key, and the
  client applies it visibly (print old → new fingerprints), never silently. Normal
  rotation = add the new key (signed by old), overlap window, then drop the old.
- **Sole-key compromise requires out-of-band re-bootstrap** — the same channel as
  the first pin. There is no in-band recovery once the only trusted key is the
  attacker's; the offline root exists precisely to avoid this state.

### Org visibility (fleet roll-up)

Every install already computes "N commits behind upstream" and each project
tracks staleness/signals in git. Add an **org roll-up** — a report (and/or a CI
job in the company repo) that reads across the org's project repos and answers
the first questions an enterprise buyer asks:

- Who is on which office version / how far behind?
- Whose project context is stale?
- Where are the low-rated agents / open improvement proposals, org-wide?

Implementation: a `cohort org-report` reading a **git-tracked repo registry**
(`registry.toml` in the office repo — the repo inventory is the first piece of
central state, and it belongs in signed git, not invented at Phase 3), emitting a
git-tracked markdown/JSON summary. No server, no new write path — a read-only
aggregator over existing signals, the same shape as `weekly-report` but across
repos. Two constraints from review:

- **Aggregate by cloning over the git protocol, not the host REST API.** A
  REST-based roll-up dies on rate limits at fleet scale (GitHub's `GITHUB_TOKEN`
  is ~1,000 req/hr per repo); `git fetch`/`ls-remote` has no such ceiling, and a
  broad read PAT is a large blast radius. The CLI is primary; any CI recipe is a
  thin GitHub-Actions-first wrapper with a documented seam (CODEOWNERS / MR /
  template-provisioning APIs differ across GitHub / GitLab / Gitea).
- **Privacy scope.** The roll-up reads **canonical + project** repos by default.
  Observing personal `~/.cohort/my` layers or session signals is
  employee-monitoring territory — **opt-in only**, never default. State this so
  "observe the fleet" never quietly becomes surveillance of the personal tier.

### Signals aggregation for the Steward loop

The self-improvement loop (Steward → propose → draft PR) currently sees
per-person / per-project slices. Feed it **org-wide** signals (feedback +
sessions aggregated read-only across project repos) so proposals reflect team
reality. Reuses `aggregate_signals`; changes the *scope* of what it reads, not
the loop's human-gated, canonical-never-edited invariants.

### Explicitly NOT proposed now

- **A fourth "team" tier.** A team office is just *another office repo*. If real
  demand appears, the actual design question is **multiple office upstreams**
  (compose company + team layers) — that deserves its own RFC before code, and
  interacts with the two-layer merge model ([P]). Don't add a tier speculatively.
- **Locked/enforced policy.** Org defaults, not org mandates, in v1.

## The centralized-server question — a decision framework, not a build

**Ask what a server adds that git does not.** Git already gives sync, offline,
merge, history, signatures, and access control — for free and battle-tested.

A server *genuinely* adds only:

1. **Real-time push** (vs. poll-on-`update`) — "the office changed, pull now."
   *Latency, not scale:* 1,000 clients polling `git ls-remote` is trivial host
   load, so a server is never *needed* for freshness. And the notification channel
   needs **zero trust**: because `update` verifies signatures on pull, a spoofed
   "update available" can only trigger a *harmless, verified* fetch. So push is
   nearly free — a cheap lossy broker (CI push → ntfy/MQTT/SSE, ~$5/mo), or just
   an hourly `git ls-remote` cron, covers the real need with no server at all.
2. **Fleet management / attestation** — central "who has what, is it signed,
   is policy applied" beyond a read roll-up. (Note: any server must first solve
   **install identity/auth** — a new problem; cheapest answer is to reuse the git
   host's identity, e.g. a check-in as a signed commit.)
3. **An org dashboard** beyond today's loopback-only, per-machine one.
4. **Provisioning** — create empty my-office repos and memberships via the host
   API, set branch protection / CODEOWNERS. Its token is scoped to repo/membership
   creation only. It **does not distribute pins or rotate signing keys** — see the
   constraint below.

Recommendation — **hub-and-spoke on git first**: the "server" is your git host +
CI. It's the source of truth; CI is the automation. This is enough for Phases 1–2
and keeps every supply-chain guarantee intact.

**If** a server is later justified, the load-bearing constraints (matching the
standing product direction — *no new write paths*):

> 1. The server is a **read-model / coordination service over the same git source
>    of truth**. It may *observe* (index fleet state), *notify* ("update
>    available"), and *provision* (create empty repos/memberships) — it must
>    **never become a second authoritative write path**, and **never distribute
>    trust roots** (pins, policy, signing keys). Pins and `org.toml` travel **only
>    via signed git**; a pin-distributing server could silently redirect the whole
>    fleet to an attacker's upstream — strictly worse than mutating an artifact,
>    which the signature check would catch.
> 2. **Rebuildability litmus test:** server state must be reconstructible from git
>    + client check-ins. Losing the server loses only *freshness*, never a trust
>    root or an artifact.
> 3. **Privacy:** it observes canonical + project state by default; personal-tier
>    observation is opt-in only.

A server that owned artifact state or distributed pins would recreate git, break
the deterministic compile/merge model, and reopen the compromised-source questions
`signed_by` and the quarantine just closed. Coordination-over-git keeps the trust
boundary where it already is.

## Prerequisite (before Phase 1) — a present-tense code fix

`update.py`'s `_git` inherits the shared `GIT_ENV` but does **not** set the
`protocol.ext.allow=never` / `protocol.fd.allow=never` bans that `myoffice.py`
sets inline. Not exploitable today (update fetches by remote *name*), but
`--profile enterprise` writing an org-supplied `office_url` into a remote makes
`ext::sh -c …` an RCE on first fetch. **Hoist the transport ban into the shared
`GIT_ENV` (gitutil) via `GIT_CONFIG_*` so every git caller inherits it and can't
drift**, and prefer a scheme *allowlist* (`ssh`/`https`/scp-like) for org URLs.
Ship this ahead of the profile work; it's a standalone security hardening.

## Staged plan

- **Phase 1 — profiles (small), gated on the trust bootstrap:** `org.toml`,
  `setup --profile`, `--role`. **Ships only with** out-of-band `--signed-by` +
  verify-before-parse, fail-closed on missing/bad `org.toml` (3.10-safe reader),
  multi-key pins + a defined rotation flow, and the `{user}`-template injection
  guards. Config over existing machinery otherwise; stdlib/git-native.
- **Phase 2 — team collaboration on git (medium):** `cohort org-report` fleet
  roll-up reading a git-tracked `registry.toml`, cloning over the git protocol;
  org-wide signals for the Steward loop; company-repo template with CODEOWNERS.
  No fourth tier.
- **Phase 3 — server decision (large, deliberate):** RFC-gate it against the
  framework above. Default answer: hub-and-spoke on git + CI (poll, or a zero-trust
  lossy notifier). If built: read-model/coordination only, never a trust-root
  distributor, rebuildable from git.

## Open questions — resolved in review

1. **`org.toml` location** → **office repo root** — travels with `update`, signed
   alongside everything, inside the verified boundary. *Correct given*
   verify-before-parse (Theme: trust bootstrap).
2. **my-office provisioning** → **client-side from template for v1** (zero-infra);
   org-side provisioning implies Phase 3. The template passes the `{user}`/URL
   validation, and provisioned org-writable remotes carry the documented quarantine
   gap (below). Provisioning stays out of Phase 1.
3. **Overridability** → **pure defaults in v1**; a client-side "locked" mode is
   theater. Add loud warnings + `org-report` visibility now; bank real enforcement
   for the Phase-3 server (host branch protection, revocation).
4. **Roster roles** → **flat subsets for v1**. Composable role layers are the same
   speculative-tier trap the RFC resists for a "team" tier — RFC-gate if demand
   appears (interacts with the two-layer merge model).

## Risks / non-goals

- **Trust bootstrap is the crux.** Mitigation: out-of-band first pin + verify
  before parse + multi-key/offline-root + chained-loud rotation + fail-closed
  setup. This is the price of admission for `--profile enterprise` — without it the
  profile *creates* the attacker-pins-their-key hole.
- **Scope creep toward a server.** Mitigation: the framework; git-first default;
  the never-distribute-trust-roots + rebuildable-from-git constraints.
- **Policy vs autonomy.** Enterprises want control; the individual story is why
  people adopt it. Mitigation: defaults, not mandates, in v1; hardening opt-in and
  loud (as `require_signed` already is); detection over prevention.
- **Governance doesn't scale.** Single-maintainer merge → CODEOWNERS in the
  company-repo template (Phase 2). The *first* thing that breaks at scale is human
  review throughput (quarantine + draft-PR triage), not sync.
- **Accepted risk (v1, documented):** the quarantine gates only hooks + memories;
  a malicious **agent/command/skill** pushed to an *org-writable* provisioned
  my-office remote places without review. Deliberate maintainer scoping — but the
  calculus differs when the remote is org-writable rather than self-owned; revisit
  the gated-kinds set for provisioned remotes.
- **Data residency (info).** `my-office sync` pushes the personal layer wholesale
  to the company host — personal memories/PII then live on org infrastructure.

## Prior art in this codebase

`signed_by`/`require_signed` (update trust), my-office quarantine (multi-writer
safety), `submit-proposals` (contribution gate), `my-office sync` (personal
layer over git), `weekly-report`/`aggregate_signals` (the read-only report
shape). This RFC composes them; it does not invent a sync system.

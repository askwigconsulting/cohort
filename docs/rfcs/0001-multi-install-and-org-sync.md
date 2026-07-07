# RFC 0001 — Multi-install: individual vs enterprise, org config, and org sync

Status: **Draft** · Target: post-0.4.0 · Owner: maintainer

## Summary

Package Cohort's already-distributed architecture into two supported install
**profiles** — *individual* and *enterprise* — and a company-owned `org.toml`
that carries policy and defaults. Add lightweight org-level **visibility** and
**signals aggregation** on the existing git substrate. Defer a centralized
**server** behind a decision framework: if one is ever built, it is a read-model
/ coordination service over the same git source of truth, never a second write
path.

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
signed_by      = ["SHA256:<acme-release-key-fp>"]

[roster]                 # role -> agent subset (extends `setup --agents`)
default = ["chief-of-staff", "counsel", "security-engineer", "cloud-architect"]
[roster.roles]
engineering = ["chief-of-staff", "security-engineer", "cloud-architect", "code-reviewer"]
finance     = ["chief-of-staff", "finance-analyst", "compliance"]

[my_office]              # optional provisioned personal-layer remote template
remote_template = "git@github.com:acme/cohort-my-{user}.git"
```

Precedence: **local user config wins over org defaults** — org sets *defaults and
recommended policy*, it does not lock the machine. (A future hard-policy mode is
out of scope; call it out but don't build it.) `[update]` values are applied to
the local `cohort.toml` at install and remain user-editable, consistent with the
existing "only unset require_signed as a deliberate downgrade" posture.

### `cohort setup --profile individual|enterprise`

- `--profile individual` — current defaults (clone-and-go, full roster, no
  provisioned remote).
- `--profile enterprise --company-url <repo>` — clone the office, read its
  `org.toml`, apply `[update]` pins, install the role roster, optionally
  provision the my-office remote from the template. One URL → configured install.
- `--role <name>` selects a `[roster.roles]` subset.

Enterprise onboarding collapses to: **`cohort setup --profile enterprise
--company-url … --role engineering`**. Pure orchestration of existing knobs.

### Org visibility (fleet roll-up)

Every install already computes "N commits behind upstream" and each project
tracks staleness/signals in git. Add an **org roll-up** — a report (and/or a CI
job in the company repo) that reads across the org's project repos and answers
the first questions an enterprise buyer asks:

- Who is on which office version / how far behind?
- Whose project context is stale?
- Where are the low-rated agents / open improvement proposals, org-wide?

Implementation: a `cohort org-report` reading a list of project repo URLs (or a
CI job with read access), emitting a git-tracked markdown/JSON summary. No
server, no new write path — a read-only aggregator over existing signals, the
same shape as `weekly-report` but across repos.

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
2. **Fleet management / attestation** — central "who has what, is it signed,
   is policy applied" beyond a read roll-up.
3. **An org dashboard** beyond today's loopback-only, per-machine one.
4. **Provisioning** — mint my-office remotes, rotate signing keys, manage roles.

Recommendation — **hub-and-spoke on git first**: the "server" is your git host +
CI. It's the source of truth; CI is the automation. This is enough for Phases 1–2
and keeps every supply-chain guarantee intact.

**If** a server is later justified, the load-bearing constraint (matches the
standing product direction — *no new write paths*):

> The server is a **read-model / coordination service over the same git source of
> truth**. It may *observe* (index fleet state), *notify* (push "update
> available"), and *provision* (create repos, distribute pins) — it must **never
> become a second authoritative write path** for canonical artifacts. Canonical
> stays git; the server never mutates it out-of-band.

A server that owned artifact state would recreate git, break the deterministic
compile/merge model, and reopen the compromised-source questions `signed_by` and
the quarantine just closed. Coordination-over-git keeps the trust boundary where
it already is.

## Staged plan

- **Phase 1 — profiles (small):** `org.toml`, `setup --profile`, `--role`. Config
  over existing machinery; stdlib/git-native. Ships enterprise onboarding.
- **Phase 2 — team collaboration on git (medium):** `cohort org-report` fleet
  roll-up; org-wide signals for the Steward loop; company-repo template with
  CODEOWNERS. No fourth tier.
- **Phase 3 — server decision (large, deliberate):** RFC-gate it against the
  framework above. Default answer: hub-and-spoke on git + CI. If built:
  read-model/coordination only.

## Open questions

1. `org.toml` location — office repo root (proposed) vs a dedicated `org/`
   subtree? Root is simplest and travels with `update`.
2. my-office remote provisioning — client-side from a template (proposed) vs a
   provisioning step run by the org? Template is zero-infra; provisioning implies
   Phase 3.
3. How far should org defaults be overridable — pure defaults (v1) vs an opt-in
   "managed/locked" policy mode later?
4. Roster roles — flat subsets (proposed) vs composable role layers?

## Risks / non-goals

- **Scope creep toward a server.** Mitigation: the framework; git-first default.
- **Policy vs autonomy.** Enterprises want control; the individual story is why
  people adopt it. Mitigation: defaults, not mandates, in v1; make hardening
  opt-in and loud (as `require_signed` already is).
- **Governance doesn't scale.** Single-maintainer merge → CODEOWNERS in the
  company-repo template (Phase 2).

## Prior art in this codebase

`signed_by`/`require_signed` (update trust), my-office quarantine (multi-writer
safety), `submit-proposals` (contribution gate), `my-office sync` (personal
layer over git), `weekly-report`/`aggregate_signals` (the read-only report
shape). This RFC composes them; it does not invent a sync system.

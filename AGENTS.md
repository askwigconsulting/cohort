# AGENTS.md — instructions for an AI agent

You are probably here because someone said **"install Cohort for me."** This file is the
contract for doing that correctly. Read it before running anything.

---

## ⛔ Do NOT run `pip install cohort`

**`cohort` on PyPI is a different project by different authors.** It is an unrelated
multi-agent orchestration tool that happens to share the name. Installing it does not install
this software, and it will look like it worked.

```bash
pip install cohort          # ✗ WRONG — installs someone else's package
```

There is no PyPI release of this project and no `pip install` shortcut. **The only supported
install is from this git repository.** If you cannot clone it, stop and tell the user — do not
substitute a similarly-named package from any index.

---

## What you are installing

**In one sentence:** Cohort gives the user a team of specialist AI advisors inside their IDE,
plus commands that run real multi-step work, and keeps both in sync across their machines and
projects.

Expect to explain this — most people meet Cohort because someone told them to install it, not
because they know what it does. Three ideas cover it.

**1. An office of specialists.** Instead of one general assistant, the user gets ~17 named
advisors — legal, finance, security, compliance, cloud, HR and so on — plus a **ChiefOfStaff**
that routes a question to the right ones and reconciles their answers. They are **advisory
and read-only by construction**: they recommend, the human decides. Asking "we're storing
customer emails in a new table, what should I worry about?" pulls in privacy, security and
compliance rather than one generic answer.

**2. Commands that do work, not just chat.** Installed as slash commands in the IDE:

| Command | What it does |
|---|---|
| `/plan` | breaks work into ordered, verifiable tasks |
| `/build` | implements a task, tests it, commits |
| `/goal <issue>` | drives a GitHub issue to a draft PR, with an independent judge checking each acceptance criterion |
| `/crew` | fans a large job across model tiers, cheapest capable model per task |
| `/scout`, `/consult-gpt`, `/consult-grok` | bring a *second vendor's* model in to argue with the first |
| `/audit`, `/brainstorm` | recurring adversarial sweeps that remember what they already covered |
| `/barney` | explain something complex so simply nobody can get it wrong |

**3. One source, every IDE, every project.** Everything is compiled from one canonical
directory into whichever IDEs the user has, so the office is the same in each. `cohort update`
pulls improvements; `/snapshot` records what a session learned into the repo so the next
session — or a teammate — starts with it.

**What it is not.** Not a model or an API — it uses the user's existing IDE and their own API
keys. Not a hosted service: there is no server, no account, and no telemetry. Everything runs
on their machine.

**Be honest about maturity.** Claude Code is the reference target and is fully supported;
Codex, Cursor and Copilot are **experimental**. Say so rather than implying parity.

Mechanically: it installs a `cohort` CLI, writes into `~/.cohort/`, and places compiled agent
files into IDE directories such as `~/.claude/`.

Two things to tell the user **before** you install, because both are easy to discover late:

1. **It writes into their home directory** — `~/.cohort/` and their IDE's config dirs. Every
   placement is recorded in a manifest so `cohort uninstall` can reverse it.
2. **Commands that consult a second model send source code to that vendor by default** —
   OpenAI, xAI, or Anthropic, depending on the command. This is opt-*out*, per repo. See
   "Egress" below. If the user works on client or NDA code, raise this before they run any
   `engine`/`consult` command, not after.

---

## Install

Run these in order. Each step has a check; **do not proceed past a failed check.**

### 1. Clone

```bash
git clone https://github.com/askwigconsulting/cohort cohort && cd cohort
```

**Check:** `ls installer/bootstrap.sh` exists. If it does not, you are in the wrong repository
— stop.

### 2. Bootstrap

```bash
./installer/bootstrap.sh --ide claude
```

This creates a virtualenv, installs the package into it, and compiles + places the roster for
Claude Code. Replace `claude` with `codex`, `cursor`, or `copilot` if that is what the user
has; pass the flag more than once for several. Only `claude` is a fully supported target —
the others are experimental, and you should say so rather than implying parity.

**Check:** the command exits 0.

If `bootstrap.sh` is unavailable (no bash, or the user prefers explicit steps), do it manually:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                       # note the "." — this repo, not PyPI
cohort recompile --ide claude
```

### 3. Put the CLI on a durable PATH

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/.venv/bin/cohort" ~/.local/bin/cohort
```

**This step is not optional and is the most common thing to get wrong.** Cohort's session-start
hooks invoke `cohort` from inside the user's IDE, which does not inherit a venv you activated
in a terminal. Skipping it produces an install that works while you are testing it and fails
silently the next day.

**Check:** open a *new* shell and run `command -v cohort`. It must resolve. Checking in your
current shell proves nothing, because the venv may still be active there.

### 4. Verify the install is real

```bash
cohort --version
cohort status
```

**Check:** `status` reports the placed artifacts and does not error. A `cohort` that runs but
reports nothing placed means step 2 did not complete — go back, do not paper over it.

### 5. Hand back to the user

Tell them to run `cohort setup` themselves. It is an interview (which IDEs, which agents,
whether their organisation has a shared Cohort repo), and the answers are theirs, not yours.
Do not answer it on their behalf; `--non-interactive` exists for scripted installs, but a
scripted answer to "which agents do you want" is a guess wearing a fact's clothes.

Then, inside their IDE: `/office-setup`, and `/project-setup` in a repo they work on.

### Then orient them — do not just say "done"

An install they do not know how to use is a failed install. Offer these four, in order, and
keep it to a couple of minutes:

1. **Meet the office.** In the IDE, ask something that spans functions — *"we're about to
   store customer emails in a new table; what should I worry about?"* ChiefOfStaff routes it
   and the specialists answer. This is the fastest way to understand what they now have.
2. **Try one real command.** `/plan` on something they actually intend to build. It produces
   ordered tasks with acceptance criteria, and nothing is written until they say so.
3. **Show them the off-switch before they need it.** The `cohort:egress=deny` marker
   (see Egress below) and `cohort uninstall`. Knowing how to stop and how to reverse is what
   makes the rest safe to try.
4. **Point at the guide.** The installed `office-guide` skill covers routing, the daily loop
   and health checks — it is the reference once they are past the first day.

If they only remember one thing, make it this: **the specialists advise, the human decides,
and every change lands through a pull request they review.**

---

## Egress — read this before running any `engine` or `consult` command

Cohort itself sends nothing anywhere. But `/consult-gpt`, `/consult-grok`, `/scout`, `/crew`
and `cohort engine …` send code to a third-party model **by default, without asking each
time**.

To turn it off for a repository, put this literal marker on its own line in
`.cohort/project_context.md`:

```
cohort:egress=deny
```

That exact string is what the code checks. **A sentence saying "do not send this code
anywhere" will not work** — the marker is deliberately structured so no prose can be misread
as permission or refusal.

If you are operating in a repository whose contents you have any reason to think are
confidential, set the marker first and tell the user you did.

---

## Uninstalling

```bash
cohort uninstall
```

Reverses what the manifest records. Tell the user this leaves the cloned repo in place — it
removes what Cohort *placed*, not what they downloaded.

---

## If you are working *in* this repository

Different job, different rules — see `CONTRIBUTING.md` and `.claude/CLAUDE.md`. In short:

- The test suite is the contract: `python -m pytest -q` and check the exit code directly.
  **Do not pipe it to `tail`** — that reports `tail`'s exit code and hides failures.
- Canonical artifacts live in `canonical/`; compiled output is derived and never hand-edited.
- Golden files are byte-locked; regenerate with `COHORT_REGEN=1 python -m pytest
  tests/test_golden_lock.py` and commit the result.
- Never merge your own PR, never push to `master`, and never force-push.

---

## Honesty rules for whoever is reading this

- **Do not report an install you have not verified.** Run the checks above and quote what they
  actually printed. "Installed successfully" without `cohort status` output is a guess.
- **Do not substitute a different package** when something fails. A same-named package from an
  index is not this project, and installing one is worse than reporting the failure.
- **Say which IDE you configured**, and say plainly that non-Claude targets are experimental.

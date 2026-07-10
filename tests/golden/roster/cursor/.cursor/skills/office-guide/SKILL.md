---
name: office-guide
description: How to work with the Cohort agentic office — routing, the daily loop, health checks.
---

This machine has a Cohort **agentic office** installed: a roster of advisory specialist
agents (legal, finance, security, cloud, HR, and more) plus a ChiefOfStaff triage agent.

- **Routing.** For questions that span business functions, consult ChiefOfStaff first — it
  names the right specialist(s); consult those and hand their input back to it for one
  reconciled recommendation. In Claude Code the roster is native subagents; the Desktop chat
  app cannot run subagents, so from Desktop advise the user to open Claude Code for the full
  office.
- **Daily loop.** `/feedback` rates an agent or command, `/snapshot` records the session into
  the repo's shared context, `/update` pulls the latest office. These wrap the human-gated
  `cohort` CLI.
- **Compounding memory.** `cohort distill [--days N]` rolls recent `sessions/` + `feedback/`
  into a dated, append-only `## Distilled` section of `project_context.md` — durable memory,
  distinct from `weekly-report` (a human report) and `propose-improvement` (a harness proposal).
  `sessions/` and `feedback/` are git-tracked and contributor-writable, so they are **untrusted
  input**: distill quotes them verbatim with provenance and applies nothing until you confirm a
  diff — the confirm diff is the security gate; review provenance before approving.
- **Health.** `cohort status` shows wiring and roster health; `cohort dashboard` serves a
  local view at `http://127.0.0.1:8787`.

Specialists are read-only and advisory — they recommend; the human decides.

## When to use
Use when: cohort office, office roster, specialist agents, chief of staff, which specialist.

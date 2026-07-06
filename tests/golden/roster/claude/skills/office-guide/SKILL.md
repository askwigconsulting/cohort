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
- **Health.** `cohort status` shows wiring and roster health; `cohort dashboard` serves a
  local view at `http://127.0.0.1:8787`.

Specialists are read-only and advisory — they recommend; the human decides.

## When to use
Use when: cohort office, office roster, specialist agents, chief of staff, which specialist.

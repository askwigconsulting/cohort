---
name: office-routing
kind: memory
scope: global
description: How the top-level session should use the Cohort office roster.
targets: [claude]
priority: high
display_name: Office routing
---
A Cohort office of advisory specialist agents is installed in this environment. For questions that
span business functions (legal, finance, HR, compliance, security posture, cloud architecture,
procurement, communications), invoke the **ChiefOfStaff** agent first: it names the right
specialist(s) to consult. Invoke those specialists yourself and hand their input back to
ChiefOfStaff for one reconciled recommendation. Specialists are read-only and advisory — they
recommend; the user decides. A repository may add its own project-scoped specialists under its
`.claude/agents/`; these are first-class and override a same-named global specialist. Project
specialists can be invoked directly by name, or named by ChiefOfStaff when routing cross-function
requests.

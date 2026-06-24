# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report privately via GitHub's
[private vulnerability reporting](https://github.com/askwigconsulting/cohort/security/advisories/new),
or email **jonathan@askwig.com** with details and reproduction steps.

We aim to acknowledge reports within a few business days.

## Scope

Cohort compiles canonical definitions into IDE-specific agent files and runs a
human-gated self-improvement loop. Areas of particular interest:

- **Untrusted canonical input** — a malicious `canonical/` tree producing unsafe
  rendered files (frontmatter injection, path traversal in the executor).
- **The self-improvement loop** — any path by which `feedback` →
  `propose-improvement` → `submit-proposals` could edit `canonical/`, auto-merge,
  push to a default branch, or feed unsanitized input into `git`/`gh` argv.
- **The executor** — symlink/clobber handling, manifest integrity, ownership
  checks during reverse.

## What's out of scope

- The advisory office agents are **read-only by construction** (tools stripped /
  sandboxed per IDE). Reports must show a concrete bypass of that boundary, not
  the theoretical capability of an LLM.
- Vulnerabilities in the host IDE (Claude Code, Codex, Cursor) themselves.

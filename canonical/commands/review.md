---
name: review
kind: command
scope: global
description: Conduct a five-axis code review — correctness, readability, architecture, security, performance
targets:
- claude
invocation: review
dry_run: true
---
Invoke the agent-skills:code-review-and-quality skill.

Review the current changes (staged or recent commits) across all five axes:

1. **Correctness** — Does it match the spec? Edge cases handled? Tests adequate?
2. **Readability** — Clear names? Straightforward logic? Well-organized?
3. **Architecture** — Follows existing patterns? Clean boundaries? Right abstraction level?
4. **Security** — Input validated? Secrets safe? Auth checked? (Use security-and-hardening skill)
5. **Performance** — No N+1 queries? No unbounded ops? (Use performance-optimization skill)

Categorize findings as Critical, Important, or Suggestion.
Output a structured review with specific file:line references and fix recommendations.

## Verdict block

End your output with a fenced ` ```verdict ` block — one `overall` line plus one
line per axis, each `pass|fail` with a one-line evidence note:

```verdict
overall: PASS|FAIL
correctness: pass|fail — one-line evidence
readability: pass|fail — one-line evidence
architecture: pass|fail — one-line evidence
security: pass|fail — one-line evidence
performance: pass|fail — one-line evidence
```

Rules:
- `overall` is `FAIL` if any axis is `fail`; `PASS` only if all five axes pass.
- Emit exactly one line per axis, in the order above, even when an axis has no findings (`pass — no issues found`).
- This fence must be the last fence in your output — it is the only verdict-shaped
  text a caller should trust. See the office-guide skill's "Verdict blocks" section
  for the full trust rule.

---
description: Implement the next task incrementally — build, test, verify, commit
---

Invoke the agent-skills:incremental-implementation skill alongside agent-skills:test-driven-development.

Pick the next pending task from the plan. For each task:

1. Read the task's acceptance criteria
2. Load relevant context (existing code, patterns, types)
3. Write a failing test for the expected behavior (RED)
4. Implement the minimum code to pass the test (GREEN)
5. Run the full test suite to check for regressions
6. Run the build to verify compilation
7. Commit with a descriptive message
8. Mark the task complete and move to the next one

If any step fails, follow the agent-skills:debugging-and-error-recovery skill.

## Worktrees for concurrent writes (in orchestrated work)

When `/build` is run as a worker task within `/orchestrate` and multiple writers
run in parallel:

- **Use the worktree provided by the coordinator.** The coordinator creates a
  per-task worktree and detached HEAD so your commits don't collide with other
  workers' `.git/index.lock`.
- **Never commit to the coordinator's shared checkout.** All work happens in your
  worktree; commits land on the detached HEAD.
- **Build and test against your worktree's scope.** The coordinator runs the full
  suite after integrating your task serially; your job is to verify your task's
  acceptance criteria within the worktree.

If you are the only writer (sequential tasks, or a single `/build` invocation),
worktrees are optional; commit to the shared branch as usual.

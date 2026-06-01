# Parallel Merge Planner

The `merge-plan` command writes a non-destructive merge planner report for parallel worktree or
task branch development. It inspects branches and worktrees, but it does not run `git merge`,
`git rebase`, `git reset`, `git checkout`, `git commit`, or `git push`.

Example:

```bash
engo merge-plan --project-root . --base main --branch task-a --branch task-b --write
```

The report summarizes:

- dirty paths in selected worktrees;
- changed files for each task branch relative to the selected base;
- likely conflict paths where the base or multiple candidates changed the same file;
- a recommended merge order that puts cleaner, smaller, lower-conflict candidates first;
- post-merge acceptance commands inferred from matching roadmap task ids or provided with
  `--post-merge-acceptance`.

When no `--branch` or `--worktree` is provided, the planner discovers sibling git worktrees and
plans them against the current branch. Operators can add `--task <task-id>` to include roadmap
acceptance commands even when a branch name does not include the task id.

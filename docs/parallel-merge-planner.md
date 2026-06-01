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

## Native Parallel Development

`engo parallel-drive` runs a bounded local parallel development drive for eligible pending roadmap
tasks. It plans safe dispatch waves from task metadata, creates isolated task branches and git
worktrees, launches worker subprocesses, monitors their local state, validates successful branches
after merge, and then removes merged task worktrees and branches.

Example:

```bash
engo parallel-drive --project-root . --max-workers 2 --max-tasks 4 --time-budget-seconds 1800
```

Use `--plan-only --json` to inspect the safe waves before launching workers. The planner does not
parallelize tasks with overlapping `file_scope` patterns, waits for explicit dependencies such as
`depends_on`, and blocks on unrelated dirty git state unless `--allow-dirty-worktree` is supplied.

Successful task workers run the normal task lifecycle in their worktree, including implementation,
acceptance, repair, e2e, manifest writing, and task checkpointing. The parent drive copies task
reports and manifests back into the base project, merges the task branch with post-merge acceptance
validation, updates durable task state, rebuilds the manifest index, and deletes the merged branch
and temporary worktree.

Failed, blocked, timed-out, or merge-failed workers are preserved. Their branch, worktree, copied
manifest, and copied report are recorded in the parallel-drive JSON and Markdown report so an
operator can inspect and repair the isolated failure without losing local evidence.

Runtime evidence is local-first:

- `.engineering/state/parallel-drive.json` records the current run, worker heartbeats, task ids,
  branch/worktree paths, merge status, cleanup status, and stop reason.
- `.engineering/reports/tasks/parallel-drives/*-parallel-drive.{md,json}` records the durable
  machine-readable and human-readable audit trail.
- `engo status --json` exposes the latest `runtime_dashboard.parallel_drive` summary for dashboards
  and operator tooling.

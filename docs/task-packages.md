# Engineering Orchestrator Task Packages

Date: 2026-06-01

This document mirrors the target task package state that is also tracked in
`.engineering/spec_tasks.yaml` and `.engineering/roadmap.yaml`.

## Task Package Contract

A task package is the smallest unit that Engineering Orchestrator can assign to
one or more worker agents. It must describe what the worker should implement and,
when the task is risky or part of a parallel batch, how the supervisor should
evaluate progress and decide whether to continue, retry, split, pause, or stop.

The source of truth for executable packages is `.engineering/roadmap.yaml`.
Spec progress is tracked in `.engineering/spec_tasks.yaml`. This document defines
the human-facing package contract that authors should follow when creating or
reviewing those task entries.

### Minimal Package

Use this shape for simple sequential work where the default supervisor policy is
enough.

```yaml
id: example-task
title: Example task
status: pending
objective: Implement the smallest complete change that satisfies the spec.
file_scope:
  - src/**
  - tests/**
acceptance:
  - name: targeted tests
    command: pytest -q tests/test_example.py
```

Required fields:

- `id`: stable, unique task id used by the roadmap, reports, manifests, and sync logs.
- `title`: short human-readable task title.
- `status`: `pending`, `planned`, `completed`, `blocked`, or another status supported by the current roadmap parser.
- `objective`: concrete worker-facing implementation goal.
- `file_scope`: paths the worker is expected to edit or inspect.
- `acceptance`: commands or checks that prove the task is complete.

### Standard Worker and Supervisor Package

For non-trivial tasks, describe the worker implementation separately from the
supervisor policy. The worker does the implementation. The supervisor evaluates
whether the implementation should continue through the orchestration gates.

```yaml
id: example-standard-task
title: Example standard task
status: pending

worker_spec:
  objective: Add roadmap status synchronization after successful task execution.
  required_changes:
    - Update run and drive completion paths.
    - Record synchronization evidence in task reports and manifests.
    - Add focused tests for task status and milestone rollup.
  file_scope:
    - src/engineering_orchestrator/**
    - tests/**
  acceptance:
    - name: roadmap sync tests
      command: pytest -q tests/test_roadmap_sync.py

supervisor_policy:
  enabled: true
  review_focus:
    - The implementation matches the task intent, not only the test text.
    - No unrelated source or roadmap entries were changed.
    - Failed or blocked results do not mark roadmap tasks as completed.
  quality_gates:
    - acceptance_tests_passed
    - no_unrelated_git_dirty
    - roadmap_status_synced
  retry_policy:
    max_retries: 1
    retry_when:
      - acceptance_failed
      - roadmap_sync_failed
  stop_when:
    - destructive_git_operation_detected
    - out_of_scope_file_changes
    - secret_or_deployment_config_changed_without_gate
  continuation_policy:
    on_success: continue_next_task
    on_failure: supervisor_decides_retry_or_pause
```

Authoring rules:

- `worker_spec` is implementation-facing and should be concrete enough for a worker agent to execute without prior conversation context.
- `supervisor_policy` is control-facing and should define how to judge the worker result, not how to implement the feature.
- The supervisor must not be given a second implementation task inside `supervisor_policy`.
- If `worker_spec` is omitted, the top-level `objective`, `file_scope`, and `acceptance` are treated as the worker contract.
- If `supervisor_policy` is omitted, Engineering Orchestrator uses the default supervisor policy for the current drive mode.

### Parallel Package Requirements

Parallel task batches need stricter boundaries because multiple workers may edit
the repository at the same time.

```yaml
id: example-parallel-task
title: Example parallel task
status: pending

worker_spec:
  objective: Implement one isolated feature slice.
  file_scope:
    - src/feature_a/**
    - tests/test_feature_a.py
  acceptance:
    - name: feature tests
      command: pytest -q tests/test_feature_a.py

parallel_policy:
  max_workers: 3
  branch_prefix: eo/feature-a
  merge_policy: supervisor_review_before_merge
  conflict_policy: pause_conflicting_branch

supervisor_policy:
  enabled: true
  gates:
    - quality_gate_completion
    - failed_task
    - blocked_task
  review_focus:
    - The task only touched its declared file scope.
    - The branch can merge without conflicting with sibling work.
    - Acceptance evidence exists before roadmap status is updated.
  allowed_decisions:
    - continue
    - retry
    - pause
    - split_task
```

Parallel packages should always include:

- `file_scope`: precise enough to detect overlap between workers.
- `parallel_policy.max_workers`: expected concurrency limit for this package or batch.
- `parallel_policy.merge_policy`: whether merges are automatic, supervisor-gated, or manual.
- `parallel_policy.conflict_policy`: what to do when branches or file scopes collide.
- `supervisor_policy.gates`: at least completion, failure, and blocked gates.

### Sensitive Package Requirements

Tasks touching deployment, secrets, billing, production data, authentication, or
infrastructure must declare a sensitive gate.

```yaml
deployment_sensitive: true
supervisor_policy:
  enabled: true
  gates:
    - deployment_sensitive_task
    - quality_gate_completion
  stop_when:
    - secret_or_deployment_config_changed_without_gate
    - production_write_without_explicit_approval
```

Sensitive tasks should prefer small sequential execution over broad parallel
execution unless the merge and rollback boundaries are explicit.

### Time Budget Requirements

Task authors must set realistic execution time budgets. The orchestrator default
timeout is only a safety fallback for small tasks; it is not an appropriate
budget for cross-cutting implementation work.

Rules:

- Do not rely on the default `900` second timeout for agent implementation commands unless the task is clearly small.
- Every `codex`, `openhands`, or other agent executor command should set an explicit `timeout_seconds`.
- If the author cannot confidently estimate the task size, choose a larger budget instead of risking a premature timeout.
- Large tasks should still be split when they have independent file scopes, but splitting is not a substitute for giving each slice enough runtime.
- The drive-level `time_budget_seconds` should be larger than the largest worker timeout plus acceptance, merge, retry, and supervisor overhead.

Recommended minimums:

| Task type | Suggested implementation timeout |
| --- | ---: |
| Docs-only or ledger-only update | `1800` seconds |
| Small focused code change | `3600` seconds |
| Cross-cutting backend / frontend / tests task | `7200` seconds |
| Production runtime, storage, deployment, or broad refactor task | `10800` seconds |
| Unknown or hard-to-estimate agent task | at least `7200` seconds |

If a task times out after producing useful changes, treat the timeout as a task
package sizing problem first: inspect the worktree diff, salvage or commit useful
work, then either raise the timeout or split the task before retrying.

### Status Synchronization

After a successful non-dry-run task, Engineering Orchestrator records completion
evidence into the task report and manifest, updates `.engineering/spec_tasks.yaml`
when a matching spec task exists, updates managed documentation status blocks
when documentation roles are configured, and updates `.engineering/roadmap.yaml`
for the completed roadmap task. When every task in the same milestone or
continuation stage is completed, the parent status is rolled up to `completed`.

Failed, blocked, skipped, or dry-run tasks must not mark roadmap tasks as
completed.

## Current Package State

| Spec Task | Roadmap Task | Status | Package Scope | Local Evidence |
| --- | --- | --- | --- | --- |
| `EH-SPEC-SYNC-001` | `target project spec synchronization` | `completed` | spec-sync audit and record commands | `src/engineering_orchestrator/spec_sync.py`, `tests/test_spec_sync.py` |
| `EH-DOC-SYNC-001` | `documentation role contract` | `completed` | target documentation role contract | `docs/engineering-orchestrator-system-spec.md`, decision record |
| `EH-DOC-SYNC-002` | `engineering-orchestrator-target-docs-sync-implementation` | `completed` | docs-sync audit, propose, record, and post-task evidence | targeted docs-sync tests, roadmap validation, command evidence |
| `EH-PARALLEL-DRIVE-001` | `engineering-orchestrator-native-parallel-drive` | `planned` | native parallel drive orchestration | future task |
| `EO-SUP-001` | `supervisor-role-spec` | `completed` | supervisor role, boundaries, trigger conditions, and task package plan | `docs/engineering-orchestrator-system-spec.md`, `docs/spec-driven-development-plan.md` |
| `EO-SUP-002` | `supervisor-context-pack` | `completed` | bounded local supervisor context pack | `src/engineering_orchestrator/core.py`, `src/engineering_orchestrator/cli.py`, `tests/test_engineering_orchestrator.py`, `docs/executor-contract.md` |
| `EO-SUP-003` | `supervisor-decision-schema` | `completed` | `supervisor_decision.v1` schema and validation | `src/engineering_orchestrator/supervisor_decision.py`, `tests/test_engineering_orchestrator.py` |
| `EO-SUP-004` | `supervisor-gated-drive-integration` | `completed` | drive / parallel-drive supervisor gate integration | `src/engineering_orchestrator/core.py`, `src/engineering_orchestrator/cli.py`, `src/engineering_orchestrator/parallel_drive.py`, `docs/supervisor-gated-drive.md` |
| `EO-SUP-005` | `supervisor-safe-roadmap-mutation` | `completed` | safe queue and roadmap mutation proposals | `src/engineering_orchestrator/core.py`, `tests/test_engineering_orchestrator.py`, `docs/supervisor-gated-drive.md` |
| `EO-SUP-006` | `supervisor-tests-and-runbook` | `completed` | tests, reports, and operator runbook | `tests/test_engineering_orchestrator.py`, `docs/supervisor-operator-runbook.md` |

<!-- engineering-orchestrator:docs-sync:start -->
## Engineering Orchestrator Documentation Sync

| Task | Requested Task | Role | Status | Updated | Evidence |
| --- | --- | --- | --- | --- | --- |
| EH-DOC-SYNC-002 | engineering-orchestrator-target-docs-sync-implementation | task_packages | completed | 2026-06-01T13:18:24Z | targeted docs sync tests passed; roadmap validation passed; docs-sync command evidence found |
| EO-SUP-002 | supervisor-context-pack | task_packages | completed | 2026-06-02T01:10:32Z | task report: .engineering/reports/tasks/20260602T005739Z-supervisor-context-pack.md; task manifest: .engineering/reports/tasks/20260602T005739Z-supervisor-context-pack.json; tas... |
| EO-SUP-003 | supervisor-decision-schema | task_packages | completed | 2026-06-02T01:23:13Z | task report: .engineering/reports/tasks/20260602T011352Z-supervisor-decision-schema.md; task manifest: .engineering/reports/tasks/20260602T011352Z-supervisor-decision-schema.jso... |
| EO-SUP-004 | supervisor-gated-drive-integration | task_packages | completed | 2026-06-02T01:42:23Z | task report: .engineering/reports/tasks/20260602T012637Z-supervisor-gated-drive-integration.md; task manifest: .engineering/reports/tasks/20260602T012637Z-supervisor-gated-drive... |
| EO-SUP-005 | supervisor-safe-roadmap-mutation | task_packages | completed | 2026-06-02T01:57:05Z | task report: .engineering/reports/tasks/20260602T014223Z-supervisor-safe-roadmap-mutation.md; task manifest: .engineering/reports/tasks/20260602T014223Z-supervisor-safe-roadmap-... |
| EO-SUP-006 | supervisor-tests-and-runbook | task_packages | completed | 2026-06-02T02:05:02Z | task report: .engineering/reports/tasks/20260602T015705Z-supervisor-tests-and-runbook.md; task manifest: .engineering/reports/tasks/20260602T015705Z-supervisor-tests-and-runbook... |
<!-- engineering-orchestrator:docs-sync:end -->

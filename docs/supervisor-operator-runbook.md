# Supervisor Operator Runbook

This runbook covers the Supervisor Codex role in gated drive operation. The supervisor role reads local
evidence, emits a structured supervisor decision, and leaves worker implementation to normal task
executors.

## Evidence Locations

- Supervisor context packs: `.engineering/reports/tasks/supervisor-context-packs/`
- Supervisor decision validation reports: `.engineering/reports/tasks/supervisor-decisions/`
- Supervisor mutation reports: `.engineering/reports/tasks/supervisor-mutations/`
- Drive and parallel-drive reports: `.engineering/reports/tasks/`
- Runtime status: `engo status --project-root <project> --json`

## Gate Checklist

1. Confirm the gate reason in the drive report under `supervisor_gates`.
2. Open the listed context pack and verify `local_only` is `true`.
3. Open the supervisor decision manifest and check `status`, `decision`, `requires_human`, and
   `safety_classification`.
4. Open the mutation manifest and check whether the action was applied, skipped, rejected, or held for
   human review.
5. Resume work only after the drive report identifies a clear local next action.

## Scenario Handling

Success or declared quality gate:
Run `engo drive --supervisor-gate quality-gate-completion --json`. A low-risk `continue` supervisor
decision may apply `approved_next_tasks` as queue order. Confirm the mutation report shows
`queue_order_applied`.

Failed task:
Run or inspect `engo drive --supervisor-gate failed-task --json`. The default supervisor gate pauses
scheduling and records the failed task evidence. Fix the local failure or provide a validated retry
decision before continuing.

Blocked task:
Run or inspect `engo drive --supervisor-gate blocked-task --json` or
`engo parallel-drive --supervisor-gate blocked-task --json`. Review blocking policy decisions, approvals,
and the supervisor mutation report before retrying.

Milestone completion:
Run `engo drive --supervisor-gate milestone-completion --json`. The gate should cite the completed
milestone in the context pack risk metadata and may continue to the next pending task if local evidence
supports it.

Deployment or secret-sensitive gate:
Run `engo drive --supervisor-gate deployment-sensitive-task --json`. Worker execution should pause before
the sensitive task runs. Treat `enter_deployment_audit`, live production, secret, network, or destructive
git risk as human-reviewed work.

Task reordering:
Provide a validated `continue` supervisor decision with `approved_next_tasks`, then run
`engo drive --supervisor-gate operator-request --supervisor-decision <decision.json> --json`. The roadmap
file is not rewritten; the durable queue order is stored in `.engineering/state/harness-state.json`.

Unsafe decision rejection:
If a supervisor decision includes commands, patches, missing human review for high-risk work, or an
understated safety classification, validation must reject it. Inspect `validation_errors` and leave the
queue unchanged.

Missing evidence:
Every supervisor decision must cite existing local evidence paths inside the project. Missing paths,
remote URLs, or paths outside the project root are rejected before mutation.

Bounded recursion:
Supervisor context packs bound list width, text length, and nested JSON depth for risk metadata and log
entries. If deeply nested local metadata is truncated, inspect the `max_depth` marker and gather a smaller
local artifact for the next supervisor decision.

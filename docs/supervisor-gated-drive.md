# Supervisor Gated Drive

`drive` and `parallel-drive` can invoke supervisor gates at configured orchestration boundaries while
leaving worker task execution unchanged. A supervisor gate builds a local-only supervisor context pack,
validates a structured `engineering-orchestrator.supervisor-decision.v1` decision, and records the
context path, decision path, applied or skipped status, and reason in the drive report JSON and Markdown.
Operator handling for success, failed task, blocked task, milestone completion, deployment gate, task
reordering, unsafe decision rejection, missing evidence, and bounded recursion is documented in
`docs/supervisor-operator-runbook.md`.

Enable gates from the CLI:

```bash
engo drive --supervisor-gate failed-task --supervisor-gate quality-gate-completion
engo parallel-drive --supervisor-gate all
```

Roadmaps may also configure gates:

```json
{
  "supervisor_gates": {
    "enabled": true,
    "gates": [
      "failed_task",
      "blocked_task",
      "milestone_completion",
      "deployment_sensitive_task",
      "quality_gate_completion",
      "budget_risk_threshold",
      "operator_request"
    ],
    "budget_risk_threshold": {
      "max_risk_score": 80
    }
  }
}
```

Supported gate types are failed task, blocked task, milestone completion, declared deployment-sensitive
task, quality-gate completion, budget/risk threshold, and explicit operator request. Declared deployment
and quality gates are detected from task metadata such as `deployment_sensitive`, `secret_sensitive`,
`quality_gate`, `quality_gates`, or task-level `supervisor_gates`.

The orchestrator auto-applies only low-risk `continue`, `pause`, and `retry` decisions. A low-risk
`continue` decision can set `approved_next_tasks`; drive and parallel-drive then prefer that durable
supervisor queue order while preserving task status, attempt, dependency, dirty-worktree, and file-scope
guards. The roadmap file is not rewritten for this safe queue mutation.

Every gate writes a supervisor mutation JSON manifest and Markdown report under
`.engineering/reports/tasks/supervisor-mutations/`. These artifacts record whether the mutation was
applied, skipped, rejected by validation, or held as `requires_human`, and they link back to the
context pack and supervisor decision evidence.

High-risk or human-required decisions are persisted and skipped until an operator handles them. This
includes `drop_task`, deployment actions, secret handling, live production mutation, destructive git,
broad roadmap rewrites, architecture goal changes, and any other unsafe supervisor request. Supervisor
decisions cannot include commands, patches, or implementation edits; worker executors remain the only
code-editing path.

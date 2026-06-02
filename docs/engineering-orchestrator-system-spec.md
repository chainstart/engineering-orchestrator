# Engineering Orchestrator System Specification

## Purpose

Engineering Orchestrator is a roadmap-driven control plane for autonomous software engineering agents.
It exists to turn a goal or specification into a durable production engineering workflow: planning,
implementation, validation, repair, evidence capture, checkpointing, and continuation.

The orchestrator must be domain-neutral. It should support web applications, mobile applications, games,
backend services, developer tools, agents, embedded software, Verilog/HDL, EDA flows, formal
verification, data systems, CI/CD, and operational automation. Domain behavior should come from
profiles, executors, playbooks, policies, and project-specific specifications rather than from a
single hard-coded project type.

## Compatibility Terminology

Engineering Orchestrator is the canonical public product name. `engineering_orchestrator` is the
canonical Python implementation package for new code. Engineering Harness is a legacy compatibility
name retained for existing users and persisted artifacts. During the staged migration,
`engineering_harness` imports, `engh`, `.engineering/state/harness-state.json`, `EH-SPEC-*`
requirement ids, and `engineering-harness.*` schema kind strings remain valid compatibility
contracts.

Agent harness means an agent-internal LLM/tool/runtime loop component. It is distinct from
Engineering Orchestrator, which is the external roadmap-driven engineering control plane.

## Product Boundary

Engineering Orchestrator is not itself the coding model. It is the engineering workflow around coding
models and local or remote tools.

The orchestrator owns:

- goal and specification intake;
- roadmap and task state;
- executor selection and policy gates;
- acceptance, repair, and end-to-end validation loops;
- durable manifests, reports, and audit evidence;
- checkpoint, CI, and release integration boundaries;
- continuation and self-iteration control.

Executors own concrete work such as editing files, running shell commands, invoking coding agents,
running HDL simulators, delegating to CI, or calling future model APIs.

## System Principles

- The specification is the source of intent.
- The roadmap is the executable plan derived from the specification.
- Completion must be proven by local or declared evidence, not by model self-assessment.
- Long-running work must be resumable, observable, auditable, and bounded by budget.
- Risky operations must be explicit, policy-gated, and reviewable.
- Domain-specific behavior must be pluggable.
- Public project documentation and repository artifacts must be English.

## Requirement IDs

Most stable product requirements use an `EH-SPEC-###` id. Rename-specific requirements use an
`EO-SPEC-###` id. Roadmap tasks and command gates should reference these ids with `spec_refs`.

### EO-SPEC-001: Engineering Orchestrator Rename Compatibility

The orchestrator must present Engineering Orchestrator as the canonical public product name while
retaining Engineering Harness as a legacy compatibility name during the staged migration.

Acceptance evidence:

- README, docs, CLI help, and tests use Engineering Orchestrator as the canonical product name.
- Documentation states that Engineering Harness is a legacy compatibility name.
- New code can import `engineering_orchestrator`; `engineering_harness` imports, `engh`, existing
  state paths, and `engineering-harness.*` schema kind strings remain valid compatibility
  contracts.

## Functional Requirements

### EH-SPEC-001: Specification Intake

The orchestrator must accept a project specification as a first-class input. The specification may be a
local Markdown document, a structured JSON/YAML contract, or a generated normalized goal intake
artifact.

Acceptance evidence:

- A project can declare its canonical spec path.
- Roadmap generation can cite the source spec.
- Status and manifests can expose the spec reference used by a task.

### EH-SPEC-002: Spec-To-Roadmap Planning

The orchestrator must derive an executable roadmap from the project specification. The roadmap must
contain milestones, tasks, continuation stages, acceptance gates, and end-to-end gates that trace
back to spec requirements.

Acceptance evidence:

- Generated roadmap tasks include `spec_refs`.
- Validation catches malformed spec references.
- Reports show which spec requirements a task claims to satisfy.

### EH-SPEC-003: Task Graph And Execution Phases

The orchestrator must model implementation work as tasks with explicit phases: `implementation`,
`acceptance`, `repair`, and `e2e`. Future versions should support dependencies and affected-task
selection.

Acceptance evidence:

- Task payloads include phase definitions and file scope.
- Failed acceptance can trigger repair up to a bounded iteration limit.
- E2E failure can fail a task after acceptance passes.

### EH-SPEC-004: Executor Abstraction

The orchestrator must use a stable executor contract so shell commands, coding agents, CI jobs, Dagger
functions, HDL tools, and future workers can be swapped without changing roadmap semantics.

Acceptance evidence:

- Executor metadata declares capabilities, input mode, approval needs, and policy behavior.
- Executor results normalize status, return code, stdout, stderr, metadata, and watchdog evidence.
- Unknown executors fail validation or preflight with actionable errors.

### EH-SPEC-005: Model And Memory Layer

The orchestrator must support future model routing, prompt templates, context compression, project
knowledge indexes, cost budgets, and long-term memory as auditable artifacts.

Acceptance evidence:

- Agent prompts receive bounded task, spec, file-scope, and verification context.
- Memory and context packs can be inspected without relying on a transient chat session.
- Sensitive values are redacted before persistence.

### EH-SPEC-006: Durable Autonomous Runtime

The orchestrator must support unattended runs that can pause, resume, cancel, recover stale state, enforce
timeouts, track heartbeats, and continue across roadmap stages.

Acceptance evidence:

- Drive state records current activity, task, phase, heartbeat, and stop reason.
- `pause`, `resume`, and `cancel` mutate durable drive control without deleting task history.
- Stale-running state can be detected and cleared deliberately.

### EH-SPEC-007: Production Acceptance And E2E Evidence

The orchestrator must require production-relevant evidence for completion. Depending on domain, this may
include unit tests, integration tests, browser E2E, API journeys, CLI journeys, HDL simulation,
formal checks, hardware-in-the-loop tests, security scans, or deployment smoke tests.

Acceptance evidence:

- Roadmap tasks declare local acceptance and E2E commands.
- Manifests include command result summaries and artifact paths.
- User or operator journeys are tied to the project experience plan.

### EH-SPEC-008: Manifest, Report, And Audit Trail

Every task run must leave durable machine-readable and human-readable evidence.

Acceptance evidence:

- Each task run writes a JSON manifest and Markdown report.
- Manifests include project, task, spec refs, phase runs, policy decisions, safety audit, git state,
  and artifact paths.
- Manifest indexes summarize project history for dashboards and CI.

### EH-SPEC-009: Git, CI, And Release Integration

The orchestrator must support clean git boundaries, task checkpoints, optional pushes, CI workflow
integration, PR feedback, failed-CI triage, and release evidence.

Acceptance evidence:

- Checkpoint readiness classifies clean, orchestrator-owned, task-scoped, and unrelated dirty paths.
- Successful tasks can create commits and optionally push.
- Future CI adapters can map failed checks back to spec refs and roadmap tasks.

### EH-SPEC-010: Policy And Governance

The orchestrator must guard risky work through structured policy decisions.

Acceptance evidence:

- Commands are checked against allowlists, blocked patterns, live-operation gates, file scope, and
  unsafe capability classification.
- Agent, manual, live, deployment, secret, network, and filesystem risks are visible in policy
  decisions.
- Approval leases are durable, fingerprinted, and stale when task or policy inputs change.

### EH-SPEC-011: Operator Experience

The orchestrator must provide operator-facing status and, eventually, a local or hosted dashboard.

Acceptance evidence:

- `status --json` exposes runtime dashboard data.
- Reports identify blockers and local next actions.
- Future UI surfaces can read manifests and state without reimplementing core logic.

### EH-SPEC-012: Domain Packs

The orchestrator must remain general while supporting domain-specific workflows through profiles,
executor adapters, templates, and playbooks.

Acceptance evidence:

- Profiles can define safe command policies and starter tasks.
- Domain packs can add validation templates and E2E patterns without changing core task semantics.
- Embedded and HDL workflows can model simulation and synthesis checks as executors and E2E gates.

### EH-SPEC-013: Self-Iteration

The orchestrator must be able to assess current state and append the next bounded continuation stage when
configured to do so.

Acceptance evidence:

- Self-iteration reads a bounded context pack and writes only allowed roadmap changes.
- Duplicate continuation plans are detected.
- Unsafe live requirements are rejected before acceptance.

### EH-SPEC-014: Public Distribution

The orchestrator must be usable as an open project for broad software engineering automation.

Acceptance evidence:

- Repository documentation is English.
- The project has an explicit Apache-2.0 license.
- Public README positioning does not bind the project to a private workspace or single domain.

### EH-SPEC-015: Target Spec Synchronization

The orchestrator must help target projects keep their own dynamic specification systems current after a
task or stage is implemented. A target project may maintain `.engineering/spec_tasks.yaml`,
implementation-status documentation, decision records, and a spec update log. The orchestrator should be
able to audit that structure and record task completion evidence without requiring the target project
to use Engineering Orchestrator internals.

Acceptance evidence:

- `engo spec-sync audit` validates the target project's dynamic spec maintenance files.
- `engo spec-sync record --apply` updates the target task ledger and appends a machine-readable
  update log entry.
- Completed orchestrator tasks attempt a best-effort update when the target task id is present in
  `.engineering/spec_tasks.yaml`.
- Missing or unmatched spec tasks are reported as skipped evidence, not silently treated as success.

### EH-SPEC-016: Target Documentation Synchronization

The orchestrator must help target projects keep their documentation hierarchy current after
implementation work. Target repositories may separate architecture blueprints, roadmaps, roadmap status
tables, canonical specs, traceability documents, task-package documents, deployment runbooks, and
machine-readable `.engineering` ledgers. A completed task may need to update more than the spec task
ledger; it may also need to refresh roadmap status, implementation status, traceability links, task
package state, deployment state, or a decision record.

Acceptance evidence:

- Target projects can declare documentation roles such as architecture blueprint, roadmap, roadmap
  status table, canonical specs, traceability documents, task package documents, deployment status, and
  decision log directory.
- A documentation audit reports missing docs, stale status links, inconsistent task/package statuses,
  and local-implementation versus deployed-status ambiguity.
- A documentation update command can propose or apply bounded updates for a completed task while
  preserving the target project's hierarchy rules.
- Completed task manifests record whether target documentation sync was applied, proposed, skipped, or
  blocked, with paths and evidence.
- Post-task drive and run flows can invoke documentation synchronization for target repositories that declare documentation roles, including roadmap progress, implementation status, actual system state, traceability, and task package documents.
- Documentation sync never marks work complete without local evidence, and never changes high-level
  architecture goals unless the task explicitly includes an architecture change.


### EH-SPEC-017: Native Parallel Development Orchestration

The orchestrator must be able to consume multiple eligible roadmap task packages and execute them as a bounded native parallel development run, without depending on an external auto-continuation supervisor or a human manually launching several orchestrator processes.

A parallel run must plan safe work lanes from task package metadata, create isolated git branches and worktrees, launch bounded worker processes, monitor worker heartbeats and task manifests, schedule the next eligible task when a worker frees, and merge successful branches back to the base branch after local acceptance and merge validation pass. Successfully merged task branches and temporary worktrees should be cleaned automatically. Failed or blocked task branches must be preserved with reports so an operator can inspect and repair them.

Acceptance evidence:

- A native command, for example `engo parallel-drive`, can run a bounded number of eligible roadmap tasks with `--max-workers`, `--max-tasks`, and `--time-budget-seconds` controls.
- The planner avoids parallelizing tasks with overlapping write scopes, explicit dependency conflicts, or unresolved dirty worktree state unless an explicit override is provided.
- Each worker receives an isolated branch/worktree, writes a task manifest, and records implementation, acceptance, repair, merge, and cleanup results.
- Completed task branches are merged back to the configured base branch only after acceptance passes; merged task branches and temporary worktrees are deleted automatically.
- Blocked or failed tasks do not block unrelated queued tasks, but their branches, reports, and state are retained for review.
- A parallel run is resumable from durable state and can continue dispatching the next backlog task when a worker completes.

### EH-SPEC-018: Supervisor Codex Role And Gated Drive Decisions

The orchestrator must support a supervisor coding-agent role that evaluates task results and planning
gates without doing concrete implementation work. Worker coding-agent processes edit files, run
commands, and satisfy task packages. The supervisor coding-agent reads bounded orchestration context,
task manifests, reports, roadmap state, docs-sync/spec-sync evidence, git summaries, and pending task
metadata, then returns a structured decision that the orchestrator core validates before changing the
queue.

The supervisor role exists to make long task sequences adaptive without relying on a transient chat
session, an external watcher, or unbounded self-iteration. It should be invoked only at configured gates:
task failure or blocked state, milestone completion, deployment or secret-sensitive gates, completion of
a declared quality gate, roadmap/spec inconsistency, budget/risk thresholds, or explicit operator
request.

Acceptance evidence:

- The orchestrator can build a bounded supervisor context pack from local evidence only.
- A supervisor decision schema validates actions such as `continue`, `pause`, `retry`, `repair_task_package`,
  `split_task`, `merge_tasks`, `drop_task`, `create_followup_tasks`, `request_human_review`, and
  `enter_deployment_audit`.
- Drive and parallel-drive can invoke the supervisor at declared gates and record the decision in
  manifests or drive reports.
- Low-risk queue decisions can be applied automatically; high-risk changes such as deleting tasks,
  changing architecture goals, deployment actions, secret handling, or live production mutations require
  explicit approval.
- Supervisor processes cannot directly edit project business code, cannot mark failed tests as passed,
  and cannot recursively call themselves without bounded iteration limits.

## Nonfunctional Requirements

- **Local-first**: workflows must be runnable on a developer machine before they are delegated to
  hosted infrastructure.
- **Auditable**: persisted state should explain what ran, why it ran, what passed, what failed, and
  what remains.
- **Resumable**: interrupted work should continue without duplicating completed phases unless
  explicitly requested.
- **Deterministic where possible**: validation, planning scaffolds, status summaries, and safety
  checks should be deterministic and testable.
- **Secure by default**: secrets, private keys, live services, production mutations, and destructive
  filesystem operations must not be silently permitted.
- **Extensible**: executors, profiles, policies, and domain packs must evolve without collapsing the
  core into one vertical product.

## Initial Traceability Contract

Roadmap tasks and commands may declare:

```json
{
  "spec_refs": ["EH-SPEC-002", "EH-SPEC-008"]
}
```

Roadmaps may declare the canonical local specification and, optionally, a structured requirement
index:

```json
{
  "spec": {
    "path": "docs/engineering-orchestrator-system-spec.md",
    "kind": "markdown",
    "requirements_index": "docs/spec-index.json"
  }
}
```

The traceability contract must:

- validate `spec_refs` as a list of non-empty unique strings;
- validate the top-level `spec` block when provided;
- validate task and command `spec_refs` against known requirement ids when a requirements index or
  parseable canonical spec path is configured;
- preserve task-level and command-level spec references in task payloads;
- include spec references in policy input, manifests, reports, and executor task context;
- expose compact spec coverage in `status --json`;
- keep roadmaps without `spec_refs` backward compatible.

The requirement index is local-only. It may be a JSON/YAML mapping with `requirements`, `ids`, or
`requirement_ids`, nested structured groups, or an inline roadmap list/mapping containing
requirement ids. Exact requirement-id strings and requirement-id mapping keys are de-duplicated.
Markdown spec paths with requirement headings are indexed directly when no separate index is
provided.

## Dynamic Target Spec Maintenance

Projects can opt into a lightweight dynamic spec contract:

```json
{
  "kind": "project.spec_tasks.v1",
  "project": "example",
  "source_spec": "docs/spec.md",
  "status_doc": "docs/implementation_status.md",
  "roadmap_doc": "docs/roadmap.md",
  "roadmap_status_doc": "docs/roadmap-status.md",
  "documentation": {
    "architecture_blueprint": "docs/architecture.md",
    "roadmap": "docs/roadmap.md",
    "roadmap_status": "docs/roadmap-status.md",
    "traceability": ["docs/spec-traceability.md"],
    "task_packages": ["docs/task-packages.md"],
    "deployment_status": "docs/deployment-status.md"
  },
  "decision_log_dir": "docs/decisions",
  "requirements": [{"id": "REQ-EXAMPLE-001", "title": "Example", "status": "pending"}],
  "tasks": [{"id": "example-task", "status": "pending", "requirement_ids": ["REQ-EXAMPLE-001"]}]
}
```

The orchestrator command surface is:

```bash
engo spec-sync audit --project-root /path/to/target --json
engo spec-sync record --project-root /path/to/target \
  --task-id example-task \
  --status completed \
  --evidence "python3 -m pytest -q" \
  --apply
```

This does not replace roadmaps. The roadmap remains the executable plan; the dynamic spec ledger is
the requirement-level memory that survives across roadmap rewrites, long-running branches, and
domain-specific repositories.

Dynamic documentation maintenance is a sibling capability to spec synchronization. Spec sync records
requirement and task status in a compact ledger. Documentation sync updates the target project's human
source-of-truth documents so architecture, roadmap, spec, task-package, local implementation, and
deployment status do not drift apart.

```bash
engo docs-sync audit --project-root /path/to/target --json
engo docs-sync propose --project-root /path/to/target \
  --task-id example-task \
  --evidence "python3 -m pytest -q" \
  --json
engo docs-sync record --project-root /path/to/target \
  --task-id example-task \
  --status completed \
  --evidence "python3 -m pytest -q" \
  --apply
```

Documentation synchronization only writes bounded managed evidence blocks and machine-readable update
logs automatically. Architecture blueprints, canonical specs, roadmap intent, and decision records are
reported as manual-review targets unless the implementation task explicitly calls for those changes.

## Supervisor Role Contract

The supervisor is not a worker executor. It is a planning and governance role inside the orchestrator
control plane.

Supervisor input:

- active objective and roadmap summary;
- recently completed, failed, blocked, or merged task reports;
- bounded git status and diff summary;
- manifest index and relevant task manifests;
- docs-sync and spec-sync results;
- pending tasks, dependencies, file scopes, and risk metadata;
- configured gate reason, for example milestone completion or deployment audit.

Supervisor output must be a structured `engineering-orchestrator.supervisor-decision.v1` document:

```json
{
  "kind": "engineering-orchestrator.supervisor-decision.v1",
  "decision": "continue",
  "approved_next_tasks": ["example-next-task"],
  "blocked_tasks": [],
  "tasks_to_rewrite": [],
  "requires_human": false,
  "reason": "Local evidence supports continuing to the next queued task.",
  "evidence": [".engineering/reports/tasks/example.json"]
}
```

The orchestrator core owns validation and application. The supervisor may propose roadmap changes, but
the core must reject invalid schema, unsafe actions, missing evidence, unapproved high-risk changes, and
unbounded recursion.

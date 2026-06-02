# Spec-Driven Development Plan

This plan turns the Engineering Orchestrator system specification into executable engineering stages.
Each stage references requirement ids from [Engineering Orchestrator System Specification](engineering-orchestrator-system-spec.md).

## Operating Model

The target operating model is:

```text
spec
  -> traceable roadmap
  -> task graph
  -> executor work
  -> acceptance and E2E evidence
  -> manifest/report audit trail
  -> checkpoint, CI, continuation
```

The orchestrator should describe this chain directly in project artifacts. A task without traceability can
still run for backward compatibility, but production roadmaps should eventually cite the spec
requirements they satisfy.

## Stage 1: Spec Traceability Foundation

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-003`
- `EH-SPEC-008`

Goal:

Make spec requirements visible in roadmap tasks, command gates, manifests, reports, and policy input.

Tasks:

1. Add `spec_refs` to task and command parsing.
2. Validate `spec_refs` as non-empty unique string lists when provided.
3. Preserve `spec_refs` in task payloads and policy input.
4. Include `spec_refs` in task manifests and Markdown reports.
5. Add tests for validation, task payloads, manifests, and reports.

Acceptance:

- Roadmaps with valid task and command `spec_refs` pass validation.
- Invalid `spec_refs` produce actionable validation errors.
- A completed task manifest includes task and command `spec_refs`.
- A completed task report includes a `Spec Traceability` section.

## Stage 2: Canonical Spec Index

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-014`

Goal:

Represent the canonical project spec as a machine-readable index that can be validated separately
from the roadmap.

Tasks:

1. Add a top-level roadmap `spec` block with `path`, `kind`, and optional `requirements_index`.
2. Parse requirement ids from a structured spec index.
3. Validate that task `spec_refs` point to known requirement ids when an index is configured.
4. Add CLI output that summarizes spec coverage.

Acceptance:

- A roadmap can declare the canonical spec path.
- Invalid references to unknown requirement ids are reported.
- `status --json` includes compact spec coverage.

## Stage 3: Spec-To-Roadmap Planner

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-013`

Goal:

Generate or update roadmap stages from a specification while preserving traceability.

Tasks:

1. Extend `plan-goal` or add a dedicated planning command that reads a spec document.
2. Generate milestones, tasks, acceptance gates, E2E gates, and `spec_refs`.
3. Add duplicate-plan detection based on spec refs and task semantics.
4. Make self-iteration append continuation stages that cite spec refs.

Acceptance:

- Generated tasks cite spec refs.
- The planner does not duplicate existing roadmap coverage.
- Self-iteration can explain which requirements the new stage advances.

## Stage 4: Executor And Memory Context

Requirement refs:

- `EH-SPEC-004`
- `EH-SPEC-005`
- `EH-SPEC-010`

Goal:

Give executors bounded spec context and make memory auditable.

Tasks:

1. Include task spec refs and requirement excerpts in agent prompts.
2. Add a context-pack contract for spec, roadmap, tests, manifests, and git state.
3. Track model/cost/context metadata in executor results where available.
4. Redact sensitive values before context and memory persistence.

Acceptance:

- Agent prompts include relevant spec refs without loading unbounded documents.
- Context packs are persisted and referenced by manifests.
- Sensitive values are redacted in context artifacts.

## Stage 5: Production Evaluation Matrix

Requirement refs:

- `EH-SPEC-007`
- `EH-SPEC-012`

Goal:

Map requirement types to domain-specific evidence.

Tasks:

1. Define evaluation templates for web, API, CLI, agent, embedded, HDL, formal, and DevOps projects.
2. Add profile or domain-pack defaults for acceptance and E2E commands.
3. Record evidence type and artifact paths per spec ref.
4. Add failure summaries that identify which requirements remain unproven.

Acceptance:

- A task can show requirement coverage by evidence type.
- HDL and embedded workflows can model simulator or hardware checks as E2E gates.
- Reports distinguish failed implementation from unproven requirement evidence.

## Stage 6: CI/CD And Operator Workflow

Requirement refs:

- `EH-SPEC-006`
- `EH-SPEC-009`
- `EH-SPEC-011`

Goal:

Connect local autonomous runs to repository-native workflows and operator surfaces.

Tasks:

1. Generate CI workflows for acceptance and E2E checks.
2. Map CI failures back to task ids and spec refs.
3. Publish PR comments or status artifacts from manifests.
4. Build a dashboard-ready data contract for goals, tasks, requirements, approvals, and evidence.

Acceptance:

- CI results can be traced back to roadmap tasks and spec requirements.
- Operators can inspect blockers, approvals, and evidence without reading raw logs.
- Dashboard data is derived from manifests and durable state.

## Stage 7: Target Spec Synchronization

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-008`
- `EH-SPEC-015`

Goal:

Keep the developed project's own spec maintenance files current after each task or stage.

Tasks:

1. Define the `.engineering/spec_tasks.yaml` target-project contract.
2. Add an audit command that checks source spec, implementation status doc, decision log directory,
   requirement ids, task ids, and task-to-requirement references.
3. Add a record command that updates a target task status, appends evidence, and writes
   `docs/spec_update_log.jsonl`.
4. Add best-effort task-completion sync from `run` and `drive` when the roadmap task id exists in
   the target spec task ledger.
5. Document how orchestrator task packages should update the developed repository's spec after each
   implementation stage.

Acceptance:

- `engo spec-sync audit --project-root <target> --json` reports passed/warning/failed with checks.
- `engo spec-sync record --project-root <target> --task-id <id> --status completed --apply` updates
  the task ledger and appends a JSONL update log.
- A completed orchestrator task records skipped/updated sync evidence in its result payload.
- Target projects can keep requirement status independent from transient roadmap structure.

## Stage 8: Target Documentation Synchronization

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-008`
- `EH-SPEC-015`
- `EH-SPEC-016`

Goal:

Keep the developed project's human-readable documentation hierarchy current after each task or stage,
without collapsing architecture, roadmap, spec, task-package, and deployment status into one document.

Tasks:

1. Extend the target-project contract with optional documentation roles: architecture blueprint, roadmap,
   roadmap status table, canonical specs, traceability docs, task-package docs, deployment status docs,
   decision log directory, and machine-readable update logs.
2. Add a `docs-sync audit` command that reports missing roles, stale links, conflicting task statuses,
   outdated status docs, and ambiguity between local implementation and deployed status.
3. Add a `docs-sync propose` or `docs-sync record` path that writes a bounded update plan for a completed
   task and can apply low-risk status-table / ledger updates when explicitly requested.
4. Connect completed `run` and `drive` tasks to best-effort documentation sync evidence, separate from
   spec-sync evidence.
5. Add context-pack support so coding executors know which target docs must be updated when a task changes
   architecture, roadmap, spec, task package, tests, deployment, or user-facing behavior.

Acceptance:

- `engo docs-sync audit --project-root <target> --json` reports role coverage and status consistency.
- `engo docs-sync propose --project-root <target> --task-id <id> --json` produces a bounded update plan
  with target paths, update reasons, evidence, and safety classification.
- `engo docs-sync record --project-root <target> --task-id <id> --apply` can update safe status-table or
  machine-ledger fields and append a documentation update log.
- Task manifests distinguish `spec_sync` from `docs_sync` and preserve skipped / blocked reasons.
- The orchestrator refuses to mark architecture or deployment status complete unless the task evidence
  explicitly supports that status.

## Stage 9: Supervisor Codex Role And Gated Drive Decisions

Requirement refs:

- `EH-SPEC-003`
- `EH-SPEC-005`
- `EH-SPEC-006`
- `EH-SPEC-008`
- `EH-SPEC-010`
- `EH-SPEC-011`
- `EH-SPEC-013`
- `EH-SPEC-017`
- `EH-SPEC-018`

Goal:

Add a bounded supervisor coding-agent role that evaluates completed or failed work, decides whether the
next task package remains valid, and proposes safe queue changes at configured gates. This replaces
external watcher/handoff scripts for ordinary orchestration supervision while keeping concrete code
edits inside worker tasks.

Tasks:

1. Define the supervisor role, trigger conditions, safety boundaries, and decision vocabulary.
2. Build a supervisor context pack from manifests, reports, roadmap state, git summaries, sync evidence,
   pending task metadata, and gate reason.
3. Add a `supervisor_decision.v1` schema with deterministic validation and safety classification.
4. Integrate supervisor gates into `drive` and `parallel-drive`.
5. Add safe roadmap / queue mutation support for low-risk decisions and explicit approval requirements
   for high-risk changes.
6. Add tests and runbook coverage for failure, milestone completion, deployment gate, and task reordering
   scenarios.

Acceptance:

- Worker executors still own implementation; supervisor executors do not directly edit business code.
- Supervisor decisions are persisted as auditable artifacts.
- Invalid or unsafe decisions are rejected before mutating the queue.
- Gated drives can continue, pause, retry, request human review, or enter deployment audit based on
  structured local evidence.

## Current Implementation Target

Stages 1 and 2 establish traceability and the canonical spec index. Stage 3 starts with the local
`plan-spec` command, which reads a spec-driven development document and proposes or materializes
traceable continuation stages without adding duplicate coverage. Duplicate detection compares
source task semantics with existing roadmap task semantics under the same spec refs, so partially
covered stages keep only the still-uncovered tasks. Generated spec traceability is kept when those
stages advance into active milestones. Self-iteration context packs expose the spec traceability
expectation, validation rejects new stages or tasks without `spec_refs` when the existing roadmap is
spec-traceable, and accepted assessments report which requirement refs newly appended stages advance.
Stage 7 adds target-project spec synchronization so Engineering Orchestrator can update the developed
repository's own dynamic spec ledger after task or stage completion. Stage 8 extends that idea from a
machine ledger to the target repository's documentation hierarchy: architecture blueprints remain the
total goal, roadmaps remain implementation plans, specs remain sub-plans and requirement sources, and
task packages / engineering roadmaps remain the smallest executable work units. Stage 9 adds a
supervisor coding-agent role inside the orchestrator control plane so long sequences can be evaluated at
gates and safely re-planned without relying on a transient chat session or an external watcher.

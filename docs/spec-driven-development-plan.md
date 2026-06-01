# Spec-Driven Development Plan

This plan turns the Engineering Orchestrator system specification into executable engineering stages.
Each stage references requirement ids from [Engineering Orchestrator System Specification](engineering-harness-system-spec.md).

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
repository's own dynamic spec ledger after task or stage completion.

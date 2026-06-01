# Decision: Add Target Documentation Synchronization

Date: 2026-06-01

Status: accepted

## Context

Engineering Orchestrator can synchronize a target project's machine-readable spec task ledger. Real
target repositories also maintain human-readable documentation hierarchies: architecture blueprints,
roadmaps, roadmap status tables, module specs, traceability docs, task-package docs, deployment runbooks,
and decision records. If the orchestrator only changes code and a compact task ledger, those documents
drift and users lose the ability to inspect where the system actually stands.

## Decision

Add a target documentation synchronization capability:

- Target projects may declare documentation roles in `.engineering/spec_tasks.yaml`.
- `engo docs-sync audit` should inspect the declared documentation roles, status links, task/package
  consistency, and local-versus-deployed status separation.
- `engo docs-sync propose` should produce a bounded update plan for a completed task, including target
  paths, evidence, safety classification, and whether the update is safe to apply automatically.
- `engo docs-sync record --apply` may apply low-risk status-table or machine-ledger updates and append
  a documentation update log.
- `run` and `drive` should record best-effort docs-sync evidence separately from spec-sync evidence.

## Consequences

- The orchestrator becomes responsible for keeping a developed repository's human source-of-truth docs
  current, not only its code and task ledger.
- Architecture, roadmap, spec, task package, and deployment status remain distinct roles rather than one
  mutable status blob.
- High-level architecture goal changes remain explicit and reviewable; docs-sync may not silently rewrite
  architecture intent just because implementation moved.
- Target repositories with no declared docs hierarchy still run normally; docs-sync reports skipped
  evidence instead of failing unrelated implementation work.


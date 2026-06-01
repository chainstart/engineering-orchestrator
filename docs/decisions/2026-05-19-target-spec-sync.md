# Decision: Add Target Project Spec Synchronization

Date: 2026-05-19

Status: accepted

## Context

Engineering Orchestrator can already materialize roadmap tasks from a spec and preserve `spec_refs`.
However, target repositories such as ARA, AMRA, ABRA, and AIRA also need their own long-lived spec
memory that records completed requirements, blockers, and evidence independently from a transient
roadmap.

## Decision

Add a target spec synchronization capability:

- `.engineering/spec_tasks.yaml` is the target project's machine-readable spec task ledger.
- `engo spec-sync audit` checks the ledger, source spec, status doc, decision log directory, ids,
  and task-to-requirement references.
- `engo spec-sync record --apply` updates one task, appends evidence, and writes
  `docs/spec_update_log.jsonl`.
- `run` and `drive` attempt a best-effort automatic record when the completed roadmap task id exists
  in the target spec task ledger.

## Consequences

- The orchestrator can now be instructed to update the developed repository's spec state after each task
  or stage.
- Target repositories remain loosely coupled because the contract is file-based.
- Missing task mappings are visible as skipped sync evidence instead of being silently ignored.

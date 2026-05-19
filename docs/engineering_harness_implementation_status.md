# Engineering Harness Implementation Status

Date: 2026-05-19

Status: dynamically maintained

Primary spec: `docs/engineering-harness-system-spec.md`

Development plan: `docs/spec-driven-development-plan.md`

Machine ledger: `.engineering/spec_tasks.yaml`

Decision records: `docs/decisions/`

## Requirement Status

| Requirement ID | Status | Evidence | Main Gap | Next Step |
| --- | --- | --- | --- | --- |
| `EH-SPEC-001` | `completed` | canonical spec path and spec_refs support | continue hardening external target specs | keep validation current |
| `EH-SPEC-002` | `completed` | `plan-spec` / `spec-backlog` materialization | richer requirement coverage reports | continue planner evolution |
| `EH-SPEC-003` | `completed` | implementation/acceptance/repair/e2e phases | dependency graph remains future work | add dependencies later |
| `EH-SPEC-004` | `partial` | executor adapters and metadata | hosted/external adapters need more hardening | continue executor work |
| `EH-SPEC-005` | `partial` | bounded context and runtime support | long-term memory still evolving | integrate memory packs |
| `EH-SPEC-006` | `completed` | drive controls, stale recovery, daemon supervisor | more production policies possible | keep reliability work ongoing |
| `EH-SPEC-007` | `partial` | acceptance/e2e evidence and domain templates | per-domain evidence matrix incomplete | expand domain packs |
| `EH-SPEC-008` | `completed` | manifests, reports, audit trail | dashboard surfacing can improve | keep report schemas stable |
| `EH-SPEC-009` | `partial` | git checkpoint and push boundaries | CI/PR automation incomplete | add CI adapters |
| `EH-SPEC-010` | `partial` | command policy, approval gates, redaction | policy engine can become richer | continue policy hardening |
| `EH-SPEC-011` | `partial` | status JSON and operator console | hosted/dashboard UI pending | keep dashboard contract stable |
| `EH-SPEC-012` | `partial` | profiles and domain pack hooks | more domain-specific packs needed | add formal/AI/security packs |
| `EH-SPEC-013` | `completed` | self-iteration and duplicate-plan detection | higher-level planner quality can improve | continue goal-gap scoring |
| `EH-SPEC-014` | `partial` | English docs and public positioning | packaging/release polish remains | prepare public release checklist |
| `EH-SPEC-015` | `completed` | `spec_sync.py`, `engh spec-sync audit/record`, tests | optional deeper markdown status mutation | use JSONL + task ledger as source of truth |

## Maintenance Rules

1. Every new stable harness capability gets an `EH-SPEC-*` id.
2. Roadmap tasks cite requirement ids through `spec_refs`.
3. Completed implementation tasks update `.engineering/spec_tasks.yaml` and append evidence.
4. Changes that affect target-repository spec maintenance require both tests and documentation.

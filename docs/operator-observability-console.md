# Operator Observability Console

The operator console is a bounded, local-only observability payload layered on top of the existing
`engo status --json` contract. It does not call external services and does not require credentials.

Inspect the structured summary in status JSON:

```bash
bin/engo status --project-root . --json
```

The top-level `operator_console` block aggregates:

- queue state, next task, blocked tasks, and continuation state;
- recent task and drive run history with deterministic trends and status counts;
- bounded task phase timelines from `.engineering/state/harness-state.json`;
- pending, approved, consumed, and stale approval leases;
- unresolved isolated failures and recent failed or blocked task manifests;
- checkpoint readiness and local git blockers;
- goal-gap scorecard categories and recommended next-stage themes;
- replay-guard reuse evidence;
- E2E run evidence and local `artifacts/browser-e2e/` files;
- recommended operator actions ordered by current blockers and risk.

Generate static local artifacts:

```bash
bin/engo operator-console --project-root . --write --json
```

This writes:

- `.engineering/reports/operator-console/operator-console.json`
- `.engineering/reports/operator-console/operator-console.md`

The payload includes a `limits` block and a `bounds` block so operators and tests can verify the
summary stays bounded. The console is deterministic for unchanged local evidence: volatile status
timestamps from dashboard and scorecard generation are not copied into the console model.

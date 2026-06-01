# Local Full Lifecycle Smoke

The local full-lifecycle smoke is a bounded unattended E2E check for the orchestrator itself. It creates a
temporary workspace, materializes a starter roadmap through `plan-goal`, validates that generated
roadmap, seeds one shell-only task, dispatches a single `workspace-drive` tick, and verifies the
resulting local evidence.

Run it before long unattended supervisor loops:

```bash
python3 -m pytest tests/test_engineering_harness.py -q -k local_full_lifecycle_unattended_smoke
```

The broader smoke selector is:

```bash
python3 -m pytest tests/test_engineering_harness.py -q -k "full_lifecycle and smoke"
```

The smoke is intentionally local-only. It does not use network access, external accounts, private
keys, live trading, production deployment, paid services, mainnet writes, or real pushes. The seeded
task uses permitted `python3 -c` shell commands to write a deterministic artifact under the temporary
project, verify it, and write E2E evidence.

This complements the focused unit and integration tests rather than replacing them. The focused tests
still cover individual edge cases such as checkpoint-gate blocking, dirty git classification,
approval leases, capability denials, stale drive recovery, planner guards, and failure isolation. The
full-lifecycle smoke checks that the public CLI surfaces line up across the happy path: workspace
dispatch, drive JSON, task report, task manifest, manifest index, policy decisions, E2E evidence,
checkpoint readiness, failure-isolation absence, goal-gap scorecard, and runtime dashboard status.

For operator visibility after a smoke stage or before a longer supervisor loop, inspect status JSON:

```bash
bin/engo status --project-root . --json
```

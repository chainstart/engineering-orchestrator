# Local Goal Intake Contract

The goal intake contract is the deterministic local input boundary for creating a new autonomous
engineering project. It normalizes user intent before roadmap synthesis and rejects goals that
require unsafe live services.

## Accepted Inputs

Use `engineering_harness.goal_intake.normalize_goal_intake` or
`engineering_harness.goal_intake.validate_goal_intake` with these fields:

- `project_name`: required display name for the new project. Whitespace is collapsed and a stable
  lowercase slug is derived.
- `profile`: required orchestrator profile id, such as `python-agent`, `node-frontend`, or
  `trading-research`.
- `goal_text`: required high-level project goal. Empty or whitespace-only goals are rejected.
- `blueprint_path`: optional local path to a blueprint or planning document. URLs are rejected.
- `constraints`: optional list of local constraints. Whitespace is collapsed and duplicates are
  removed while preserving order.
- `desired_experience_kind`: optional requested experience kind. Supported values are
  `dashboard`, `submission-review`, `multi-role-app`, `app-specific`, `api-only`, and `cli-only`;
  common aliases such as `app`, `api only`, and `cli` normalize to the canonical ids.

## Normalized Output

Successful normalization returns a JSON-serializable mapping:

```json
{
  "schema_version": 1,
  "kind": "engineering-harness.goal-intake.v1",
  "project": {
    "name": "Autonomous Report Worker",
    "slug": "autonomous-report-worker",
    "profile": "python-agent"
  },
  "goal": {
    "text": "Build a local autonomous report generator."
  },
  "blueprint": {
    "path": "docs/blueprint.md",
    "provided": true
  },
  "constraints": ["local only"],
  "experience": {
    "kind": "dashboard",
    "provided": true
  },
  "safety": {
    "mode": "local-only",
    "allow_live_services": false,
    "blocked_requirements": [],
    "rules": [
      "Goals must be implementable without private credentials.",
      "Goals must not require production deployment, mainnet writes, live trading, or real-fund movement.",
      "Blueprints must be local paths, not remote URLs."
    ]
  },
  "roadmap_seed": {
    "project": "Autonomous Report Worker",
    "profile": "python-agent",
    "goal": "Build a local autonomous report generator.",
    "blueprint_path": "docs/blueprint.md",
    "constraints": ["local only"],
    "experience_kind": "dashboard"
  }
}
```

`roadmap_seed` is the compact handoff shape intended for later roadmap synthesis. It contains only
the normalized values needed to seed a roadmap.

The local CLI handoff is `plan-goal`, which renders a deterministic starter roadmap from this
contract. See [Goal Roadmap Planner](goal-roadmap-planner.md).

When `desired_experience_kind` is omitted, roadmap synthesis uses the domain frontend generator to
derive a required local experience plan. The derived decision is deterministic and appears in the
generated roadmap as `experience.decision_contract`.

## Validation Rules

Validation is pure and local. It does not call external services, read private credentials, deploy
software, or perform live trading.

The contract rejects:

- missing project names, profiles, or goals;
- unknown profile ids;
- empty constraints;
- non-local blueprint URLs;
- unsupported desired experience kinds;
- unsafe live-service requirements in the goal or constraints.

Unsafe live-service requirements include direct requirements for production or mainnet deployment,
mainnet write commands such as `cast send` or `--broadcast`, live trading or real order placement,
real-fund movement, private credential use, production service mutation, and paid live deployment.
Negated safety constraints such as "no live trading" are allowed.

Use `validate_goal_intake` when callers need a status payload:

```json
{
  "schema_version": 1,
  "kind": "engineering-harness.goal-intake.validation.v1",
  "status": "failed",
  "error_count": 1,
  "errors": ["`goal_text` is required"],
  "blocked_requirements": [],
  "goal_intake": null
}
```

Use `normalize_goal_intake` when invalid input should raise `GoalIntakeValidationError`.

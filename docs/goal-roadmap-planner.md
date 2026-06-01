# Goal Roadmap Planner

`plan-goal` is the local starter workflow that turns a high-level goal into a deterministic
`.engineering/roadmap.yaml` proposal.

It does not call model APIs, require external accounts, deploy software, move funds, or perform live
trading. The planner only normalizes local input and renders a template-driven roadmap.

## Usage

Preview a roadmap without writing files:

```bash
PYTHONPATH=src python3 -m engineering_orchestrator.cli plan-goal \
  --project-root /path/to/project \
  --name "Autonomous Report Worker" \
  --profile python-agent \
  --experience-kind dashboard \
  --constraint "Keep generated reports local and deterministic." \
  --stage-count 4 \
  --goal "Build an autonomous dashboard worker for local research artifacts."
```

Write the starter roadmap:

```bash
PYTHONPATH=src python3 -m engineering_orchestrator.cli plan-goal \
  --project-root /path/to/project \
  --name "Autonomous Report Worker" \
  --profile python-agent \
  --goal-file docs/goal.txt \
  --blueprint docs/blueprint.md \
  --materialize
```

Use `--force` only when replacing an existing `.engineering/roadmap.yaml`.

`--stage-count` is bounded from 1 to 4 and defaults to 4. The generated stages are deterministic:

1. `stage-1-local-slice`
2. `stage-2-experience-validation`
3. `stage-3-policy-evidence-observability`
4. `stage-4-unattended-drive-readiness`

Use a smaller stage count when the first roadmap should stay intentionally narrow. `--experience-kind`
can pin the experience plan to `dashboard`, `submission-review`, `multi-role-app`, `app-specific`,
`api-only`, or `cli-only`; otherwise the planner derives the kind from the project name, profile,
goal text, and blueprint path. Repeated `--constraint` values are normalized into the goal intake and
copied into generated task prompts.

## Domain Frontend Contract

Roadmap generation always carries a required frontend experience plan. The shared
`engineering_orchestrator.domain_frontend` module emits a local decision contract under
`experience.decision_contract`, `planning.domain_frontend`, and `generated_from.domain_frontend`.
The contract records the selected domain, experience kind, rule id, surface policy, rationale,
matched hints, and local-only constraints.

The default routing is domain-aware:

- autonomous theorem prover or Lean/formalization goals become `dashboard` plans with a
  `dashboard-only` surface policy for proof attempts and artifacts;
- student paper systems become `submission-review` plans covering submission, review, returned
  decisions, revision upload, and timeline state;
- multi-role systems become `multi-role-app` plans covering account setup, login, role assignment,
  permission denial, approval handoff, and audit history;
- ordinary software becomes `app-specific` with primary workspace, create/edit, detail, empty, and
  error views;
- API-first and CLI-first goals keep non-browser API or CLI experience contracts with local examples
  and deterministic journey checks.

`bin/engo frontend-tasks` includes the same decision contract in its proposal/materialization output.
`bin/engo status --json` exposes it at both top-level `domain_frontend` and
`runtime_dashboard.domain_frontend`, with the complete annotated plan under
`runtime_dashboard.frontend_experience`.

## Generated Roadmap Shape

The generated roadmap includes:

- an explicit `experience` block derived from the project name, profile, goal text, and blueprint
  path hints;
- a first `baseline` milestone with local `python3` acceptance commands that verify the starter
  roadmap and local-only safety settings;
- an ordered continuation backlog with a first local slice, experience validation, policy/evidence/
  observability hardening, and unattended drive readiness stages up to the configured stage count;
- every generated continuation task includes `file_scope`, `codex` implementation and repair
  entries, profile-aware local acceptance commands, and local e2e or journey-evidence gates tied to
  the experience plan;
- `self_iteration` guidance for profiles or goals that are likely to need rolling autonomous work.

The implementation and repair gates are configured as gated `codex` executor entries so normal
drives will require explicit `--allow-agent` approval before invoking an agent. Acceptance and e2e
commands remain local shell checks.

## Self-Iteration Safety Contract

When `self_iteration` is enabled, planner output is treated as an untrusted roadmap diff. Before the
planner runs, the orchestrator evaluates local checkpoint readiness. If unrelated dirty git paths would
block a clean roadmap materialization checkpoint, the planner is not invoked, `.engineering/roadmap.yaml`
is left unchanged, and a blocked self-iteration assessment is written under
`.engineering/reports/tasks/assessments/` with the checkpoint readiness, dirty paths, blocking paths,
reason, and recommended operator action.

After the planner exits, the orchestrator checks checkpoint readiness again before accepting the roadmap
diff. Harness-owned self-iteration artifacts from the current run, such as the snapshot, context pack,
assessment sidecar, and active orchestrator state file, are ignored for this acceptance check. Planner-made
unrelated dirty paths still block acceptance; the previous roadmap text is restored and the report
records the compact evidence.

The orchestrator then reloads `.engineering/roadmap.yaml` and accepts the output only when it:

- appends exactly `self_iteration.max_stages_per_iteration` new unmaterialized
  `continuation.stages` entries;
- leaves existing roadmap fields, milestones, tasks, task statuses, and continuation stages unchanged;
- avoids duplicate stage or task ids;
- avoids duplicate continuation plans by comparing deterministic semantic fingerprints of stable
  local fields such as titles, objectives, task titles, file scope, acceptance commands, and E2E
  commands while ignoring ids, generated timestamps, and status fields; the context pack also
  includes an identity fingerprint with task ids for auditability;
- when the existing roadmap is spec-traceable through a configured `spec` block or existing
  `spec_refs`, gives every new stage and task non-empty `spec_refs` so the assessment can explain
  which requirements the continuation advances;
- gives every new task non-empty `file_scope` and local acceptance commands;
- includes `codex` implementation and `codex` repair entries for tasks that require implementation
  work;
- avoids live operations, private keys, mainnet writes, production deployments, paid services,
  real-fund movement, and live trading requirements.

Accepted output is then checked with the normal roadmap validator. Invalid output is rejected, the
previous roadmap text is restored, and the self-iteration report records the validation errors. To
recover a checkpoint-gate block, resolve the listed `blocking_paths` locally with your own commit,
stash, move, or cleanup, then rerun self-iteration; the orchestrator will not clean or checkpoint those
operator-owned paths.

## Self-Iteration Planner Input

Before a self-iteration planner runs, the orchestrator writes a bounded JSON context pack next to the
self-iteration snapshot under `.engineering/reports/tasks/assessments/`. The planner prompt includes
both paths:

- `Status snapshot`: the machine-readable status snapshot for the run.
- `Planner context pack`: the bounded planner input contract.

Planners should read the context pack first and use the roadmap file only for the final append. The
context pack is local-only, redacted with the orchestrator secret redaction helper, and capped by count and
excerpt size. Its top-level fields are:

- `summary`: compact counts for continuation stages, duplicate-plan fingerprints, manifests,
  reports, docs, tests, source files, git status lines, recent commits, and spec traceability.
- `roadmap`: project/profile/goal metadata, task status counts, next task, continuation summary, and
  capped continuation stage summaries.
- `spec` and `spec_traceability`: compact coverage and whether appended stages must cite
  requirement refs with `spec_refs`.
- `duplicate_plan`: a bounded list of existing continuation-stage fingerprints and task-id/title
  hints so planners can avoid re-appending the same local plan under new ids.
- `manifests`: latest manifest-index summary plus the most recent task-run manifest summaries.
- `reports`: recent task report and drive report metadata.
- `docs`: blueprint metadata and capped excerpts from relevant local docs.
- `test_inventory` and `source_inventory`: capped local file inventories.
- `git`: repository flag, branch/head, short status lines, and recent commits.
- `goal_gap_scorecard`: deterministic local scorecard for unattended-reliability categories, including
  bounded evidence paths, `complete`/`partial`/`missing`/`blocked` status, numeric risk/severity, and
  recommended next-stage themes.

The self-iteration snapshot, JSON assessment, and Markdown report record the context-pack path,
summary, and goal-gap scorecard so an operator can audit exactly what the planner saw.

Planners should treat `goal_gap_scorecard.categories` as ordered priority evidence. A `blocked`
category means the next stage should resolve that local blocker before adding broad new work.
`missing` means the orchestrator lacks local evidence for the category, not that the capability is known
to be absent. Use `risk_score`, `severity`, `evidence_paths`, and `recommended_next_stage_themes`
instead of reading drive reports ad hoc when deciding the next self-iteration theme.

Do not treat protected live-drive or checkpoint-window evidence as a recovery blocker. A
stale-running category with an `in_progress` rationale means the current drive has a fresh heartbeat
and planners should wait for or build around that local run, not request
`recover-stale-running-drive`. A checkpoint category with `checkpoint_pending` or `in_progress`
rationale means only orchestrator-owned or file-scope paths are dirty. Plan `close-git-boundary` work only
when the category recommends it or `checkpoint_readiness.blocking_paths` is non-empty.

## Generated Goal Gates

Continuation tasks now put behavioral checks before the small roadmap contract smoke:

- `python-agent` and `agent-monorepo` tasks start with `python3 -m pytest tests -q` and require a
  local `tests/e2e` pytest journey check.
- Browser-facing experience plans add a `python3 -m engineering_orchestrator.browser_e2e ...` user
  experience gate. It uses local Playwright specs when installed and falls back to a static HTML
  route/form/role smoke that writes DOM evidence under `artifacts/browser-e2e/`.
- `node-frontend` tasks still use npm-oriented gates such as `npm test` and, when relevant,
  `npm run e2e`, leaving the generated implementation prompt to create or wire the local scripts.
- `cli-only` and `api-only` experience plans add deterministic documented-example or local command
  checks under `examples/`, `docs/examples/`, `tests/cli/`, `tests/api/`, or `tests/e2e/`.
- Every generated task also requires local journey evidence or an executable journey check tied to
  the selected `experience.e2e_journeys` entry, for example under `docs/e2e/`, `docs/evidence/`, or
  `tests/e2e/`.

The generated implementation prompt lists the exact acceptance and E2E commands plus candidate test,
example, and evidence paths. Coding agents should create those local artifacts as part of the task,
not replace the gates with external-account, paid-service, production-deployment, private-key,
mainnet, real-fund, or live-trading checks.

## Safety Boundary

The planner reuses the local goal-intake validator. It rejects non-local blueprint URLs and unsafe
requirements such as production deployment, mainnet writes, private key use, live trading, real-fund
movement, and paid live services.

The generated starter is intentionally conservative. Tighten the generated acceptance and e2e checks
with project-specific local tests as implementation details become clear, while preserving the
local-only safety boundary.

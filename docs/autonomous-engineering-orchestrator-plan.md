# Autonomous Engineering Orchestrator Development Plan

This plan defines the long-range direction for Engineering Orchestrator as a local-first control and
execution layer for roadmap-driven software work, coding agents, safety gates, project-specific
frontends, and end-to-end user validation.

## Product Positioning

Engineering Orchestrator should remain the control layer, not another CI system or another coding agent.
It should coordinate work across tools, keep policies explicit, select tasks, invoke executors,
capture evidence, and decide whether a task is complete.

The system should learn from mature platforms without copying their full weight:

- Dagger: local-first, repeatable, observable, typed execution units, containerized workflows.
- Temporal: durable long-running workflows, resumability, cancellation, retry state, human gates.
- Backstage: project catalog, templates, task history, software ownership, developer portals.
- Open Policy Agent: policy-as-code, structured policy input, decoupled decision and enforcement.
- GitHub Actions: repository-native workflow integration, commit status, PR checks and comments.
- OpenHands, SWE-agent, and mini-swe-agent: agent worker integration, trajectories, tool control.
- Pants and Bazel-style build systems: affected-task selection, caching, concurrency, toolchain reuse.

## Design Principles

- Local-first by default. Every workflow should be runnable from a developer machine before it is
  delegated to CI or hosted infrastructure.
- Control plane, not worker. The orchestrator owns task intent, policy, state, evidence, and completion
  rules. Shell commands, Dagger functions, CI jobs, and coding agents are replaceable executors.
- Project-specific experience. Each project should define the frontend or operator surface that
  matches its users, roles, and risk profile.
- User-path validation. Unit and integration tests are not enough for user-facing systems. Roadmap
  tasks should support E2E checks that simulate real users and real operator flows.
- Auditable autonomy. Agent-driven work must leave durable state, reports, policy decisions, git
  context, prompts, tool calls, and verification evidence.
- Explicit safety. Dangerous operations, secrets, live services, financial actions, mainnet writes,
  and production deployment must be guarded by structured policy and human approval gates.

## Target Architecture

Engineering Orchestrator should evolve into these modules:

1. Roadmap and task model
   - Milestones, tasks, continuation stages, self-iteration plans, dependencies, owners, risks,
     budgets, and acceptance gates.
   - Task phases: implementation, acceptance, repair, e2e, evidence collection, checkpoint.

2. Executor framework
   - Stable executor interface with adapters for shell, codex, Dagger, GitHub Actions, OpenHands,
     SWE-agent or mini-swe-agent, and future local workers.
   - Executor capability declarations such as file writes, network access, browser access, secrets,
     model usage, sandbox type, and max cost.

3. Policy engine
   - Structured policy decisions for commands, file scope, secrets, live operations, frontends,
     E2E environments, deployment, agent permissions, and git checkpoints.
   - Initial Python policy evaluator, with an optional OPA/Rego backend later.

4. Durable run engine
   - Per-phase state, retry history, cancellation, pause/resume, manual approval queue, no-progress
     detection, timeout accounting, and resumable long-running drives.
   - Temporal-style concepts without requiring Temporal in the first implementation.

5. Project catalog and templates
   - Workspace project discovery, profile detection, project metadata, owners, lifecycle state,
     standard template bundles, and bootstrap tasks.
   - Backstage-style catalog concepts with a lightweight local storage model.

6. Frontend experience module
   - Each project defines an experience spec describing target users, roles, UI surfaces, state
     views, workflows, authentication needs, and E2E journeys.
   - If a substantial project does not define an experience spec, the orchestrator should derive a
     default visualization plan from its profile, task shape, and project kind.
   - The orchestrator uses that spec to create frontend roadmap tasks, UI acceptance gates, and E2E tests.

7. E2E and user simulation
   - First-class `e2e` task phase after normal acceptance.
   - Browser automation, API journeys, CLI journeys, multi-role workflows, seed data, and realistic
     environment simulation.

8. Observability and reports
   - Machine-readable run manifests, Markdown reports, OpenTelemetry-compatible traces, command
     summaries, artifact indexes, and dashboard-ready status JSON.

9. GitHub and CI integration
   - Generate CI workflows, publish run summaries to PRs, map CI failures to roadmap tasks, and
     optionally delegate acceptance or E2E checks to repository-native runners.

10. Release and governance
    - Versioned roadmap schema, migration tooling, compatibility tests, golden fixtures, plugin
      compatibility checks, and release notes.

## Frontend Experience Module

The orchestrator should treat frontend design as project-specific engineering work, not a generic addon.
Every substantial project can define an `experience` block in its roadmap:

```json
{
  "experience": {
    "kind": "dashboard | submission-review | multi-role-app | api-only | cli-only",
    "personas": ["operator", "student", "reviewer", "admin"],
    "primary_surfaces": ["dashboard", "submission portal", "review console"],
    "auth": {
      "required": true,
      "roles": ["student", "reviewer", "admin"]
    },
    "e2e_journeys": [
      {
        "id": "student-submit-review-revise",
        "persona": "student",
        "goal": "Submit a paper, receive comments, upload revision, and view decision."
      }
    ]
  }
}
```

If a project has no explicit `experience` block, the orchestrator should still provide a default
visualization plan instead of leaving the frontend undefined. Defaults should be conservative:

- autonomous or research workers get an operator dashboard;
- submission and review systems get a submission/review workflow;
- multi-role systems get authenticated role-specific views;
- API-first projects get API docs, example journeys, and optional operational status pages;
- CLI-only projects get documented command journeys and optional report viewers.

Recommended frontend archetypes:

- Autonomous theorem prover or research worker
  - UI: operator dashboard.
  - Key screens: run queue, proof attempts, theorem status, resource use, errors, latest artifacts.
  - E2E: seed theorem, start run, observe progress, inspect proof or failure explanation.

- Student paper review and revision system
  - UI: submission and review workflow.
  - Key screens: student submission, reviewer comments, revision upload, status timeline, decision.
  - E2E: student submits draft, reviewer returns comments, student revises, reviewer accepts.

- Multi-role operational system
  - UI: authenticated app with role-specific routes.
  - Key screens: login, admin console, operator queue, reviewer/approver screens, audit log.
  - E2E: create users, exercise role boundaries, complete the cross-role workflow.

- API or CLI-first library
  - UI: generated docs, examples, status pages, notebooks, or lightweight dashboard if needed.
  - E2E: run documented examples exactly as a user would.

## Development Stages

### Stage 1: Schema and Validation Foundation

Goal: make the roadmap model expressive enough for mature autonomous engineering.

Tasks:

- Add roadmap schema validation for task phases, continuation stages, self-iteration, experience
  specs, executor capability requirements, and E2E journeys.
- Add golden roadmap fixtures for valid and invalid projects.
- Add schema version metadata and migration stubs.
- Document the supported roadmap fields and compatibility policy.

Acceptance:

- Invalid roadmap shapes fail validation with actionable errors.
- Valid roadmap examples for dashboard, submission-review, multi-role app, api-only, and cli-only projects pass.
- Existing projects continue to load without migration.

### Stage 2: Run Manifest and Evidence Model

Goal: record every task run as auditable machine-readable evidence.

Tasks:

- Write a JSON manifest for every task run.
- Include git refs, dirty-before and dirty-after paths, command metadata, executor metadata, policy
  decisions, artifact paths, timing, retries, stdout/stderr digests, and report links.
- Add manifest indexes for project dashboards and CI summaries.

Acceptance:

- Every task report has a matching manifest.
- Manifest content can reconstruct what ran and why the task passed, failed, or was blocked.

### Stage 3: Executor Plugin Framework

Goal: make executors replaceable while keeping orchestrator policy and evidence consistent.

Tasks:

- Define the executor interface and result contract.
  - Initial contract: [Executor Contract](executor-contract.md).
- Refactor shell and codex into executor adapters.
- Add executor capability checks before execution.
- Add Dagger adapter design and a minimal proof-of-concept adapter.
- Add OpenHands/SWE-agent adapter design without binding the core to either tool.

Acceptance:

- Existing shell and codex tasks behave the same through the plugin interface.
- Unknown executors fail during validation or preflight with clear messages.
- Executor results are normalized into the run manifest.

### Stage 4: Policy Engine V2

Goal: replace prefix-only safety checks with structured policy decisions.

Tasks:

- Define policy input schema for task, command, executor, project, git state, environment, and
  requested capabilities.
  - Initial schema: [Policy Engine V2 Schema](policy-engine-v2.md).
- Implement Python policy evaluator for command allowlists, blocked patterns, live gates, file
  scope, secret handling, E2E environment, and deployment gates.
- Add optional OPA-compatible policy export and evaluation hook.
- Add policy decision records to reports and manifests.

Acceptance:

- All current allowlist behavior is preserved.
- Policy decisions explain allow, deny, warn, and require-human-approval outcomes.
- File-scope and dirty-worktree decisions are represented as policy decisions.

### Stage 5: Durable Drive Engine

Goal: make long autonomous runs resumable and inspectable.

Tasks:

- Persist phase-level state before and after each implementation, acceptance, repair, and E2E phase.
- Add `pause`, `resume`, `cancel`, and `approve` operations.
- Add retry backoff and no-progress state.
- Add manual approval records for live actions, high-risk agent work, and deployment.

Acceptance:

- Interrupted drives can resume without losing task context.
- Manual gates are visible in status and reports.
- Re-running a drive does not duplicate completed phases unless explicitly requested.

Current durable phase state:

- Each task stores an ordered `phase_history` in `.engineering/state/harness-state.json`.
- Every phase transition is recorded as a `before` and `after` event with a sequence number,
  task attempt, timestamp, phase name, status, message, and compact metadata.
- Recorded phases include implementation, numbered acceptance attempts, numbered repair attempts,
  E2E, file-scope guard, manifest writing, checkpoint intent, and final result.
- `current_phase` remains populated after a `before` event until the matching `after` event is
  written, so interrupted drives can show which phase was active when work stopped.
- `phase_states` keeps the latest event per phase for quick inspection, while manifests and reports
  remain the public evidence artifacts for completed attempts.

Current durable drive controls:

- Drive-level pause, resume, and cancel state is stored in the `drive_control` block in
  `.engineering/state/harness-state.json`.
- Active drives also record their owner pid, started time, latest heartbeat, current loop activity,
  current task, last progress message, and watchdog status. Stale state is reported when the pid is
  gone or the heartbeat exceeds the local `drive_watchdog.stale_after_seconds` threshold.
- Rolling drives with task checkpointing record roadmap materialization checkpoint intent before
  generated tasks run. Clean or orchestrator-owned roadmap/materialization dirtiness is checkpointed first;
  pre-existing unrelated user dirtiness blocks materialization before roadmap mutation and is recorded
  as a deferred checkpoint boundary in the drive report.
- Drive reports include a deterministic goal-gap retrospective and JSON sidecar that compare final
  local evidence with the long-run unattended reliability goal.
- `pause` and `cancel` stop future drive scheduling without deleting roadmap tasks or reports.
- `resume` clears pause, cancel, or stale state and lets a later `drive` invocation continue selecting
  work without killing unrelated processes.
- Manual, live, and agent policy gates create records in `approval_queue`; approved records unblock
  the affected task and are marked `consumed` after the task completes.
- Operator commands and examples are documented in [Durable Drive Controls](durable-drive-controls.md).

### Stage 6: Frontend Experience Planning

Goal: make every substantial generated project define the right frontend for its users.

Tasks:

- Add `experience` schema and validation.
- Add default frontend visualization plans for projects without explicit experience specs.
- Add profile defaults for dashboard-only, submission-review, multi-role app, API-only, and CLI-only.
- Add commands to summarize the required frontend and E2E journeys.
- Add roadmap generators that create frontend tasks from the experience spec.

Acceptance:

- Projects can declare their user roles and UI surfaces.
- `engo status` exposes frontend readiness and missing E2E journeys.
- Frontend-related tasks are generated with file scope and acceptance commands.

Current helper:

- `engo frontend-tasks` proposes a `frontend-visualization` milestone from the explicit
  `experience` block or the derived default plan.
- `engo frontend-tasks --materialize` appends that milestone to the roadmap and records a decision
  log event.
- Generated tasks remain framework-neutral. Dashboard, submission-review, and multi-role projects
  may use browser E2E checks if the project already has them; API-only and CLI-only projects can use
  documented examples, API tests, CLI tests, or local scripts instead.

### Stage 7: Frontend Templates and UI Workflows

Goal: provide practical frontend starting points without forcing every project into one stack.

Tasks:

- Add template packs for dashboard, submission-review, and multi-role app projects.
- Keep templates framework-neutral at the orchestrator level, with adapters for common stacks.
- Add generated UI acceptance criteria such as role-based routes, empty states, loading states,
  error states, audit trails, and responsive layout checks.
- Add documentation for tailoring the frontend to the project domain.

Acceptance:

- A project can bootstrap a domain-appropriate frontend plan and task set.
- Generated tasks include realistic UI states and role flows.
- The orchestrator does not assume every project needs the same UI.

### Stage 8: E2E and Real User Simulation

Goal: make user experience validation a standard part of project completion.

Tasks:

- Support browser-based E2E commands, API journey tests, CLI user journeys, and multi-role flows.
- Add fixture and seed-data conventions.
- Add screenshots, traces, videos, and accessibility reports as artifacts where available.
- Add E2E failure summaries that point back to roadmap tasks.

Acceptance:

- E2E runs after acceptance and can fail a task.
- Reports include user journey names and artifact links.
- Example projects cover dashboard, submission-review, and multi-role flows.

### Stage 9: Observability and Dashboard

Goal: make orchestrator activity visible without reading raw logs.

Tasks:

- Add a local dashboard or static report viewer for projects, tasks, runs, policies, E2E artifacts,
  and continuation progress.
- Add JSON endpoints or generated static data for dashboard consumption.
- Add optional OpenTelemetry export.

Acceptance:

- A user can inspect project status, running tasks, blocked gates, latest reports, and E2E evidence
  from a visual surface.
- Dashboard data comes from manifests and state, not duplicated logic.

### Stage 10: GitHub and CI Integration

Goal: connect local orchestrator runs to repository-native workflows.

Tasks:

- Generate GitHub Actions workflows for validation, acceptance, E2E, and report publishing.
- Add PR comments with task status and report links.
- Map failed CI checks back to roadmap tasks.
- Allow selected tasks to delegate acceptance or E2E to CI.

Acceptance:

- A repository can opt into generated CI with one command.
- PR feedback links back to orchestrator tasks and evidence.

### Stage 11: Intelligent Task Selection and Caching

Goal: improve speed and relevance as projects grow.

Tasks:

- Add changed-file and file-scope based task selection.
- Add dependency-aware affected checks inspired by Pants and Bazel.
- Cache safe acceptance results by inputs where possible.
- Add concurrency for independent tasks.

Acceptance:

- The orchestrator can explain why a task was selected or skipped.
- Independent checks can run concurrently without corrupting state.

### Stage 12: Production Hardening

Goal: make the orchestrator dependable enough for long-lived real projects.

Tasks:

- Add schema migrations, compatibility tests, plugin contract tests, release notes, and changelog.
- Add security review for command execution, secret redaction, report persistence, and agent prompts.
- Add documentation for operating autonomous drives safely.

Acceptance:

- Releases are versioned and migration-safe.
- Security-sensitive behavior has tests and documented guarantees.

## Roadmap Operating Model

- Use `engo plan-goal` to create the first local starter roadmap from a high-level project goal.
- Use explicit milestones for work that is already committed to the current repository.
- Use `continuation.stages` for future planned work that can be materialized by `engo advance` or
  `engo drive --rolling`.
- Use `self_iteration` only when the repository has enough policy and test coverage to let a planner
  append the next stage safely.
- Every implementation task should define file scope, acceptance commands, and E2E commands when a
  user-visible experience is affected.
- Every agent task should define allowed file scope, timeout, model expectations, and repair path.

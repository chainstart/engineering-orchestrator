# Engineering Orchestrator

Engineering Orchestrator is a production-oriented engineering mothership for autonomous software development. It organizes goals, specifications, roadmaps, state, policies, executors, tests, reports, commits, and recovery into one durable workflow so AI coding capability can keep moving a real engineering target forward over long-running, auditable loops.

It is not a personal script for one local workspace, and it is not a toy demo generator. Its purpose is to become a general software engineering agent control plane: a user provides a goal, specification, or product direction; the orchestrator turns that intent into a roadmap and tasks; replaceable executors implement the work; acceptance and end-to-end checks decide whether the work is complete; state and evidence are recorded; failures are repaired or paused; successful tasks can be committed and pushed; unattended drives continue into the next stage.

The target domain is intentionally open ended. Engineering Orchestrator should be able to drive websites, apps, games, agents, backend services, CLIs, data and research systems, embedded software, Verilog/HDL, EDA flows, formal verification, protocol engineering, CI/CD, and operational automation. Its real limits are current model capability, target-project toolchain maturity, and whether the project can provide reliable validation and safety feedback.

## Compatibility And Terminology

Engineering Orchestrator is the canonical product name. The canonical slug for new public packaging and documentation is `engineering-orchestrator`, and the Chinese product name is `工程编排器`.

Engineering Harness is a legacy compatibility name. Existing downstream users can keep using the Python import path `engineering_harness`, the legacy CLI entry point `engh`, the existing `.engineering/state/harness-state.json` state path, and historical `engineering-harness.*` report/schema kind strings during the migration window.

Use `engo` for new command examples. `engh` remains supported as a legacy compatibility command and should behave the same. Agent harness is reserved for an agent-internal LLM/tool/runtime loop component, not this external roadmap-driven engineering control plane.

The staged migration path is: update public docs and help first, keep all compatibility contracts working, introduce forward aliases, then consider deeper package/source/repository renames only after downstream users have a migration window.

## Core Positioning

Engineering Orchestrator is not another coding model. It is the production workflow around coding models and engineering tools.

- **Goal driven**: work is anchored to user requirements, specifications, blueprints, and roadmaps, not one-off prompts.
- **Roadmap driven**: project progress is modeled through `.engineering/roadmap.yaml`, task phases, acceptance commands, and continuation stages.
- **Unattended by design**: `drive`, `self-iterate`, and `daemon-supervisor` support long-running execution, budgets, idle backoff, and resumability.
- **Executor neutral**: shell commands, Codex, model APIs, CI runners, Dagger, OpenHands, SWE-agent, hardware toolchains, and future workers should all be replaceable executors.
- **Production loop**: implementation, tests, repair, E2E validation, reports, policy decisions, git checkpoints, pushes, and CI/CD belong in one feedback loop.
- **State and memory first**: long-running work needs state files, decision logs, reports, manifests, approval records, and failure isolation instead of relying on a single chat context.
- **Safety and governance built in**: file scope, command policy, secret redaction, network and deployment capabilities, human approvals, and audit evidence are part of execution.

## What It Is Not

Engineering Orchestrator should not be understood as:

- a personal script for projects under one local directory;
- a scaffold for web demos or toy projects;
- a replacement for a single AI coding assistant;
- only a CI system, task queue, or project management tool;
- a vertical tool only for Python, frontends, blockchain, or research projects.

It is the engineering control layer above those systems: it coordinates models, repositories, tests, CI, deployment, knowledge, artifacts, and human approval into a durable autonomous development workflow.

## Current Capabilities

The current version already provides the foundation:

- Initialize project-local `.engineering/` control directories and built-in profiles.
- Generate starter roadmaps from high-level goals.
- Execute roadmap task phases: `implementation`, `acceptance`, `repair`, and `e2e`.
- Invoke shell commands and a gated Codex executor.
- Maintain durable state, phase history, decision logs, Markdown reports, and JSON manifests.
- Support rolling continuation, self-iteration planners, pause/resume/cancel, and approval gates.
- Enforce command allowlists, live/manual/agent gates, file scope guards, unsafe capability audits, and secret redaction.
- Create git commit/push checkpoints after successful tasks.
- Generate or inspect project experience plans and E2E-oriented tasks for UI, API, and CLI workflows.
- Dispatch across workspaces and run daemon supervisors that rotate among projects.

These capabilities are the skeleton of the engineering mothership. The next step is to make it operate more like a long-lived autonomous engineering organization, not just a local CLI.

## Mental Model

Each target project owns an engineering control directory:

```text
.engineering/
  roadmap.yaml
  policies/
    command-allowlist.yaml
    deployment-policy.yaml
    secret-policy.yaml
  state/
    harness-state.json
    decision-log.jsonl
  reports/
```

The core loop is:

```text
goal/spec/blueprint
  -> roadmap
  -> next task
  -> implementation executor
  -> acceptance checks
  -> repair loop
  -> e2e/user/hardware validation
  -> report + manifest + state
  -> git checkpoint / push / CI
  -> continuation or self-iteration
```

A task is the smallest autonomous work unit. It usually defines:

- `file_scope`: the paths an executor is allowed to modify.
- `implementation`: commands or agent steps that perform the work.
- `acceptance`: tests or checks that decide whether the task is complete.
- `repair`: follow-up commands or agent steps after failed acceptance.
- `e2e`: user journeys, system journeys, hardware simulation, or integration validation.
- `max_task_iterations`: the cap for implementation, acceptance, and repair loops.

## Relation To Devin

Devin publicly positions itself as an AI software engineer that can write, run, and test code, and can enter team workflows through Web, IDE, Shell, Browser, API, Slack/Teams, GitHub/GitLab/Bitbucket, Linear/Jira, scheduled sessions, playbooks, and session insights. See the official Devin docs for [Introducing Devin](https://docs.devin.ai/get-started/devin-intro), [Scheduled Sessions](https://docs.devin.ai/product-guides/scheduled-sessions), and [Session Insights](https://docs.devin.ai/product-guides/session-insights).

Engineering Orchestrator can learn from that direction, but the product boundary is different:

- Devin is closer to a hosted AI engineer product; Engineering Orchestrator is closer to an open, local-first, executor-neutral control plane.
- Devin focuses on delegating work to an AI engineer; Engineering Orchestrator focuses on institutionalizing the long-running software development workflow that can drive many executors through a roadmap.
- Devin emphasizes interactive takeover, team integrations, and hosted product experience; Engineering Orchestrator should emphasize roadmap schema, durable state machines, policy, evidence, resumability, executor plugins, and auditable autonomy.
- Devin strengths such as task delegation, parallel backlog work, scheduled sessions, knowledge/playbooks, session insights, and ready-made integrations should become open modules in Engineering Orchestrator rather than platform-bound features.

Useful ideas to borrow:

- **Session insights**: generate health, failure cause, context quality, token/cost, retry, and improvement summaries for each drive or session.
- **Playbooks and skills**: package repeatable workflows such as fixing CI, upgrading dependencies, implementing API endpoints, or closing a Verilog simulation loop.
- **Scheduled sessions**: evolve `daemon-supervisor` into a formal scheduler with cron, queues, priorities, notifications, and historical audit.
- **Knowledge onboarding**: build a project knowledge index from README files, specs, ADRs, issues, PRs, CI logs, and design docs.
- **Team integrations**: connect to GitHub/GitLab, Jira/Linear, Slack/Teams, CI, artifact registries, and deployment platforms.
- **Takeover experience**: provide a local dashboard or web UI where an operator can inspect, pause, approve, take over, and resume tasks.

## Development Direction

To become a real software engineering agent, Engineering Orchestrator should prioritize these layers:

1. **Roadmap schema and migrations**
   - Define stable schemas for goals, specs, tasks, dependencies, risks, budgets, acceptance, E2E, hardware simulation, deployment, and approvals.
   - Provide schema versions, migration tools, golden fixtures, and compatibility tests.

2. **Executor plugin system**
   - Model shell, Codex, OpenAI API, Dagger, GitHub Actions, OpenHands, SWE-agent, Verilator, Vivado, PlatformIO, and future tools as executors.
   - Require each executor to declare capabilities, cost, isolation level, network/file/secret needs, and result contracts.

3. **Model and memory layer**
   - Support multi-model routing, cost budgets, context compression, project knowledge indexes, long-term memory, and task-level prompt templates.
   - Store memory as auditable project assets instead of leaving it inside transient model sessions.

4. **Durable autonomous runtime**
   - Strengthen daemon operation, leases, heartbeats, stale recovery, retry backoff, concurrent scheduling, and cross-project queues.
   - Support real 24/7 operation: pausable, resumable, upgradeable, auditable, and budget-limited.

5. **Evaluation and production acceptance**
   - Go beyond unit tests with browser E2E, API journeys, CLI journeys, load tests, security scans, HDL simulation, formal verification, hardware-in-the-loop checks, and deployment smoke tests.
   - Let evidence define completion instead of relying on model self-assessment.

6. **CI/CD and release automation**
   - Generate or maintain CI workflows.
   - Map local manifests to PR comments, commit statuses, artifacts, and release notes.
   - Feed failed CI checks back into roadmap tasks.

7. **Security and governance**
   - Continue strengthening unsafe capability classification, secret redaction, sandbox policy, deployment gates, approval queues, and audit logs.
   - Deny or require human approval by default for production environments, funds, private keys, customer data, hardware flashing, and other high-risk operations.

8. **Operator UI**
   - Provide a local or hosted dashboard for goals, roadmaps, running tasks, failure isolation, approvals, reports, E2E evidence, and resource consumption.
   - The UI should serve engineering operations, not marketing.

9. **Domain packs**
   - Provide profiles, acceptance templates, toolchain detection, and playbooks for Web, App, Game, Agent, Embedded, Verilog, Formal, Data, and DevOps domains.
   - Keep the orchestrator general and add domain-specific power through profiles, executors, and playbooks.

## Installation

Install from the repository root in editable mode:

```bash
python3 -m pip install -e .
```

Then use the CLI:

```bash
engo --help
```

You can also run without installing:

```bash
PYTHONPATH=src python3 -m engineering_harness.cli --help
```

## Built-In Profiles

List built-in profiles:

```bash
engo profiles
```

Current profiles:

- `agent-monorepo`
- `evm-protocol`
- `evm-security-research`
- `lean-formalization`
- `node-frontend`
- `python-agent`
- `trading-research`

These profiles are starting points, not domain limits. Embedded, Verilog, games, mobile, data platforms, and other domains should be added through domain packs.

## Initialize A Project

Initialize the engineering control directory for any target project:

```bash
engo init \
  --project-root /path/to/project \
  --profile python-agent \
  --name my-project
```

Validate the roadmap:

```bash
engo validate --project-root /path/to/project
```

Inspect status:

```bash
engo status --project-root /path/to/project
engo status --project-root /path/to/project --json
```

## Create A Roadmap From A Goal

Generate a starter roadmap from a goal:

```bash
engo plan-goal \
  --project-root /path/to/project \
  --name my-project \
  --profile python-agent \
  --goal "Build a production-grade autonomous research agent with durable task state and an operator dashboard."
```

Write the roadmap to `.engineering/roadmap.yaml`:

```bash
engo plan-goal \
  --project-root /path/to/project \
  --name my-project \
  --profile python-agent \
  --goal-file docs/goal.md \
  --blueprint docs/spec.md \
  --materialize
```

If a roadmap already exists and you intentionally want to replace it, add `--force`.

## Run One Task

Show the next task:

```bash
engo next --project-root /path/to/project
```

Run a dry run without executing real commands:

```bash
engo run --project-root /path/to/project --dry-run
```

Execute the next task:

```bash
engo run --project-root /path/to/project
```

If the task needs a coding agent, explicitly allow it:

```bash
engo run --project-root /path/to/project --allow-agent
```

## Autonomous Drive

Run pending tasks until completion, failure, blocking, or budget exhaustion:

```bash
engo drive \
  --project-root /path/to/project \
  --max-tasks 5 \
  --time-budget-seconds 14400
```

Allow rolling continuation:

```bash
engo drive \
  --project-root /path/to/project \
  --rolling \
  --time-budget-seconds 14400
```

Allow a self-iteration planner to append the next stage after the roadmap queue is exhausted:

```bash
engo drive \
  --project-root /path/to/project \
  --rolling \
  --self-iterate \
  --allow-agent \
  --time-budget-seconds 14400
```

Create a git checkpoint after each passed task:

```bash
engo drive \
  --project-root /path/to/project \
  --rolling \
  --allow-agent \
  --commit-after-task
```

Commit and push:

```bash
engo drive \
  --project-root /path/to/project \
  --rolling \
  --allow-agent \
  --commit-after-task \
  --push-after-task
```

## 24/7 Supervisor

`daemon-supervisor` rotates across projects in a workspace for long-running operation:

```bash
engo daemon-supervisor \
  --workspace /path/to/workspace \
  --rolling \
  --self-iterate \
  --allow-agent \
  --run-window-seconds 86400 \
  --sleep-seconds 30 \
  --scheduler-policy fair
```

It repeatedly runs `workspace-drive` ticks, selects projects by budget and scheduler policy, records runtime state, and backs off when idle or repeatedly nonproductive.

## Pause, Resume, Cancel, Approve

Pause future scheduling:

```bash
engo pause --project-root /path/to/project --reason "operator review"
```

Resume:

```bash
engo resume --project-root /path/to/project --reason "review complete"
```

Cancel future scheduling until resumed:

```bash
engo cancel --project-root /path/to/project --reason "stop this run"
```

List approval gates:

```bash
engo approvals --project-root /path/to/project
```

Approve all pending gates:

```bash
engo approve --project-root /path/to/project --all --reason "approved by operator"
```

## Frontend, API, CLI, And E2E Experience

Production software must define how users or operators validate that it works. The orchestrator uses an `experience` block to describe the target experience:

```json
{
  "experience": {
    "kind": "dashboard",
    "personas": ["operator"],
    "primary_surfaces": ["run queue", "artifact viewer", "failure dashboard"],
    "auth": {
      "required": false,
      "roles": []
    },
    "e2e_journeys": [
      {
        "id": "operator-inspects-run",
        "persona": "operator",
        "goal": "Inspect a completed autonomous run and review its artifacts."
      }
    ]
  }
}
```

Generate experience-related tasks:

```bash
engo frontend-tasks --project-root /path/to/project
engo frontend-tasks --project-root /path/to/project --materialize
```

Here "frontend" does not only mean a web UI. It can also mean an API journey, CLI journey, hardware simulation report, EDA waveform artifact, operator dashboard, or any surface a real user or engineer uses to judge system completeness.

## Roadmap Continuation

When explicit tasks are exhausted, the orchestrator can materialize the next continuation stages:

```json
{
  "continuation": {
    "enabled": true,
    "goal": "Ship the full production system described by the spec.",
    "blueprint": "docs/spec.md",
    "stages": [
      {
        "id": "production-hardening",
        "title": "Production hardening",
        "objective": "Add reliability, observability, and release checks.",
        "tasks": [
          {
            "id": "production-hardening-tests",
            "title": "Add production readiness checks",
            "file_scope": ["src/**", "tests/**", "docs/**"],
            "acceptance": [
              {
                "name": "focused tests",
                "command": "python3 -m pytest tests/test_production_readiness.py -q"
              }
            ]
          }
        ]
      }
    ]
  }
}
```

Advance manually:

```bash
engo advance --project-root /path/to/project
```

Advance automatically during a drive:

```bash
engo drive --project-root /path/to/project --rolling
```

## Spec Backlog

For spec-driven projects, the orchestrator can turn Markdown `Stage` / `Tasks` sections into
continuation stages with a dedicated planning command. By default `plan-spec` reads
`spec.development_plan` from `.engineering/roadmap.yaml`; pass `--spec` to read an explicit local
document. Use `--from-stage` when earlier stages are already implemented:

Roadmaps can also declare the canonical project specification:

```json
{
  "spec": {
    "path": "docs/engineering-harness-system-spec.md",
    "kind": "markdown",
    "requirements_index": "docs/spec-index.json"
  }
}
```

`requirements_index` is optional. When it is configured, it must be a local JSON/YAML mapping or
inline list/mapping that exposes requirement ids such as `EH-SPEC-001`; nested groups and
requirement-id mapping keys are also indexed. Markdown `spec.path` documents with requirement
headings are indexed too. Roadmap validation reports task or command `spec_refs` that point to
unknown ids. `engo status --json` includes compact spec coverage at top-level `spec` and under
`runtime_dashboard.spec`; plain `engo status` prints the same coverage as a compact one-line
summary.

```bash
engo plan-spec --project-root /path/to/project --from-stage 2
engo plan-spec --project-root /path/to/project --from-stage 2 --materialize
```

Additional sources can be passed explicitly:

```bash
engo plan-spec \
  --project-root /path/to/project \
  --spec docs/spec-driven-development-plan.md \
  --spec docs/autonomous-engineering-harness-plan.md \
  --materialize
```

Generated tasks include source metadata, `spec_refs` when the source stage declares requirement
ids, Codex implementation and repair commands, local pytest and validation gates, and status JSON
E2E evidence. Re-running the command skips stages that are already present or already covered by
the same spec refs and task semantics. When only some source tasks are already covered, the planner
keeps the remaining tasks and reports the skipped coverage in `skipped_task_count` and
`skipped_tasks`. `engo spec-backlog` remains available as a compatibility alias and accepts the
same inputs.

When generated continuation stages are advanced into active milestones, the orchestrator preserves stage
source metadata plus task and command `spec_refs`, so manifests and reports still trace back to the
source specification. Self-iteration assessments also summarize the requirement refs advanced by
newly appended stages.

## Task Example

```json
{
  "id": "worker-runtime-loop",
  "title": "Implement durable worker runtime loop",
  "max_task_iterations": 3,
  "file_scope": ["src/**", "tests/**", "docs/**"],
  "implementation": [
    {
      "name": "Codex implementation",
      "executor": "codex",
      "prompt": "Implement the durable worker loop described by this task.",
      "timeout_seconds": 3600
    }
  ],
  "acceptance": [
    {
      "name": "focused tests",
      "command": "python3 -m pytest tests/test_worker_runtime.py -q"
    }
  ],
  "repair": [
    {
      "name": "Codex repair",
      "executor": "codex",
      "prompt": "Fix the failing acceptance checks for this task.",
      "timeout_seconds": 1800
    }
  ],
  "e2e": [
    {
      "name": "operator journey",
      "command": "python3 -m pytest tests/e2e/test_operator_journey.py -q"
    }
  ]
}
```

## Safety Model

Default principle: production autonomy must be able to stop, explain itself, and leave evidence.

- Coding agent executors are gated by default and require `--allow-agent`.
- Live operations, deployments, mainnet actions, funds, private keys, external writes, and high-risk deletes must go through policy and human approval.
- Command execution records policy decisions, capability classification, and safety audits.
- Sensitive values are redacted from reports, manifests, and state.
- File scope guards prevent task execution from modifying unrelated paths.
- Checkpoint readiness distinguishes clean worktrees, orchestrator-generated changes, and unrelated user changes.

Safety policy should not block production development. It should make risk explicit: what can run automatically, what needs human approval, and what should never be executed by an unattended agent.

## Documentation

More design documents:

- [Autonomous Engineering Orchestrator Development Plan](docs/autonomous-engineering-harness-plan.md)
- [Engineering Orchestrator System Specification](docs/engineering-harness-system-spec.md)
- [Spec-Driven Development Plan](docs/spec-driven-development-plan.md)
- [Durable Drive Controls](docs/durable-drive-controls.md)
- [Executor Contract](docs/executor-contract.md)
- [Goal Intake Contract](docs/goal-intake-contract.md)
- [Goal Roadmap Planner](docs/goal-roadmap-planner.md)
- [Policy Engine V2](docs/policy-engine-v2.md)
- [Browser User Experience E2E](docs/browser-user-experience-e2e.md)
- [Workspace Drive Dispatcher](docs/workspace-drive-dispatcher.md)

## License

Engineering Orchestrator is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).

# Engineering Orchestrator Self-Rename Spec

Date: 2026-06-01

Status: stage 2 local migration implemented

Requirement id: `EO-SPEC-001`

## 1. Problem

The current project is named `engineering-harness`, but its implemented responsibility is broader than an LLM harness. It is a roadmap-driven engineering control plane that schedules tasks, creates auditable runs, invokes executors, applies policy, records evidence, and coordinates long-running software development work.

In current agent terminology, `harness` is better reserved for the inner runtime that wraps an LLM with tools, memory, policy, and loop controls. The legacy compatibility name `Engineering Harness` creates terminology conflict with future agent runtime design when treated as canonical.

## 2. Target Name

Canonical product name:

```text
Engineering Orchestrator
```

Canonical repository / package-facing slug:

```text
engineering-orchestrator
```

Chinese product name:

```text
工程编排器
```

## 3. Compatibility Policy

The rename must be staged. The first implementation must not break existing downstream usage.

Required compatibility:

- Keep the Python package import path `engineering_harness` working during the migration window.
- Keep `bin/engh` working during the migration window.
- Add new user-facing naming in docs and CLI help where practical.
- Optional but recommended: add a new `bin/engo` compatibility-forward CLI wrapper.
- Do not rename the GitHub repository as part of this code task.
- Do not delete old state directories, report schemas, or manifest kind strings unless a migration exists.

Terminology rule:

- `Engineering Orchestrator` means the external engineering task control plane.
- `Agent harness` means an agent-internal LLM/tool/runtime loop component.
- `Engineering Harness` may appear only as a legacy compatibility name.

## 4. EO-001 Scope

Task `EO-001-self-rename-engineering-orchestrator` should perform the first self-hosted rename pass:

- Update README and core docs to explain the canonical name and legacy compatibility.
- Update CLI description/help strings where this does not break tests.
- Add a forward-compatible command alias if low risk.
- Update tests that assert display strings.
- Keep old imports, config paths, report schema versions, and existing task manifests compatible.
- Add documentation of the staged migration path.

Out of scope for EO-001:

- Renaming the repository on GitHub.
- Renaming the Python source package from `engineering_harness` to `engineering_orchestrator`.
- Migrating all historical report kind strings.
- Removing `engh` or any old command.

## 5. Acceptance

The self-hosted task is complete when:

- The project presents `Engineering Orchestrator` as the canonical name.
- Legacy `Engineering Harness` wording is clearly marked as compatibility wording.
- Existing CLI entry points still work.
- The unit test suite passes.
- Roadmap validation passes.
- Search results show the new canonical name in README/docs/src/tests while allowing compatibility mentions.

## 6. Stage 2 Local Migration

Stage 2 moves the local implementation package and packaged CLI entry points to the canonical source name while preserving legacy compatibility:

- `src/engineering_orchestrator/` is the canonical implementation package.
- `src/engineering_harness/` remains as a thin compatibility wrapper package.
- `engo` and `engh` both resolve to `engineering_orchestrator.cli:main`.
- New documentation examples should use `engineering_orchestrator`.
- Existing `engineering_harness` imports, `engh`, `.engineering/state/harness-state.json`, and historical `engineering-harness.*` kind strings remain valid during the compatibility window.

Still out of scope:

- Renaming the GitHub repository and remote URL.
- Migrating old state paths or historical manifest kind strings.
- Removing `engh` or compatibility import wrappers.

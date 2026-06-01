# Durable Drive Controls

Engineering Orchestrator stores drive controls and approval gates in the local project state file:

```text
.engineering/state/harness-state.json
```

The controls are local-first. A pause or cancel request prevents the next drive task from starting.
It does not terminate an operating-system process that is already running; the current task report
and phase state remain the durable evidence for what happened.

For multi-project unattended operation, use the local workspace dispatcher instead of starting many
project drives at once. See [Workspace Drive Dispatcher](workspace-drive-dispatcher.md) for
`engo workspace-drive`, deterministic queue ordering, skip evidence, and workspace dispatch reports.

## Heartbeat And Watchdog

While `drive` is running, the `drive_control` block records the owning process id, drive start time,
last heartbeat, current activity, current task when known, and last progress message. Inspect it with:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The JSON includes `drive_control.watchdog`. A healthy active drive reports `status: running` and
`stale: false`. A stale drive reports `status: stale` when the recorded process is gone, the recorded
pid is missing, or the last heartbeat is older than the local threshold.

The default stale-heartbeat threshold is one hour. Configure it locally in the roadmap:

```json
{
  "drive_watchdog": {
    "stale_after_seconds": 7200
  }
}
```

For one shell session, override it with:

```bash
ENGINEERING_HARNESS_DRIVE_STALE_AFTER_SECONDS=7200 python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The watchdog only probes the recorded pid with a non-destructive local liveness check. It never kills
or signals unrelated work beyond that liveness probe.

Before a new `drive` starts selecting work, the orchestrator runs a local stale-running recovery preflight
against the durable `drive_control` block. Automatic recovery only happens when all of these are true:

- `drive_control.status` is `running`;
- the last heartbeat is older than the configured watchdog threshold or missing;
- the recorded pid is missing or no longer running.

In that case the orchestrator transitions `drive_control` back to `idle`, appends a
`stale-running-recovery` history event, writes `stale_running_recovery` evidence with the previous
pid, heartbeat age, threshold, reason, `recovered_at`, and recommended follow-up, then continues with
normal task selection. If the recorded pid is alive, or if the heartbeat is still fresh, the preflight
does not mutate state and the new drive is blocked with `stale_running_block` / `stale_running_preflight`
evidence.

## Runtime Dashboard Payload

`status --json` also includes `runtime_dashboard`, a dashboard-ready summary for long unattended
runs. It is generated from local state, task manifests, drive reports, and workspace-dispatch reports;
it does not start a server or require external services.

```bash
bin/engo status --project-root /path/to/project --json
```

Inspect these fields first:

- `runtime_dashboard.drive_watchdog`: active drive pid, heartbeat age, stale reason, and current
  liveness verdict.
- `runtime_dashboard.drive_control.stale_running_recovery` and `stale_running_block`: the latest
  automatic recovery evidence or the current reason recovery is unsafe.
- `runtime_dashboard.current_task` and `current_phase`: the active task/phase when a drive is
  running, otherwise the next roadmap task when one is selectable.
- `runtime_dashboard.executor_no_progress`: configured no-progress thresholds, current executor
  watchdog evidence, and the latest unresolved no-progress failure.
- `runtime_dashboard.executor_diagnostics`: local readiness for registered executors, including
  OpenHands/Dagger enablement, binary discovery, and unsafe capability warnings.
- `runtime_dashboard.approval_leases`: pending, approved, consumed, and stale approval lease counts
  plus compact pending approval records.
- `runtime_dashboard.failure_isolation`: unresolved isolated task failures and their local recovery
  actions.
- `runtime_dashboard.replay_guard`: restart-safe phase reuse evidence for interrupted task drives,
  including which implementation or acceptance phases were reused and the matching command-group
  fingerprints.
- `runtime_dashboard.self_iteration.latest_assessment`: the latest self-iteration planner or
  checkpoint-gate result, including compact checkpoint readiness evidence when a gate blocked.
- `runtime_dashboard.workspace_dispatch`: nearest workspace dispatch queue, latest dispatch report,
  and active or latest lease status.
- `runtime_dashboard.daemon_supervisor_runtime`: nearest durable daemon supervisor run window,
  restartable-loop metadata, tick decisions, latest stop reason, and supervisor report sidecar.
- `runtime_dashboard.latest_reports`: latest task, drive, and workspace dispatch report metadata with
  JSON sidecar paths.
- `runtime_dashboard.goal_gap_scorecard`: bounded deterministic scorecard for unattended-reliability
  categories such as stuck detection, stale-running recovery, checkpoint boundaries, failure
  isolation, duplicate-plan guard, approval/capability safety, workspace dispatch, and E2E evidence.
- `runtime_dashboard.goal_gap.next_actions`: deterministic next actions from the latest drive
  goal-gap retrospective, or a current-status fallback when no drive report exists yet.

The rest of the status payload remains machine-readable and stable for scripts. Operators can still
open the referenced Markdown report when they need full stdout/stderr context, but the dashboard block
is intended to answer the first triage questions without reading raw reports.

`status --json` also exposes the same scorecard at top-level `goal_gap_scorecard`. Each category is
ordered by orchestrator priority and includes:

- `status`: `complete`, `partial`, `missing`, or `blocked`.
- `risk_score`: integer `0` to `100`; higher values should be handled earlier.
- `severity`: integer `0` to `4`, derived from the risk score for simple sorting.
- `evidence_paths`: bounded local JSON/report/status paths or payload keys used for the decision.
- `rationale` and `recommended_next_stage_themes`: compact planner/operator guidance.

Interpret `blocked` as an immediate local blocker, `missing` as absent local evidence, and `partial`
as evidence that exists but is incomplete or still carries risk. The scorecard is local-only and uses
existing status, manifest, approval, failure, watchdog, checkpoint, dispatch, test/source, git, and
latest goal-gap retrospective evidence.

During a live drive, a fresh heartbeat is treated as protected in-progress work even if the recorded
pid cannot currently be observed. In that case the stale-running category reports an `in_progress`
rationale and does not recommend `recover-stale-running-drive`. That recovery theme is reserved for
the deterministic stale case where the heartbeat is stale and the owner pid is dead or missing.

Checkpoint categories make the same distinction between protected work and blockers. Dirty
`safe_to_checkpoint_paths` from roadmap materialization, the active drive task's file scope, or the
next task's file scope are reported as `checkpoint_pending` or `in_progress` rationale. The
`close-git-boundary` recommendation appears only when `blocking_paths` is non-empty.

## Rolling Checkpoint Boundaries

When `drive --rolling` materializes continuation stages and `--commit-after-task` or
`--push-after-task` is enabled, the orchestrator treats roadmap materialization as its own checkpoint
boundary before generated tasks run.

If the git worktree is clean or only roadmap/materialization paths are dirty immediately before
materialization, the orchestrator commits the roadmap materialization first. This covers rolling
self-iteration, where the planner may have already appended an unmaterialized continuation stage to
`.engineering/roadmap.yaml`. Generated task checkpoints then run against a clean boundary and do not
mistake orchestrator-owned accumulated roadmap edits for pre-existing user dirtiness.

If unrelated user changes are present before materialization, the orchestrator blocks that rolling
materialization before mutating the roadmap. The drive report records the checkpoint intent, the
deferred materialization checkpoint result, `blocking_paths`, and the operator action needed to close
the boundary. This preserves the protection against committing unrelated user changes while still
allowing orchestrator-owned roadmap batches to checkpoint deterministically.

## Checkpoint Readiness

`status --json`, drive JSON sidecars, and drive Markdown reports include `checkpoint_readiness`. This
is a read-only local git model; it never commits, cleans, stashes, or pushes. It classifies current
dirty paths as:

- `harness_materialization`: orchestrator-owned roadmap/materialization paths, such as
  `.engineering/roadmap.yaml`;
- `task_scope`: paths inside the active drive task's `file_scope`, or the next task's `file_scope`
  when no current drive task is recorded;
- `unrelated_user`: dirty paths outside both of those boundaries.

The payload reports `ready`, `blocking`, `reason`, `dirty_paths`, `blocking_paths`,
`safe_to_checkpoint_paths`, and `recommended_action`. Staged, modified, deleted, and untracked paths
are all included in `dirty_paths`; `dirty_path_states` keeps the porcelain status evidence for
debugging.

Interpret the fields conservatively:

- `reason: clean`: unattended checkpointing has no local git blocker.
- `reason: harness_materialization_dirty`: only roadmap/materialization paths are dirty. Review or
  checkpoint those paths before unrelated work, or let a rolling materialization checkpoint handle
  them when checkpointing is enabled.
- `reason: task_scope_dirty`: dirty paths are inside the current or next task scope. Review and
  checkpoint them before switching to unrelated work.
- `blocking: true`: unrelated user dirtiness is present. Commit, stash, move, or otherwise resolve
  the `blocking_paths` yourself, then rerun `status --json` or the workspace dispatcher. The orchestrator
  will not commit or clean those paths for you.

### Self-Iteration Checkpoint Gate

Before self-iteration invokes a planner, the orchestrator evaluates checkpoint readiness. If unrelated
dirty paths are already present, the planner is not invoked and `.engineering/roadmap.yaml` is not
changed. The orchestrator writes a blocked self-iteration assessment under
`.engineering/reports/tasks/assessments/` with:

- `status: blocked`;
- `checkpoint_readiness`;
- `dirty_paths` and `blocking_paths`;
- `reason`; and
- `recommended_action`.

The same compact evidence is present in `drive --json`, the drive Markdown/JSON report,
`status --json` at `self_iteration.latest_assessment`, and
`runtime_dashboard.self_iteration.latest_assessment`.

After a planner exits, the orchestrator checks checkpoint readiness again before accepting the roadmap
diff. Harness-owned files created by the self-iteration run itself, such as its snapshot, context
pack, assessment sidecar, and active orchestrator state file, are allowed for this acceptance check. Any
other planner-created dirty path blocks acceptance; the previous roadmap text is restored and the
assessment records the blocking paths.

Recovery is local and explicit: inspect `blocking_paths`, commit/stash/move or otherwise resolve
only the operator-owned dirty paths yourself, then rerun `status --json`, `drive --self-iterate`, or
the workspace dispatcher. The orchestrator does not clean, stash, or commit those paths.

## Goal-Gap Retrospective

Every completed drive report includes a deterministic `Goal-Gap Retrospective` section and a matching
JSON sidecar next to the Markdown report under `.engineering/reports/tasks/drives/`.

The retrospective compares the final local orchestrator state with the unattended reliability goal: drain
or safely extend the roadmap, preserve local audit evidence, surface blockers deterministically, and
avoid unsafe external dependencies. It does not call a model or external service. The evidence is
bounded to local orchestrator artifacts:

- final status summary, including continuation and self-iteration settings;
- manifest index summary and recent task/drive report metadata;
- drive control and approval queue state;
- self-iteration context-pack summaries when any exist;
- local test inventory; and
- local git status and recent commits.

The Markdown section lists completed reliability capabilities, remaining risks, likely next stage
themes, and whether another self-iteration should be requested. The embedded JSON block uses the
same machine-readable `engineering-harness.goal-gap-retrospective` payload that is also present in
the drive JSON sidecar and in `drive --json` output.

Self-iteration is recommended only when the roadmap queue is empty, self-iteration is enabled, no
task or continuation stage is pending, and the drive is not blocked, failed, interrupted, or stopped
by budget. If queued work, pending approvals, failed tasks, or budget exhaustion remain, the
retrospective records those blockers instead of recommending another planning loop.

## Failure Isolation Recovery

When a task ends `failed` or `blocked`, the task manifest includes a deterministic
`failure_isolation` block. The same block is available on the task result returned by `run` or
`drive --json`, and drive reports include a top-level `failure_isolation` summary in both Markdown
and the JSON sidecar.

The task block records:

- task and milestone ids;
- the failed phase, such as `implementation`, `acceptance-2`, `repair-1`, `e2e`, `file-scope-guard`,
  or `task` for task-level policy gates;
- `failure_kind`, such as `acceptance_failure`, `policy_block`, or `file_scope_violation`;
- retry exhaustion details for task attempts and repair/acceptance iterations;
- task report and manifest paths;
- compact blocking policy decisions;
- file-scope violations; and
- a local next action for recovering the task.

Inspect unresolved isolated failures with:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The JSON response includes `failure_isolation.unresolved_count` and
`failure_isolation.latest_isolated_failures`. A failure is unresolved when the latest manifest for a
task contains `failure_isolation` and the durable task state is still `failed` or `blocked`.
Approving a pending approval gate moves that task back to `pending`; completing the task clears the
unresolved state while preserving the older manifest evidence.

Local recovery is intentionally explicit:

- for `policy_block`, review the blocking policy decisions and either approve the local gate or
  adjust the command/task before rerunning;
- for `file_scope_violation`, inspect the listed paths, keep changes within `file_scope`, then rerun;
- for `executor_no_progress`, inspect the executor watchdog evidence, fix the silent or hung local
  command, or raise the local no-progress threshold before rerunning;
- for `executor_timeout`, inspect the timeout evidence, shorten or repair the local command, or raise
  the command `timeout_seconds` before rerunning;
- for implementation, acceptance, repair, or E2E failures, inspect the named phase in the task report,
  apply a local fix inside the task file scope, then rerun the task.

Rolling drives and self-iteration will not extend the roadmap while unresolved isolated failures
exist. A later `drive --rolling --self-iterate` stops with status `isolated_failure` before adding or
materializing continuation work, and the drive report points back to the isolated task evidence.

## Executor No-Progress Watchdog

Built-in subprocess executors run in an owned local process group. The orchestrator records executor
watchdog metadata for implementation, repair, acceptance, E2E, and self-iteration planner
subprocesses: phase, executor id, command name, pid, start time, last output/progress time,
`timeout_seconds`, configured no-progress threshold, and termination evidence when a watchdog fires.
Executors that expose structured stdout events can also attach compact `executor_event` payloads to
the same progress stream. The built-in OpenHands adapter uses this for JSONL output and persists the
latest event plus a short event history in drive control state.

Runtime timeout uses each command's `timeout_seconds`. No-progress detection is disabled by default
to preserve short local tests, and can be enabled locally in the roadmap:

```yaml
executor_watchdog:
  enabled: true
  no_progress_seconds: 900
  phase_no_progress_seconds:
    implementation: 1800
    repair: 900
    acceptance: 300
    e2e: 600
    planner: 1200
```

Individual commands may override the roadmap with `no_progress_timeout_seconds`. Environment
variables override roadmap defaults for local recovery runs:

```bash
ENGINEERING_HARNESS_EXECUTOR_NO_PROGRESS_SECONDS=300 \
  python3 -m engineering_harness.cli drive --project-root /path/to/project

ENGINEERING_HARNESS_EXECUTOR_NO_PROGRESS_ACCEPTANCE_SECONDS=60 \
  python3 -m engineering_harness.cli run --project-root /path/to/project
```

Use `ENGINEERING_HARNESS_EXECUTOR_WATCHDOG_ENABLED=0` to disable no-progress checks locally.
Runtime `timeout_seconds` still applies. When a watchdog fires, only the process group started for
that executor invocation is terminated; the drive continues to write machine-readable status,
manifest, task report, and `failure_isolation.executor_watchdog` evidence.

## Pause A Long Drive

```bash
python3 -m engineering_harness.cli pause --project-root /path/to/project --reason "operator review"
```

The next `drive` invocation exits with status `paused` and does not start a task:

```bash
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

Inspect the durable state:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The `drive_control` block shows `status: paused` and `pause_requested: true`.

## Resume A Drive

```bash
python3 -m engineering_harness.cli resume --project-root /path/to/project --reason "review complete"
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

`resume` clears pause, cancel, and reviewed stale watchdog state. It does not start work by itself;
run `drive` after resuming. If a drive is still actively running with a live pid or a fresh heartbeat,
`resume` leaves that active state in place.

## Recover A Stale Drive

If status shows `drive_control.status: stale`, inspect the last activity and task fields first:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

For unattended local drives, the next `drive` invocation automatically recovers only the deterministic
stale-running case where the heartbeat is stale and the recorded pid is missing or dead. The drive
report JSON, Markdown report, `status --json`, and runtime dashboard expose the
`stale_running_recovery` block.

The local regression that exercises the dead-owner recovery path through a full drive and E2E command
is:

```bash
python3 -m pytest tests/test_engineering_harness.py -q -k stale_running_dead_owner_recovery_e2e
```

When a stale state was already marked and reviewed, or when an operator intentionally wants to clear a
paused/cancelled control, clear it locally:

```bash
python3 -m engineering_harness.cli resume --project-root /path/to/project --reason "recover stale drive"
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

The next drive selects work from durable task state and reports. It does not kill the old pid. If the
heartbeat is fresh, status and the scorecard keep the run as protected `in_progress` work so a
concurrent local run is not overwritten. If the heartbeat is stale but the pid is still alive, stale
recovery remains blocked for operator review.

## Restart-Safe Phase Replay

When a local drive is interrupted after a task phase has already passed, the next local `drive`
continues from durable phase state instead of blindly replaying completed commands. A passed
`implementation` phase, or the latest passed `acceptance-N` phase, is reused only when its saved
command-group fingerprint matches the current task definition. If the task command group changed, or
the task is in an explicit failed/blocked retry state, the guard leaves the phase runnable.

Replay-guard reuse is local and deterministic. It never requires network access or external account
state, and it does not bypass remaining required work: E2E commands, file-scope checks, manifest
writing, failure-isolation cleanup, checkpoint gates, and normal finalization still run. Evidence is
recorded in:

- task `phase_history` with `event: "reused"` and `metadata.replay_guard`;
- task run manifests under `replay_guard`, plus per-command `executor_result.metadata.replay_guard`;
- drive Markdown and JSON reports under `replay_guard`;
- `status --json` at top-level `replay_guard`;
- `runtime_dashboard.replay_guard`.

## Cancel A Drive

```bash
python3 -m engineering_harness.cli cancel --project-root /path/to/project --reason "superseded plan"
```

Future `drive` invocations stop with status `cancelled` until the control state is cleared:

```bash
python3 -m engineering_harness.cli resume --project-root /path/to/project --reason "clear cancellation"
```

Cancellation is a drive control, not a roadmap edit. It does not delete tasks or reports.

## Approval Queue

Manual, live, and agent gates create pending approval records when a task is blocked by policy.
Each record is a local approval lease request, not a permanent bypass.

```bash
python3 -m engineering_harness.cli drive --project-root /path/to/project
python3 -m engineering_harness.cli approvals --project-root /path/to/project
```

The queue records the approval id, task id, gate kind, phase or command, reason, deterministic
approval fingerprint, and lease timestamps. The fingerprint is computed from stable local policy
decision fields: project root, task id, normalized phase, command name, command or prompt digest,
executor id and metadata, decision kind, approval kind, approval flag, file scope, and command-policy
metadata. To approve one gate:

```bash
python3 -m engineering_harness.cli approve --project-root /path/to/project APPROVAL_ID --reason "approved by operator"
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

For local test projects, all pending gates can be approved at once:

```bash
python3 -m engineering_harness.cli approve --project-root /path/to/project --all --reason "local dry-run approval"
```

Approved gates unblock the affected task for a later drive run only while the current policy decision
still matches the stored fingerprint and the lease has not expired. The default local lease TTL is one
hour. Projects can override it in `.engineering/roadmap.yaml`:

```json
{
  "approval_leases": {
    "ttl_seconds": 3600
  }
}
```

If the command text, prompt, executor, file scope, or relevant policy metadata changes after approval,
the old record is marked `stale` with reason `approval fingerprint mismatch: current policy decision
changed`, and the next blocked run queues a fresh approval. Expired leases are marked `stale` with the
expiration timestamp. When a task completes, matching approval records are marked `consumed` so the
state remains auditable and cannot satisfy future gates.

`approvals --json`, `status --json`, task manifests, and drive reports expose `stale_count` and
`stale_reasons` under `approval_queue`.

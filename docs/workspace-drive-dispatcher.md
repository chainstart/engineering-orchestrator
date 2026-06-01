# Workspace Drive Dispatcher

`engo workspace-drive` scans a local workspace, builds a deterministic project queue, and starts at
most one eligible project drive per invocation. It is intended for unattended loops such as cron or a
local supervisor where each tick should make bounded progress without pushing, deploying, or requiring
external accounts.

Inspect the workspace first:

```bash
bin/engo scan --workspace /path/to/workspace --json
```

Run one bounded dispatch tick:

```bash
bin/engo workspace-drive --workspace /path/to/workspace --max-tasks 1 --json
```

For long unattended rolling work, keep each tick small:

```bash
bin/engo workspace-drive \
  --workspace /path/to/workspace \
  --max-tasks 1 \
  --rolling \
  --max-continuations 1 \
  --time-budget-seconds 1800 \
  --json
```

For a local daemon-style loop that persists supervisor metadata between ticks, use
`daemon-supervisor`. It is still file/CLI based and does not fork into the background; an external
process manager, cron, or shell can restart it safely because the loop state is durable:

```bash
bin/engo daemon-supervisor \
  --workspace /path/to/workspace \
  --max-ticks 12 \
  --run-window-seconds 3600 \
  --max-tasks 1 \
  --rolling \
  --max-continuations 1 \
  --json
```

The supervisor writes restartable runtime state to:

```text
<workspace>/.engineering/state/daemon-supervisor-runtime.json
```

and writes Markdown/JSON reports under:

```text
<workspace>/.engineering/reports/daemon-supervisor-runtime/
```

Each supervisor tick delegates to `workspace-drive`, then records the dispatch report, selected
project, drive status, idle/sleep/backoff decision, run-window tick count, and an operator-visible
`stop_reason`. If the previous supervisor process disappears while its state still says `running`, a
later invocation records `restartable_loop.recovered_previous` and resumes from the durable report
history. Completed project tasks are not rerun because the selected project still uses its normal
durable task state and replay/manifest evidence.

By default, the dispatcher uses the local fair scheduler. It keeps the safety queue deterministic, but
eligible projects are ordered by a score instead of always taking the first resolved path. The score is
computed from local-only evidence:

- pending roadmap task count and pending continuation stage count;
- latest drive goal-gap retrospective severity and age;
- recent workspace dispatch success or failure history;
- a cooldown penalty for projects selected recently, so long loops do not keep choosing the same
  project while another eligible project waits;
- nonproductive dispatch backoff when the most recent selected drive failed, was interrupted, hit
  self-iteration planner output validation failure, or exhausted budget without completing a task or
  materializing continuation/self-iteration work.

Equal fair scores are resolved by stable resolved-path ordering. For compatibility checks that should
assert the old first-eligible-path behavior, pass:

```bash
bin/engo workspace-drive --workspace /path/to/workspace --scheduler-policy path-order --json
```

Later eligible projects are left for later invocations and recorded with `one_project_per_invocation`.

## Nonproductive Backoff

Backoff is local and deterministic. The dispatcher reads only workspace dispatch report sidecars and
the selected project's local drive report evidence. It does not rewrite a project's roadmap or state
just because that project is in backoff.

An eligible project receives a bounded score penalty when its latest selected dispatch is classified
as nonproductive. The default backoff window is 3600 seconds. Override it per invocation with:

```bash
bin/engo workspace-drive --workspace /path/to/workspace --nonproductive-backoff-seconds 7200 --json
```

or set `ENGINEERING_HARNESS_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS` for unattended
supervisor loops. Values are capped at 86400 seconds; `0` disables this backoff component.

Inspect `queue[].backoff` or `queue[].score_components.nonproductive_backoff` for the decision. The
payload records `decision`, `active`, `source_report`, `reason`, `age_seconds`, `threshold_seconds`,
`expires_at`, and the selected drive report path. `active_penalty` means another healthy eligible
project should run first. `expired`, `productive`, and `not_nonproductive` do not apply the penalty.

Recovery is evidence-driven:

- `drive_failed`: inspect `source_drive_report_json`, fix the local failure cause, then let the
  backoff expire or rerun with a smaller backoff window after review.
- `planner_validation_failed`: inspect the self-iteration report referenced by the drive report,
  correct the planner output contract, and rerun locally.
- `budget_without_progress`: raise the relevant task, continuation, or self-iteration budget only
  when the previous tick had no chance to materialize work.
- `interrupted`: resolve the local interruption source before expecting the project to compete
  normally.

Paused, cancelled, unrecoverable stale-running, approval-blocked, invalid, out-of-scope, and
failure-isolated projects remain hard skips rather than backoff-scored candidates.

## Dispatch Lease

`workspace-drive` acquires a durable local lease before scanning the workspace. The lease lives under:

```text
<workspace>/.engineering/state/workspace-dispatch-lease/lease.json
```

The lease records the workspace root, owner pid, start time, last heartbeat time, heartbeat count,
selected project when known, command options, and stale-after threshold. It is released on successful
and failed dispatch completion.

If another tick finds a fresh lease owned by a live process, it refuses to scan or dispatch, exits
non-zero with `status: "lease_held"` in `--json` output, and still writes the normal Markdown report
and JSON sidecar under `.engineering/reports/workspace-dispatches/`.

Stale leases are recovered locally before scanning when either:

- the recorded owner pid is no longer running;
- the recorded heartbeat is older than the lease stale-after threshold.

The default threshold is 3600 seconds. Override it for a command with:

```bash
bin/engo workspace-drive --workspace /path/to/workspace --lease-stale-after-seconds 7200 --json
```

or set `ENGINEERING_HARNESS_WORKSPACE_DISPATCH_LEASE_STALE_AFTER_SECONDS` for cron or supervisor
environments.

## Safety Skips

Before scoring projects, `workspace-drive` runs the same local stale-running recovery preflight that
`drive` uses for each valid project. A project whose `drive_control.status` is `running` is recovered
to `idle` only when the heartbeat is stale and the recorded pid is missing or dead. The queue item,
selected-project block, dispatch JSON sidecar, Markdown report, and runtime dashboard then include
`stale_running_recovery` evidence. If the pid is alive or the heartbeat is fresh, the dispatcher does
not mutate the project and the queue item carries `stale_running_block` / `stale_running_preflight`
evidence.

Projects are skipped, with explicit JSON evidence, when they are:

- missing an engineering roadmap or carrying an invalid roadmap;
- outside the requested local workspace scope;
- blocked by checkpoint readiness because unrelated dirty git paths are present;
- paused, cancelled, already running, or stale-running;
- waiting on pending approval gates;
- carrying unresolved isolated task failures;
- empty when neither `--rolling` nor `--self-iterate` is requested.

Skipped projects are inspected read-only except for the deterministic stale-running recovery above.
Their roadmap and state files are not rewritten just because the workspace dispatcher scanned them.

Each queue item carries `checkpoint_readiness`. The dispatcher treats `blocking: true` as a hard
`checkpoint_not_ready` skip before it reports a project as eligible. Read `blocking_paths` and
`recommended_action` first: unattended loops must not commit or clean unrelated user changes. A
project can re-enter the queue after the operator commits, stashes, moves, or otherwise resolves those
blocking paths locally. Roadmap-only materialization dirtiness and next-task `file_scope` dirtiness
are reported as `safe_to_checkpoint_paths` rather than hard skips.

When `--self-iterate` is enabled, the selected project still runs the project-local self-iteration
checkpoint gate before invoking its planner and again before accepting a planner roadmap diff. If that
gate blocks inside the selected drive, the project drive report and the workspace dispatch sidecar
carry the blocked self-iteration assessment path plus compact `checkpoint_readiness`,
`dirty_paths`, `blocking_paths`, `reason`, and `recommended_action` evidence. The dispatcher does not
repair those paths; resolve them locally and let the next tick rescan the project.

## Reports

Every invocation writes a Markdown report and JSON sidecar under:

```text
<workspace>/.engineering/reports/workspace-dispatches/
```

The JSON output and sidecar include the scheduler policy, durable `queue_summary`, full queue, each
eligible project score, score components, explicit priority/starvation-prevention evidence, resource
budget fields, project drive-lease ownership snapshot, retry/backoff summary, checkpoint readiness,
nonproductive backoff decision, skip reasons, selected reason, selected project, stale-running
recovery/block evidence, drive status, self-iteration count, and the selected project drive report
path. Use the project-level report for task execution details and the workspace report for scheduling
evidence.

## Operating Loop

Use repeated small invocations instead of a single large drive. For a bounded local supervisor check,
run a fixed number of ticks and let each tick write its own dispatch report:

```bash
WORKSPACE=/path/to/workspace
TICKS=6

for tick in $(seq 1 "$TICKS"); do
  bin/engo workspace-drive \
    --workspace "$WORKSPACE" \
    --max-tasks 1 \
    --time-budget-seconds 1800 \
    --rolling \
    --max-continuations 1 \
    --json
done
```

For cron, keep the same one-project tick shape and redirect stdout to your local supervisor logs:

```cron
*/5 * * * * cd /path/to/engineering-harness && bin/engo workspace-drive --workspace /path/to/workspace --max-tasks 1 --time-budget-seconds 1800 --rolling --max-continuations 1 --json
```

For a long-running shell supervisor, keep the interval outside the dispatcher:

```bash
while true; do
  bin/engo workspace-drive \
    --workspace /path/to/workspace \
    --max-tasks 1 \
    --time-budget-seconds 1800 \
    --rolling \
    --max-continuations 1 \
    --json
  sleep 300
done
```

The built-in lease is sufficient for overlapping cron or local supervisor ticks. A second tick will
produce machine-readable lease evidence instead of racing the active scanner. Supervisors should treat
`lease_held` as a normal contention outcome and retry on the next interval.

`daemon-supervisor` adds a durable run window around those same ticks. The JSON output and sidecar
include:

- `run_window`: start time, deadline, configured window seconds, tick count, and remaining ticks.
- `restartable_loop`: generation, resume count, previous-loop snapshot, recovered stale runtime
  evidence, and completed dispatch report history.
- `ticks[]`: one workspace dispatch result per loop tick with the decision that followed it.
- `last_decision`: the latest `continue`, `sleep`, or `stop` action, including idle sleep and
  nonproductive backoff seconds when relevant.
- `stop_reason`: the final operator-facing reason such as `max_ticks`, `idle_limit`,
  `run_window_expired`, `dispatch_failed`, or `runtime_already_running`.

Resolve safety skips locally before expecting a project to re-enter the queue:

- `bin/engo resume --project-root <project>` clears pause, cancel, or stale drive control after review.
- `bin/engo approvals --project-root <project> --json` shows pending approval gates.
- `bin/engo status --project-root <project> --json` shows unresolved isolated failure evidence.

Review evidence after each run from newest to oldest:

```bash
ls -t /path/to/workspace/.engineering/reports/workspace-dispatches/*.json | head
python3 -m json.tool /path/to/workspace/.engineering/reports/workspace-dispatches/<report>.json
```

Check `status`, `selected`, `queue_summary`, `queue[].skip_reasons`,
`queue[].checkpoint_readiness`, `queue[].project_lease`, `queue[].resource_budget`,
`queue[].retry_backoff_summary`, `queue[].stale_running_recovery`,
`queue[].stale_running_block`, and `lease` first. Top-level `status` value `lease_held` is normal
contention evidence for overlapping ticks.
`lease.recovered: true` with a `recovery.reason` such as `pid_gone` or `heartbeat_stale` shows stale
local lease recovery. Project-level `stale_running_recoveries[]` shows recovered drive-control state
before selection. For a selected project, open `selected.drive_report_json` or
`drive.drive_report_json` for task-level evidence. For skipped projects, inspect the project with
`approvals --json` or `status --json` before resuming or approving anything.

For scheduler questions, inspect `scheduler_policy`, `selected.selected_reason`,
`queue[].scheduler_rank`, `queue[].score`, `queue[].priority`, `queue[].backoff`,
`queue[].retry_backoff_summary`, and `queue[].score_components`. Safety skips such as approval
blocks and unresolved isolated failures remain blocking evidence and are not scored; their queue
items carry `score: null` with the skip codes under `score_components.skip_codes`.

Project status now carries the nearest workspace dispatch evidence under
`runtime_dashboard.workspace_dispatch`. From a project directory, use:

```bash
bin/engo status --project-root . --json
```

That dashboard block shows the latest dispatch queue, queue summary, selected project, active
workspace lease when one exists, latest released lease evidence, per-project drive lease evidence,
resource budgets, retry/backoff summaries, stale-running recovery or block evidence, score
components, selected reason, scheduler policy, nonproductive backoff evidence, and the dispatch report
sidecar paths. It is the quickest way to answer whether the project is skipped by workspace
scheduling, cooling down after a recent selection or nonproductive drive, blocked by a lease, blocked
by live drive-control protection, out of retry budget, or simply waiting behind another eligible
project.

The same status payload also carries the nearest daemon supervisor evidence under
`runtime_dashboard.daemon_supervisor_runtime` and top-level `daemon_supervisor_runtime`. Inspect
`stop_reason`, `last_decision`, `run_window`, `restartable_loop.completed_dispatch_reports`, and
`latest_report.json_path` first when diagnosing a long unattended local loop.

The workspace dispatcher does not expose push flags and passes project drives with local checkpointing
disabled. Roadmap tasks still run under the project policy engine, so live operations and agent or
manual approval gates remain blocked unless explicitly allowed by the operator.

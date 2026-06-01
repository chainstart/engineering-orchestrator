# Executor Contract

Executor adapters let the orchestrator run shell commands, Codex/OpenHands prompts, and future execution
backends without changing task semantics. Roadmap tasks still define command groups with `executor`,
`command` or `prompt`, `timeout_seconds`, optional `no_progress_timeout_seconds`, `required`,
`model`, `sandbox`, and optional `requested_capabilities`.

## Adapter Interface

An adapter provides:

- `metadata`: stable executor identity and capability metadata.
- `diagnostics(project_root=...)` (optional): local readiness details for status surfaces without
  launching the executor.
- `display_command(invocation)`: the auditable command string written to reports and manifests.
- `execute(invocation)`: a normalized result with status, return code, timestamps, stdout, stderr,
  and adapter-specific metadata.

Built-in executors are registered in `default_executor_registry()`:

- `shell`: local `/bin/bash` process execution. It uses the command allowlist policy.
- `codex`: `codex exec` agent execution. It requires `--allow-agent`.
- `openhands`: local OpenHands CLI headless agent execution. It requires `--allow-agent` and is
  blocked unless OpenHands support is explicitly enabled.
- `dagger`: local Dagger CLI execution. It is discoverable by roadmap validation, but execution is
  blocked unless Dagger support is explicitly enabled.

Tests and future integrations can pass a custom registry to `Harness(..., executor_registry=...)`.
Unknown executor ids fail roadmap validation and task preflight with the same clear messages used
by existing orchestrator behavior.

`engo status --json` includes `executor_diagnostics` at the top level and under
`runtime_dashboard.executor_diagnostics`. The diagnostics payload records each registered executor's
status, configured/enabled booleans, capability metadata, unsafe capability classes, and
adapter-specific health details when available.

## Capability Requests

Roadmap command entries may declare `requested_capabilities` when a task needs an explicit local
executor capability contract:

```json
{
  "name": "local tests",
  "command": "python3 -m pytest tests -q",
  "requested_capabilities": ["local_process", "workspace_write", "stdout", "stderr", "exit_code"]
}
```

The field is optional for backward compatibility. When present it must be a non-empty list of known
capability names. The current local vocabulary is:

- Low-risk local capabilities: `local_process`, `workspace_write`, `exit_code`, `stdout`, `stderr`,
  `agent`, `local_dagger_cli`, `local_openhands_cli`, `containerized_execution`, and
  `requires_explicit_configuration`.
- Unsafe request markers: `network`, `network_access`, `secret_access`, `secrets`,
  `browser_automation`, `deployment`, `deploy`, `live_operations`, and `live`.

Before a command runs, the orchestrator compares `requested_capabilities` with the selected executor's
metadata capabilities. Supported low-risk requests are allowed. Unsupported requests are denied.
Unsafe request markers are denied even if an executor could technically perform them, because the
unattended local orchestrator does not yet have an explicit approval mechanism for network access,
secret access, browser automation, deployment, or live operations.

## Manifest Contract

Each run keeps the legacy manifest fields for compatibility:

- `executor`
- `command`
- `status`
- `returncode`
- `stdout`
- `stderr`
- `required`
- `timeout_seconds`
- `no_progress_timeout_seconds`
- `model`
- `sandbox`
- `requested_capabilities`
- `executor_capabilities`

Each run also includes normalized executor fields:

- `executor_metadata`: schema version, id, display name, kind, adapter id, input mode,
  capabilities, and policy/approval flags.
- `executor_result`: schema version, status, return code, started/finished timestamps, stdout and
  stderr summaries, and optional adapter-specific result metadata.
- `context_pack` for agent prompt runs, when a bounded local context pack was written.

This keeps reports and manifest readers compatible while giving future adapters a stable place to
record capabilities and result details.

## Agent Context Packs

Before a real `agent` executor run with prompt input, the orchestrator writes a redacted JSON context pack
under `.engineering/reports/tasks/agent-context-packs/`. The pack includes bounded task metadata,
the current command, verification command summaries, relevant `spec_refs`, compact spec index
metadata, and short requirement excerpts extracted from the configured local spec path or structured
requirements index.

The Codex and OpenHands prompt templates include:

- the relevant `spec_refs`;
- bounded requirement excerpts;
- the context-pack path and digest.

The prompt never embeds the full spec document. Context pack counts and text sizes are capped, and
sensitive-looking values are redacted before persistence. Task manifests reference the context pack
from the run-level `context_pack` field and from `artifacts` with kind `agent_context_pack`.

## Watchdog Results

Built-in subprocess adapters (`shell`, enabled `dagger`, `openhands`, and `codex`) enforce
`timeout_seconds` with an owned local process group. When `no_progress_timeout_seconds` or the local
`executor_watchdog` roadmap defaults are configured, the same process monitor also terminates runs
that produce no stdout/stderr progress before the threshold.

Watchdog outcomes use deterministic executor statuses:

- `timeout`: the command exceeded `timeout_seconds`;
- `no_progress`: the command exceeded the configured no-progress threshold.

Task status remains `failed` for required commands, and the task manifest records
`failure_isolation.executor_watchdog` with the phase, executor metadata, command name, pid when
available, thresholds, last output/progress timestamp, report paths, and local next action.
Self-iteration planner watchdog failures return planner failure-isolation evidence in the
self-iteration result and report.

## OpenHands Adapter Stub

The OpenHands adapter is intentionally gated for local experimentation. Roadmaps select it with
`executor: "openhands"` and provide a natural-language task in `prompt`. The orchestrator expands the
prompt with the same task context used for Codex, records the auditable display command as
`openhands --headless --json --override-with-envs -t <task:...>`, runs from the project root, and
captures stdout/stderr into the standard manifest fields.

By default the adapter returns a blocked executor result:

- status: `blocked`
- return code: `null`
- metadata: `{"configured": false, "required_environment": "ENGINEERING_HARNESS_ENABLE_OPENHANDS"}`

To opt into local OpenHands execution, set `ENGINEERING_HARNESS_ENABLE_OPENHANDS=1` in the orchestrator
environment and ensure the `openhands` binary is on `PATH`. Set
`ENGINEERING_HARNESS_OPENHANDS_BINARY=/path/to/openhands` to use a specific binary. If the roadmap
entry sets `model`, the adapter exports it as `LLM_MODEL` and passes `--override-with-envs`, matching
the current OpenHands CLI configuration path.

OpenHands executor metadata includes a `health` summary with the selected binary, whether it was
found on `PATH`, whether `LLM_MODEL`, `LLM_API_KEY`, or `LLM_BASE_URL` are present, and whether
`~/.openhands/agent_settings.json` exists. Successful runs also include an `openhands_jsonl` summary
of parsed JSONL output: event counts, recent compact events, touched paths, and any non-JSON lines.
The orchestrator records booleans for credentials and redacted previews; it does not copy API keys into
manifests.

While the process is still running, complete JSONL objects written to stdout are also translated into
`executor_event` progress payloads. Persisted drive state keeps `latest_executor_event`,
`executor_event_count`, and a short `executor_event_history`, so status consumers can show the most
recent OpenHands action before the final manifest is written.

Because the OpenHands executor declares `network_access` and `browser_automation`, policy manifests
also include warning-level capability audit evidence for those inherent executor capabilities. The
warning does not replace the adapter enablement gate; execution still requires
`ENGINEERING_HARNESS_ENABLE_OPENHANDS=1` and `--allow-agent`.

This keeps the current orchestrator safe while leaving a clear integration path for later work:

- define finer local workspace, network, and browser policies before unattended use;
- keep all OpenHands outcomes flowing through the same executor metadata and result contract used by
  shell, Dagger, and Codex.

## Dagger Adapter Stub

The Dagger adapter is intentionally local-first. Roadmaps select it with `executor: "dagger"` and
provide a Dagger CLI command in `command`. The command may be written either as arguments after the
binary, such as `call test --source=.`, or with the leading binary, such as
`dagger call test --source=.`. The adapter records the auditable display command as `dagger ...`,
runs from the project root, captures stdout/stderr into the standard manifest fields, and does not
execute through a shell.

By default the adapter returns a blocked executor result:

- status: `blocked`
- return code: `null`
- metadata: `{"configured": false, "required_environment": "ENGINEERING_HARNESS_ENABLE_DAGGER"}`

To opt into local Dagger execution, set `ENGINEERING_HARNESS_ENABLE_DAGGER=1` in the orchestrator
environment and ensure the `dagger` binary is on `PATH`. If the flag is enabled but the binary is
missing, the adapter still blocks and records `{"configured": true, "missing_binary": "dagger"}`.

This keeps the current orchestrator safe while leaving a clear integration path for later work:

- add project-level Dagger module discovery;
- map roadmap task metadata into Dagger function arguments;
- define cache, network, and secret policies before enabling remote or deployment-oriented runs;
- keep all Dagger outcomes flowing through the same executor metadata and result contract used by
  shell and Codex.

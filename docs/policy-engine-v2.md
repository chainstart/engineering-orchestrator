# Policy Engine V2 Schema

Policy Engine V2 records the data used for orchestrator safety checks as a structured `policy_input`
contract and emits normalized `policy_decisions` from that input. Task manifests persist those
decisions and a deterministic `policy_decision_summary`; Markdown task reports embed the same
summary and decisions in a JSON evidence block; manifest indexes aggregate the summaries across
runs. The current Python evaluator preserves the existing command allowlist, executor approval,
executor capability, manual approval, live flag, git preflight, and file-scope behavior.

## Policy Input

Task-run manifests include a top-level `policy_input` object with `schema_version: 1`.

The input captures:

- `project`: project name, root, profile, and roadmap path.
- `task`: task and milestone ids, title, status, approval requirements, and iteration limit.
- `phase`: `task` for task-level checks or the command group name for command-level checks.
- `command`: command name, command text or prompt, required flag, timeout, model, sandbox,
  requested capabilities, and executor id.
- `executor`: normalized executor metadata from the executor contract, including executor
  capabilities.
- `git`: repository flag, root, branch, head, short head, and refs.
- `worktree`: git preflight status, file-scope guard status, dirty paths, changed paths, and violations.
- `file_scope`: allowed patterns plus out-of-scope and violation paths.
- `approvals`: `allow_manual`, `allow_agent`, task approval requirements, and executor agent requirement.
- `live`: `allow_live`, live-gated patterns, matched patterns, and whether a live action was detected.
- `context`: policy profile, command policy version, and orchestrator default timeout.

## Policy Decisions

Each decision has `schema_version: 1`, `kind`, `scope`, `outcome`, `effect`, `severity`, `reason`,
and the structured `input` used to make the decision.

Decision styles:

- Allow: `outcome: allowed`, `effect: allow`.
- Deny: `outcome: denied`, `effect: deny`.
- Warning: `outcome: warning`, `effect: warn`.
- Requires approval: `outcome: requires_approval`, `effect: requires_approval`, with
  `requires_approval: true` and an `approval_flag`.

The evaluator currently emits decisions for manual approval, task agent approval, executor policy,
capability policy, executor approval, command policy, live approval, git preflight, and file-scope
guard checks.
Approval queue records are leases bound to a deterministic fingerprint of the current local decision
context. A later run treats an approved record as satisfied only when the current fingerprint matches
and the lease TTL has not expired. Mismatched or expired records become `stale`, and fresh policy
blocks queue new records instead of reusing the stale lease.

`capability_policy` decisions are emitted when a command declares `requested_capabilities`, when
command safety classification detects unsafe behavior, or when the selected executor declares
inherent unsafe capabilities such as network or browser automation. Allowed decisions mean every
requested low-risk capability is present in the selected executor metadata. Denied decisions are
blocking and record `requested_capabilities`, `executor_capabilities`, `unsupported_capabilities`,
`unsafe_capabilities`, explicit filesystem/network/secret/deploy class metadata, and the known
vocabulary in decision metadata. Unsafe requests and detected unsafe command behavior are denied
rather than approval-gated because the unattended safety contract has no current local approval
mechanism for network access, secret access, browser automation, deployment, or live operations.
When an older command allowlist denial already blocks a detected unsafe command, the capability
policy still emits warning-level audit evidence instead of hiding the unsafe classification.
Inherent unsafe executor capabilities are also warning-level audit evidence unless another policy
gate blocks; this keeps explicitly configured agent executors observable without silently changing
their adapter-specific enablement gates.

The summary shape is intentionally compact:

- `total`: number of decisions emitted for the run or index.
- `by_kind`, `by_outcome`, `by_effect`, and `by_severity`: deterministic count maps.
- `blocking`: compact decision records whose effect is `deny` or `requires_approval`.
- `requires_approval`: compact decision records that name the required approval flag.

## OPA/Rego Compatibility

Policy Engine V2 includes an optional OPA/Rego compatibility surface, but does not add Open Policy
Agent as a runtime dependency. The built-in Python evaluator remains authoritative by default.
External OPA/Rego evaluation is advisory unless a future integration explicitly changes enforcement
semantics. Task execution does not call this hook unless a caller opts in outside the default run
path.

The compatibility hook lives in `engineering_harness.policy_compat`:

- `export_policy_input_for_opa(policy_input)` returns an OPA-friendly JSON document.
- `serialize_policy_input_for_opa(policy_input)` returns the same document as deterministic JSON.
- `evaluate_opa_policy_input(policy_input)` is disabled by default and returns no decisions.
- `evaluate_opa_policy_input(policy_input, enabled=True, evaluator=...)` calls an injected evaluator
  and treats returned decisions as advisory.

The export wrapper has `schema_version: 1`, `kind: opa_rego_policy_input_export`, `target:
opa-rego`, `authoritative_engine: python`, OPA package hints, and the original `policy_input`
contract under `policy_input`.

Recommended Rego layout:

```rego
package engineering_harness.policy.v1

default decisions := []

policy_input := input.policy_input
```

The export advertises `data.engineering_harness.policy.v1.decisions` as the entrypoint and
`input.policy_input` as the input path. Rego policies that emit decisions should use the same
normalized decision vocabulary as the Python evaluator: `allowed`, `denied`, `warning`, and
`requires_approval`; `allow`, `deny`, `warn`, and `requires_approval`; plus the existing severity
values. External decisions should be converted back to `policy_decisions` schema version 1 before
they are compared with or displayed alongside Python decisions.

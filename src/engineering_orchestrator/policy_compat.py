from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any


POLICY_INPUT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
OPA_COMPATIBILITY_SCHEMA_VERSION = 1
OPA_REGO_PACKAGE = "engineering_orchestrator.policy.v1"
OPA_REGO_ENTRYPOINT = "data.engineering_orchestrator.policy.v1.decisions"

__all__ = [
    "OPA_COMPATIBILITY_SCHEMA_VERSION",
    "OPA_REGO_ENTRYPOINT",
    "OPA_REGO_PACKAGE",
    "evaluate_opa_policy_input",
    "export_policy_input_for_opa",
    "serialize_policy_input_for_opa",
]


def _policy_input_contract(policy_input: object) -> dict[str, Any]:
    as_contract = getattr(policy_input, "as_contract", None)
    if callable(as_contract):
        payload = as_contract()
    else:
        payload = policy_input
    if not isinstance(payload, Mapping):
        raise TypeError("policy_input must be a mapping or expose as_contract()")
    contract = deepcopy(dict(payload))
    if contract.get("schema_version") != POLICY_INPUT_SCHEMA_VERSION:
        raise ValueError(f"policy_input schema_version must be {POLICY_INPUT_SCHEMA_VERSION}")
    return contract


def export_policy_input_for_opa(
    policy_input: object,
    *,
    external_evaluation_enabled: bool = False,
) -> dict[str, Any]:
    """Return an OPA/Rego-compatible export document for a policy input.

    The export is intentionally advisory metadata plus the existing policy input
    contract. It does not import, spawn, or depend on OPA.
    """

    return {
        "schema_version": OPA_COMPATIBILITY_SCHEMA_VERSION,
        "kind": "opa_rego_policy_input_export",
        "target": "opa-rego",
        "policy_input_schema_version": POLICY_INPUT_SCHEMA_VERSION,
        "policy_decision_schema_version": POLICY_DECISION_SCHEMA_VERSION,
        "authoritative_engine": "python",
        "external_evaluation": {
            "enabled": bool(external_evaluation_enabled),
            "decision_mode": "advisory",
            "runtime_dependency": None,
        },
        "rego": {
            "package": OPA_REGO_PACKAGE,
            "entrypoint": OPA_REGO_ENTRYPOINT,
            "input_path": "input.policy_input",
        },
        "policy_input": _policy_input_contract(policy_input),
    }


def serialize_policy_input_for_opa(
    policy_input: object,
    *,
    external_evaluation_enabled: bool = False,
    indent: int | None = None,
) -> str:
    return json.dumps(
        export_policy_input_for_opa(
            policy_input,
            external_evaluation_enabled=external_evaluation_enabled,
        ),
        indent=indent,
        sort_keys=True,
    )


def evaluate_opa_policy_input(
    policy_input: object,
    *,
    enabled: bool = False,
    evaluator: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Safe OPA/Rego evaluation stub.

    The built-in Python policy evaluator remains authoritative. When disabled,
    this function does not call the supplied evaluator. When enabled, callers
    must inject their own evaluator and its result is still advisory.
    """

    export = export_policy_input_for_opa(policy_input, external_evaluation_enabled=enabled)
    if not enabled:
        return {
            "schema_version": OPA_COMPATIBILITY_SCHEMA_VERSION,
            "kind": "opa_rego_policy_evaluation",
            "enabled": False,
            "status": "disabled",
            "authoritative": False,
            "authoritative_engine": "python",
            "decision_mode": "disabled",
            "reason": "OPA/Rego compatibility hook is disabled; Python evaluator remains authoritative.",
            "export": export,
            "decisions": [],
        }
    if evaluator is None:
        return {
            "schema_version": OPA_COMPATIBILITY_SCHEMA_VERSION,
            "kind": "opa_rego_policy_evaluation",
            "enabled": True,
            "status": "not_configured",
            "authoritative": False,
            "authoritative_engine": "python",
            "decision_mode": "advisory",
            "reason": "No external OPA/Rego evaluator was supplied; no OPA runtime dependency is bundled.",
            "export": export,
            "decisions": [],
        }

    external_result = evaluator(deepcopy(export))
    if not isinstance(external_result, Mapping):
        raise TypeError("OPA/Rego evaluator must return a mapping")
    result_payload = deepcopy(dict(external_result))
    decisions = result_payload.get("decisions", [])
    if decisions is None:
        decisions = []
    if not isinstance(decisions, list):
        raise TypeError("OPA/Rego evaluator result decisions must be a list")
    return {
        "schema_version": OPA_COMPATIBILITY_SCHEMA_VERSION,
        "kind": "opa_rego_policy_evaluation",
        "enabled": True,
        "status": "evaluated",
        "authoritative": False,
        "authoritative_engine": "python",
        "decision_mode": "advisory",
        "reason": "OPA/Rego evaluator returned advisory decisions; Python evaluator remains authoritative.",
        "export": export,
        "external_result": result_payload,
        "decisions": deepcopy(decisions),
    }

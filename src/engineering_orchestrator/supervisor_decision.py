from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPERVISOR_DECISION_KIND = "engineering-orchestrator.supervisor-decision.v1"
SUPERVISOR_DECISION_VALIDATION_KIND = "engineering-orchestrator.supervisor-decision-validation.v1"
SUPERVISOR_DECISION_SCHEMA_VERSION = 1

SUPERVISOR_DECISION_ACTIONS = {
    "continue",
    "pause",
    "retry",
    "repair_task_package",
    "split_task",
    "merge_tasks",
    "drop_task",
    "create_followup_tasks",
    "request_human_review",
    "enter_deployment_audit",
}

SUPERVISOR_DECISION_HIGH_RISK_ACTIONS = {"drop_task", "enter_deployment_audit"}
SUPERVISOR_DECISION_HUMAN_REQUIRED_ACTIONS = {
    "drop_task",
    "request_human_review",
    "enter_deployment_audit",
}
SUPERVISOR_DECISION_ROADMAP_REWRITE_ACTIONS = {
    "repair_task_package",
    "split_task",
    "merge_tasks",
    "drop_task",
    "create_followup_tasks",
}
SUPERVISOR_DECISION_UNSUPPORTED_FIELDS = {
    "command",
    "commands",
    "shell_command",
    "shell_commands",
    "file_edits",
    "patch",
    "patches",
    "diff",
    "implementation",
    "execution",
}

_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class SupervisorDecision:
    decision: str
    reason: str
    evidence: tuple[str, ...]
    approved_next_tasks: tuple[str, ...] = ()
    blocked_tasks: tuple[str, ...] = ()
    tasks_to_rewrite: tuple[str, ...] = ()
    followup_tasks: tuple[dict[str, Any], ...] = ()
    requires_human: bool = False
    safety_classification: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_contract(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SUPERVISOR_DECISION_SCHEMA_VERSION,
            "kind": SUPERVISOR_DECISION_KIND,
            "decision": self.decision,
            "approved_next_tasks": list(self.approved_next_tasks),
            "blocked_tasks": list(self.blocked_tasks),
            "tasks_to_rewrite": list(self.tasks_to_rewrite),
            "requires_human": self.requires_human,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "safety_classification": dict(self.safety_classification),
        }
        if self.followup_tasks:
            payload["followup_tasks"] = [dict(item) for item in self.followup_tasks]
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class SupervisorDecisionValidation:
    accepted: bool
    decision: str
    normalized_decision: dict[str, Any]
    errors: tuple[dict[str, str], ...]
    warnings: tuple[dict[str, str], ...]
    safety_classification: dict[str, Any]

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema_version": SUPERVISOR_DECISION_SCHEMA_VERSION,
            "kind": SUPERVISOR_DECISION_VALIDATION_KIND,
            "accepted": self.accepted,
            "status": "accepted" if self.accepted else "rejected",
            "decision": self.decision,
            "error_count": len(self.errors),
            "errors": [dict(item) for item in self.errors],
            "warning_count": len(self.warnings),
            "warnings": [dict(item) for item in self.warnings],
            "safety_classification": dict(self.safety_classification),
            "normalized_decision": dict(self.normalized_decision),
        }


def supervisor_decision_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_supervisor_decision(
    payload: dict[str, Any],
    *,
    project_root: Path | None = None,
    known_task_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    context_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = _validate_supervisor_decision(
        payload,
        project_root=project_root,
        known_task_ids=known_task_ids,
        context_pack=context_pack,
    )
    return validation.as_contract()


def _validate_supervisor_decision(
    payload: dict[str, Any],
    *,
    project_root: Path | None,
    known_task_ids: set[str] | list[str] | tuple[str, ...] | None,
    context_pack: dict[str, Any] | None,
) -> SupervisorDecisionValidation:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        errors.append(_issue("payload", "invalid_type", "supervisor decision payload must be an object"))
        payload = {}

    kind = str(payload.get("kind") or "").strip()
    if kind != SUPERVISOR_DECISION_KIND:
        errors.append(
            _issue("kind", "invalid_kind", f"kind must be {SUPERVISOR_DECISION_KIND}")
        )

    schema_version = payload.get("schema_version")
    if schema_version is not None and schema_version != SUPERVISOR_DECISION_SCHEMA_VERSION:
        errors.append(_issue("schema_version", "invalid_schema_version", "schema_version must be 1 when present"))

    action = str(payload.get("decision") or "").strip()
    if action not in SUPERVISOR_DECISION_ACTIONS:
        errors.append(_issue("decision", "unsupported_action", f"unsupported supervisor action: {action or '<missing>'}"))

    reason = str(payload.get("reason") or "").strip()
    if not reason:
        errors.append(_issue("reason", "missing_reason", "reason must be a non-empty string"))

    requires_human_value = payload.get("requires_human")
    requires_human = requires_human_value is True
    if not isinstance(requires_human_value, bool):
        errors.append(_issue("requires_human", "invalid_requires_human", "requires_human must be a boolean"))

    known = {str(item) for item in known_task_ids or [] if str(item).strip()}
    for field_name in ("approved_next_tasks", "blocked_tasks", "tasks_to_rewrite"):
        if field_name not in payload:
            errors.append(_issue(field_name, "missing_field", f"{field_name} must be declared as a list"))
    approved_next_tasks = _string_list(payload.get("approved_next_tasks"), "approved_next_tasks", errors)
    blocked_tasks = _string_list(payload.get("blocked_tasks"), "blocked_tasks", errors)
    tasks_to_rewrite = _string_list(payload.get("tasks_to_rewrite"), "tasks_to_rewrite", errors)
    _validate_task_references("approved_next_tasks", approved_next_tasks, known, errors)
    _validate_task_references("blocked_tasks", blocked_tasks, known, errors)
    _validate_task_references("tasks_to_rewrite", tasks_to_rewrite, known, errors)

    evidence = _evidence_list(payload.get("evidence"), project_root=project_root, errors=errors)
    followup_tasks = _followup_tasks(payload, known_task_ids=known, errors=errors)
    safety = classify_supervisor_decision(
        action,
        requires_human=requires_human,
        payload=payload,
    )
    _validate_declared_safety(payload.get("safety_classification"), safety, errors=errors, warnings=warnings)
    _validate_context_pack(context_pack, errors=errors, warnings=warnings)
    _validate_unsupported_fields(payload, errors=errors)

    if action == "continue" and not approved_next_tasks:
        errors.append(
            _issue("approved_next_tasks", "missing_approved_next_tasks", "continue requires approved_next_tasks")
        )
    if action == "retry" and not blocked_tasks:
        errors.append(_issue("blocked_tasks", "missing_retry_targets", "retry requires blocked_tasks"))
    if action in {"repair_task_package", "split_task", "merge_tasks", "drop_task"} and not tasks_to_rewrite:
        errors.append(
            _issue("tasks_to_rewrite", "missing_tasks_to_rewrite", f"{action} requires tasks_to_rewrite")
        )
    if action == "create_followup_tasks" and not followup_tasks:
        errors.append(
            _issue("followup_tasks", "missing_followup_tasks", "create_followup_tasks requires followup_tasks")
        )
    if (action in SUPERVISOR_DECISION_HUMAN_REQUIRED_ACTIONS or safety.get("requires_human_by_policy")) and not requires_human:
        errors.append(
            _issue("requires_human", "human_required", f"{action} requires requires_human=true")
        )

    accepted = not errors
    safety["decision_effect"] = "allow" if accepted and not requires_human else "requires_human" if accepted else "reject"
    normalized = SupervisorDecision(
        decision=action,
        reason=reason,
        evidence=tuple(evidence),
        approved_next_tasks=tuple(approved_next_tasks),
        blocked_tasks=tuple(blocked_tasks),
        tasks_to_rewrite=tuple(tasks_to_rewrite),
        followup_tasks=tuple(followup_tasks),
        requires_human=requires_human,
        safety_classification=safety,
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    ).as_contract()

    return SupervisorDecisionValidation(
        accepted=accepted,
        decision=action,
        normalized_decision=normalized,
        errors=tuple(errors),
        warnings=tuple(warnings),
        safety_classification=safety,
    )


def classify_supervisor_decision(
    decision: str,
    *,
    requires_human: bool,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = str(decision or "").strip()
    payload = payload or {}
    unsupported = action not in SUPERVISOR_DECISION_ACTIONS
    high_risk = action in SUPERVISOR_DECISION_HIGH_RISK_ACTIONS
    roadmap_rewrite = action in SUPERVISOR_DECISION_ROADMAP_REWRITE_ACTIONS
    agent_risk = action in {"retry", "repair_task_package"}
    reason = str(payload.get("reason") or "")
    safety_payload = payload.get("safety_classification") if isinstance(payload.get("safety_classification"), dict) else {}
    architecture_risk = bool(safety_payload.get("architecture_risk")) or bool(
        re.search(r"\b(architecture|architectural|goal change|change the goal|scope change)\b", reason, re.IGNORECASE)
    )
    destructive_git_risk = bool(safety_payload.get("destructive_git_risk")) or bool(
        re.search(r"\b(git reset|reset --hard|git clean|force[- ]?push|push --force|checkout --)\b", reason, re.IGNORECASE)
    )
    broad_roadmap_rewrite = bool(safety_payload.get("broad_roadmap_rewrite")) or bool(
        re.search(
            r"\b(broad roadmap rewrite|rewrite the roadmap|replace the roadmap|delete milestone|drop milestone)\b",
            reason,
            re.IGNORECASE,
        )
    )
    deployment_risk = action == "enter_deployment_audit" or bool(safety_payload.get("deployment_risk")) or bool(
        re.search(r"\b(deploy|deployment|release to production)\b", reason, re.IGNORECASE)
    )
    manual_risk = bool(requires_human or action == "request_human_review")
    secret_risk = bool(safety_payload.get("secret_risk")) or bool(
        re.search(r"\b(secret|credential|token|api[-_ ]?key)\b", reason, re.IGNORECASE)
    )
    network_risk = bool(safety_payload.get("network_risk")) or bool(
        re.search(r"\b(network|remote|internet|webhook|http|https)\b", reason, re.IGNORECASE)
    )
    live_risk = deployment_risk or bool(safety_payload.get("live_risk")) or bool(
        re.search(r"\b(live|production|prod|mainnet)\b", reason, re.IGNORECASE)
    )
    risk_flags = {
        "agent": agent_risk,
        "manual": manual_risk,
        "live": live_risk,
        "deployment": deployment_risk,
        "secret": secret_risk,
        "network": network_risk,
        "filesystem": roadmap_rewrite,
        "architecture": architecture_risk,
        "destructive_git": destructive_git_risk,
        "roadmap": broad_roadmap_rewrite,
    }
    risk_categories = sorted(name for name, enabled in risk_flags.items() if enabled)
    requires_human_by_policy = bool(
        action in SUPERVISOR_DECISION_HUMAN_REQUIRED_ACTIONS
        or high_risk
        or deployment_risk
        or secret_risk
        or live_risk
        or architecture_risk
        or destructive_git_risk
        or broad_roadmap_rewrite
    )
    risk_level = (
        "high"
        if requires_human_by_policy
        else "medium"
        if roadmap_rewrite
        else "low"
    )
    auto_apply_allowed = action in {"continue", "pause", "retry"} and not requires_human and risk_level == "low"
    return {
        "schema_version": SUPERVISOR_DECISION_SCHEMA_VERSION,
        "kind": "engineering-orchestrator.supervisor-decision-safety.v1",
        "decision": action,
        "unsupported_action": unsupported,
        "high_risk_action": high_risk,
        "requires_human": bool(requires_human),
        "requires_human_by_policy": requires_human_by_policy,
        "risk_level": risk_level,
        "risk_categories": risk_categories,
        "risk_flags": risk_flags,
        "auto_apply_allowed": auto_apply_allowed,
        "roadmap_rewrite": roadmap_rewrite,
        "supported_actions": sorted(SUPERVISOR_DECISION_ACTIONS),
    }


def _issue(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def _string_list(value: Any, field: str, errors: list[dict[str, str]]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_issue(field, "invalid_type", f"{field} must be a list of task ids"))
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = str(item).strip() if isinstance(item, str) else ""
        location = f"{field}[{index}]"
        if not text:
            errors.append(_issue(location, "invalid_task_id", "task id must be a non-empty string"))
            continue
        if not _TASK_ID_RE.fullmatch(text):
            errors.append(_issue(location, "invalid_task_id", f"task id has unsupported characters: {text}"))
            continue
        if text in seen:
            errors.append(_issue(location, "duplicate_task_id", f"duplicate task id: {text}"))
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _validate_task_references(
    field: str,
    values: list[str],
    known_task_ids: set[str],
    errors: list[dict[str, str]],
) -> None:
    if not known_task_ids:
        return
    for value in values:
        if value not in known_task_ids:
            errors.append(_issue(field, "unknown_task_id", f"unknown task id: {value}"))


def _evidence_list(value: Any, *, project_root: Path | None, errors: list[dict[str, str]]) -> list[str]:
    if not isinstance(value, list):
        errors.append(_issue("evidence", "missing_evidence", "evidence must be a non-empty list of local paths"))
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        raw = item
        if isinstance(item, dict):
            raw = item.get("path")
        text = str(raw).strip() if isinstance(raw, str) else ""
        location = f"evidence[{index}]"
        if not text:
            errors.append(_issue(location, "invalid_evidence", "evidence item must be a non-empty local path"))
            continue
        normalized_path = _normalize_local_evidence_path(text, project_root=project_root, location=location, errors=errors)
        if not normalized_path:
            continue
        if normalized_path in seen:
            errors.append(_issue(location, "duplicate_evidence", f"duplicate evidence path: {normalized_path}"))
            continue
        seen.add(normalized_path)
        normalized.append(normalized_path)
    if not normalized:
        errors.append(_issue("evidence", "missing_evidence", "at least one valid local evidence path is required"))
    return normalized


def _normalize_local_evidence_path(
    value: str,
    *,
    project_root: Path | None,
    location: str,
    errors: list[dict[str, str]],
) -> str | None:
    if _URI_RE.match(value):
        errors.append(_issue(location, "remote_evidence", "evidence must be a local path, not a URI"))
        return None
    path = Path(value)
    if project_root is None:
        if path.is_absolute() or ".." in path.parts:
            errors.append(_issue(location, "unsafe_evidence_path", "evidence path must be relative when no project root is supplied"))
            return None
        return path.as_posix()

    root = project_root.resolve()
    candidate = path if path.is_absolute() else project_root / path
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        errors.append(_issue(location, "invalid_evidence_path", f"could not resolve evidence path: {value}"))
        return None
    if resolved != root and root not in resolved.parents:
        errors.append(_issue(location, "evidence_outside_project", "evidence path must stay inside the project root"))
        return None
    if not resolved.exists():
        errors.append(_issue(location, "missing_evidence_path", f"evidence path does not exist: {value}"))
        return None
    return resolved.relative_to(root).as_posix()


def _followup_tasks(
    payload: dict[str, Any],
    *,
    known_task_ids: set[str],
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    value = payload.get("followup_tasks")
    if value is None:
        value = payload.get("tasks_to_create")
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_issue("followup_tasks", "invalid_type", "followup_tasks must be a list"))
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        location = f"followup_tasks[{index}]"
        if not isinstance(item, dict):
            errors.append(_issue(location, "invalid_followup_task", "followup task must be an object"))
            continue
        task_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not task_id or not _TASK_ID_RE.fullmatch(task_id):
            errors.append(_issue(f"{location}.id", "invalid_task_id", "followup task id must be a non-empty task id"))
            continue
        if task_id in seen:
            errors.append(_issue(f"{location}.id", "duplicate_task_id", f"duplicate followup task id: {task_id}"))
            continue
        if task_id in known_task_ids:
            errors.append(_issue(f"{location}.id", "existing_task_id", f"followup task id already exists: {task_id}"))
            continue
        if not title:
            errors.append(_issue(f"{location}.title", "missing_title", "followup task title is required"))
            continue
        seen.add(task_id)
        normalized.append(
            {
                key: item[key]
                for key in ("id", "title", "reason", "spec_refs", "file_scope")
                if key in item
            }
        )
    return normalized


def _validate_declared_safety(
    declared: Any,
    computed: dict[str, Any],
    *,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    if declared is None:
        return
    if not isinstance(declared, dict):
        errors.append(_issue("safety_classification", "invalid_type", "safety_classification must be an object"))
        return
    declared_level = str(declared.get("risk_level") or "").strip().lower()
    computed_level = str(computed.get("risk_level") or "low")
    if declared_level and declared_level in _RISK_ORDER and _RISK_ORDER[declared_level] < _RISK_ORDER[computed_level]:
        errors.append(
            _issue(
                "safety_classification.risk_level",
                "understated_risk",
                f"declared risk_level {declared_level} is lower than computed {computed_level}",
            )
        )
    elif declared_level and declared_level not in _RISK_ORDER:
        warnings.append(
            _issue("safety_classification.risk_level", "unknown_risk_level", f"unknown risk_level: {declared_level}")
        )
    if declared.get("requires_human_by_policy") is False and computed.get("requires_human_by_policy"):
        errors.append(
            _issue(
                "safety_classification.requires_human_by_policy",
                "understated_human_gate",
                "declared safety cannot clear a required human gate",
            )
        )


def _validate_context_pack(
    context_pack: dict[str, Any] | None,
    *,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    if context_pack is None:
        return
    if not isinstance(context_pack, dict):
        errors.append(_issue("context_pack", "invalid_type", "context pack must be an object when supplied"))
        return
    if context_pack.get("kind") != "engineering-harness.supervisor-context-pack":
        warnings.append(_issue("context_pack.kind", "unexpected_context_kind", "context pack kind is not supervisor-context-pack"))
    if context_pack.get("local_only") is not True:
        errors.append(_issue("context_pack.local_only", "non_local_context", "supervisor context pack must be local_only=true"))


def _validate_unsupported_fields(payload: dict[str, Any], *, errors: list[dict[str, str]]) -> None:
    for field_name in sorted(SUPERVISOR_DECISION_UNSUPPORTED_FIELDS):
        value = payload.get(field_name)
        if value in (None, "", [], {}):
            continue
        errors.append(
            _issue(
                field_name,
                "unsupported_supervisor_output",
                "supervisor decisions cannot include worker commands, patches, or implementation edits",
            )
        )

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import append_jsonl, load_mapping, write_mapping
from .spec_sync import SPEC_TASKS_RELATIVE_PATH


DOCS_UPDATE_LOG_RELATIVE_PATH = "docs/docs_update_log.jsonl"
DOCS_SYNC_AUDIT_KIND = "engineering-harness.docs-sync-audit.v1"
DOCS_SYNC_PLAN_KIND = "engineering-harness.docs-sync-plan.v1"
DOCS_SYNC_UPDATE_KIND = "engineering-harness.docs-sync-update.v1"

DOCUMENTATION_ROLE_ALIASES = {
    "architecture": "architecture_blueprint",
    "architecture_blueprint": "architecture_blueprint",
    "roadmap": "roadmap",
    "roadmap_status": "roadmap_status",
    "roadmap_status_table": "roadmap_status",
    "status_doc": "development_progress",
    "implementation_status": "development_progress",
    "development_progress": "development_progress",
    "actual_system_state": "actual_system_state",
    "system_state": "actual_system_state",
    "canonical_spec": "canonical_specs",
    "canonical_specs": "canonical_specs",
    "source_spec": "canonical_specs",
    "traceability": "traceability",
    "traceability_documents": "traceability",
    "task_package": "task_packages",
    "task_packages": "task_packages",
    "deployment": "deployment_status",
    "deployment_status": "deployment_status",
    "decision_log": "decision_log_dir",
    "decision_log_dir": "decision_log_dir",
}

FILE_ROLES = {
    "architecture_blueprint",
    "roadmap",
    "roadmap_status",
    "development_progress",
    "actual_system_state",
    "canonical_specs",
    "traceability",
    "task_packages",
    "deployment_status",
}
DIRECTORY_ROLES = {"decision_log_dir"}
EXPECTED_DOCUMENTATION_ROLES = FILE_ROLES | DIRECTORY_ROLES
STATUS_ROLES = {
    "roadmap_status",
    "development_progress",
    "actual_system_state",
    "traceability",
    "task_packages",
}
AUTO_APPLY_FILE_ROLES = {
    "roadmap_status",
    "development_progress",
    "actual_system_state",
    "traceability",
    "task_packages",
    "deployment_status",
}
MANUAL_REVIEW_ROLES = {
    "architecture_blueprint",
    "roadmap",
    "canonical_specs",
    "decision_log_dir",
}
STATUS_WORDS = {
    "pending": "pending",
    "planned": "planned",
    "active": "active",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "partial": "partial",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "passed": "completed",
    "blocked": "blocked",
    "failed": "failed",
    "skipped": "skipped",
    "deployed": "deployed",
    "not deployed": "not_deployed",
    "not_deployed": "not_deployed",
    "local only": "local_only",
    "local_only": "local_only",
}
MANAGED_BLOCK_START = "<!-- engineering-orchestrator:docs-sync:start -->"
MANAGED_BLOCK_END = "<!-- engineering-orchestrator:docs-sync:end -->"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def audit_documentation_system(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    tasks_path = root / SPEC_TASKS_RELATIVE_PATH
    checks: list[dict[str, Any]] = []

    def add_check(name: str, status: str, message: str, **metadata: Any) -> None:
        check = {"name": name, "status": status, "message": message}
        check.update({key: value for key, value in metadata.items() if value not in (None, [], {})})
        checks.append(check)

    if not tasks_path.exists():
        add_check("spec_tasks", "error", f"missing {SPEC_TASKS_RELATIVE_PATH}", path=SPEC_TASKS_RELATIVE_PATH)
        return _audit_payload(root, checks, spec_tasks_path=tasks_path, documentation={})

    try:
        payload = load_mapping(tasks_path)
    except Exception as exc:
        add_check("spec_tasks_parse", "error", str(exc), path=SPEC_TASKS_RELATIVE_PATH)
        return _audit_payload(root, checks, spec_tasks_path=tasks_path, documentation={})

    add_check("spec_tasks", "passed", "spec task ledger exists", path=SPEC_TASKS_RELATIVE_PATH)
    documentation = documentation_roles(payload)
    tasks = _mapping_list(payload.get("tasks"))
    task_ids = _task_match_ids(tasks)
    completed_tasks = [
        task
        for task in tasks
        if _normalize_status(str(task.get("status") or "")) == "completed"
    ]

    if not documentation:
        add_check("documentation_roles", "warning", "no documentation roles are declared")
    else:
        add_check("documentation_roles", "passed", f"{len(documentation)} documentation role(s) declared")

    for role in sorted(EXPECTED_DOCUMENTATION_ROLES - set(documentation)):
        add_check(role, "warning", "documentation role is not declared")

    role_texts: dict[str, list[tuple[str, str]]] = {}
    for role, values in sorted(documentation.items()):
        if role in DIRECTORY_ROLES:
            for value in values:
                path = root / value
                if path.is_dir():
                    add_check(role, "passed", "directory exists", path=value)
                    if not any(path.iterdir()):
                        add_check(f"{role}_empty", "warning", "decision log directory is empty", path=value)
                else:
                    add_check(role, "warning", "directory is missing", path=value)
            continue
        if role not in FILE_ROLES:
            add_check(role, "warning", "unknown documentation role", paths=values)
            continue
        for value in values:
            path = root / value
            if path.is_file():
                add_check(role, "passed", "file exists", path=value)
                role_texts.setdefault(role, []).append((value, path.read_text(encoding="utf-8", errors="ignore")))
            else:
                add_check(role, "error", "file is missing", path=value)

    _audit_status_links(checks, role_texts, tasks, task_ids)
    _audit_task_package_consistency(checks, role_texts, tasks)
    _audit_deployment_separation(checks, documentation, role_texts, completed_tasks)

    return _audit_payload(root, checks, spec_tasks_path=tasks_path, documentation=documentation, payload=payload)


def propose_documentation_update(
    project_root: Path,
    *,
    task_id: str,
    status: str,
    evidence: list[str] | None = None,
    requirement_ids: list[str] | None = None,
    note: str = "",
    actor: str = "engineering-harness",
    phase: str = "",
    stage_id: str = "",
    architecture_change: bool = False,
) -> dict[str, Any]:
    root = project_root.resolve()
    tasks_path = root / SPEC_TASKS_RELATIVE_PATH
    if not tasks_path.exists():
        return _skipped_plan(root, task_id, "spec_tasks_not_configured", f"missing {SPEC_TASKS_RELATIVE_PATH}")

    payload = load_mapping(tasks_path)
    documentation = documentation_roles(payload)
    if not documentation:
        return _skipped_plan(root, task_id, "documentation_roles_not_configured", "no documentation roles are declared")

    tasks = _mapping_list(payload.get("tasks"))
    task = find_task_record(tasks, task_id)
    if task is None:
        return _skipped_plan(
            root,
            task_id,
            "task_not_tracked_in_spec_tasks",
            f"spec task not found for task id or roadmap_task_id: {task_id}",
            documentation=documentation,
        )

    evidence_values = _string_list(evidence)
    normalized_status = _normalize_status(status)
    if normalized_status == "completed" and not evidence_values:
        return _blocked_plan(
            root,
            payload,
            documentation,
            task,
            task_id,
            status,
            reason="completion_evidence_required",
            message="documentation sync cannot mark a task complete without local evidence",
            evidence=evidence_values,
            requirement_ids=requirement_ids,
            note=note,
            actor=actor,
            phase=phase,
            stage_id=stage_id,
        )

    return _build_update_plan(
        root,
        payload,
        documentation,
        task,
        requested_task_id=task_id,
        status=status,
        evidence=evidence_values,
        requirement_ids=requirement_ids,
        note=note,
        actor=actor,
        phase=phase,
        stage_id=stage_id,
        architecture_change=architecture_change,
    )


def record_documentation_update(
    project_root: Path,
    *,
    task_id: str,
    status: str,
    evidence: list[str] | None = None,
    requirement_ids: list[str] | None = None,
    note: str = "",
    actor: str = "engineering-harness",
    phase: str = "",
    stage_id: str = "",
    architecture_change: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    plan = propose_documentation_update(
        project_root,
        task_id=task_id,
        status=status,
        evidence=evidence,
        requirement_ids=requirement_ids,
        note=note,
        actor=actor,
        phase=phase,
        stage_id=stage_id,
        architecture_change=architecture_change,
    )
    if not apply or plan.get("status") in {"skipped", "blocked"}:
        return plan

    root = project_root.resolve()
    payload = load_mapping(root / SPEC_TASKS_RELATIVE_PATH)
    tasks = _mapping_list(payload.get("tasks"))
    task = find_task_record(tasks, task_id)
    if task is None:
        return _skipped_plan(
            root,
            task_id,
            "task_not_tracked_in_spec_tasks",
            f"spec task not found for task id or roadmap_task_id: {task_id}",
            documentation=documentation_roles(payload),
        )

    now = str(plan.get("updated_at") or utc_now_iso())
    applied_actions: list[dict[str, Any]] = []
    skipped_actions: list[dict[str, Any]] = []
    for action in plan.get("actions", []):
        if not isinstance(action, dict):
            continue
        if not bool(action.get("applyable")):
            skipped_actions.append(action)
            continue
        if action.get("action") != "update_managed_status_block":
            skipped_actions.append(action)
            continue
        path_value = str(action.get("path") or "")
        if not path_value:
            skipped_actions.append(action)
            continue
        path = root / path_value
        if not path.is_file():
            skipped_actions.append({**action, "blocked_reason": "target_doc_missing"})
            continue
        _upsert_managed_block(path, _managed_block_entry(plan, action, now))
        applied_actions.append(action)

    docs_evidence_entries = [
        {
            "kind": "record",
            "value": item,
            "recorded_at": now,
            "actor": actor,
        }
        for item in _string_list(evidence)
    ]
    task["documentation_sync"] = {
        "status": "applied",
        "updated_at": now,
        "requested_task_id": task_id,
        "new_status": status,
        "docs_update_log": DOCS_UPDATE_LOG_RELATIVE_PATH,
        "updated_paths": _unique([str(action.get("path")) for action in applied_actions if action.get("path")]),
        "manual_review_paths": _unique(
            [str(action.get("path")) for action in skipped_actions if action.get("path")]
        ),
    }
    if docs_evidence_entries:
        existing = _mapping_list(task.get("documentation_evidence"))
        task["documentation_evidence"] = [*existing, *docs_evidence_entries]
    payload["updated_at"] = now
    payload["tasks"] = tasks
    write_mapping(root / SPEC_TASKS_RELATIVE_PATH, payload)

    update_record = {
        "kind": DOCS_SYNC_UPDATE_KIND,
        "updated_at": now,
        "project": plan.get("project"),
        "task_id": plan.get("task_id"),
        "requested_task_id": task_id,
        "stage_id": stage_id,
        "phase": phase,
        "new_status": status,
        "requirement_ids": plan.get("requirement_ids", []),
        "evidence": docs_evidence_entries,
        "note": note,
        "actor": actor,
        "actions": applied_actions,
        "skipped_actions": skipped_actions,
        "docs_update_log": DOCS_UPDATE_LOG_RELATIVE_PATH,
    }
    update_record = {key: value for key, value in update_record.items() if value not in ("", [], None)}
    append_jsonl(root / DOCS_UPDATE_LOG_RELATIVE_PATH, update_record)

    result = dict(plan)
    result["kind"] = DOCS_SYNC_UPDATE_KIND
    result["status"] = "applied"
    result["applied_action_count"] = len(applied_actions)
    result["skipped_action_count"] = len(skipped_actions)
    result["applied_actions"] = applied_actions
    result["skipped_actions"] = skipped_actions
    result["updated_paths"] = _unique([str(action.get("path")) for action in applied_actions if action.get("path")])
    result["docs_update_log"] = DOCS_UPDATE_LOG_RELATIVE_PATH
    return result


def documentation_roles(payload: dict[str, Any]) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}

    documentation = payload.get("documentation")
    if isinstance(documentation, dict):
        for raw_role, raw_value in documentation.items():
            role = DOCUMENTATION_ROLE_ALIASES.get(str(raw_role).strip(), str(raw_role).strip())
            for value in _string_list(raw_value):
                roles.setdefault(role, [])
                if value not in roles[role]:
                    roles[role].append(value)

    top_level_sources = {
        "source_spec": "canonical_specs",
        "status_doc": "development_progress",
        "roadmap_doc": "roadmap",
        "roadmap_status_doc": "roadmap_status",
        "decision_log_dir": "decision_log_dir",
    }
    for source_key, role in top_level_sources.items():
        for value in _string_list(payload.get(source_key)):
            roles.setdefault(role, [])
            if value not in roles[role]:
                roles[role].append(value)

    return {role: values for role, values in roles.items() if values}


def has_documentation_roles(project_root: Path) -> bool:
    tasks_path = project_root.resolve() / SPEC_TASKS_RELATIVE_PATH
    if not tasks_path.exists():
        return False
    try:
        payload = load_mapping(tasks_path)
    except Exception:
        return False
    documentation = payload.get("documentation")
    if isinstance(documentation, dict):
        return any(_string_list(value) for value in documentation.values())
    return any(_string_list(payload.get(key)) for key in ("roadmap_doc", "roadmap_status_doc"))


def find_task_record(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    task_id = str(task_id)
    for task in tasks:
        if str(task.get("id") or "") == task_id:
            return task
    for task in tasks:
        if str(task.get("roadmap_task_id") or "") == task_id:
            return task
    return None


def _build_update_plan(
    root: Path,
    payload: dict[str, Any],
    documentation: dict[str, list[str]],
    task: dict[str, Any],
    *,
    requested_task_id: str,
    status: str,
    evidence: list[str],
    requirement_ids: list[str] | None,
    note: str,
    actor: str,
    phase: str,
    stage_id: str,
    architecture_change: bool,
) -> dict[str, Any]:
    now = utc_now_iso()
    task_id = str(task.get("id") or requested_task_id)
    accepted_requirement_ids = _unique([*(_string_list(task.get("requirement_ids"))), *_string_list(requirement_ids)])
    actions: list[dict[str, Any]] = []

    for role, paths in sorted(documentation.items()):
        for path_value in paths:
            path = root / path_value
            exists = path.is_dir() if role in DIRECTORY_ROLES else path.is_file()
            if role in AUTO_APPLY_FILE_ROLES:
                actions.append(
                    {
                        "role": role,
                        "path": path_value,
                        "exists": exists,
                        "action": "update_managed_status_block",
                        "applyable": bool(exists),
                        "safety_classification": "low",
                        "reason": "append or update an Engineering Orchestrator managed evidence block",
                        "blocked_reason": None if exists else "target_doc_missing",
                    }
                )
            elif role in MANUAL_REVIEW_ROLES:
                reason = "architecture updates require explicit review" if role == "architecture_blueprint" else "role is review-only"
                if role == "architecture_blueprint" and architecture_change:
                    reason = "task declares an architecture change; update remains manual-review only"
                actions.append(
                    {
                        "role": role,
                        "path": path_value,
                        "exists": exists,
                        "action": "manual_review",
                        "applyable": False,
                        "safety_classification": "manual_review",
                        "reason": reason,
                    }
                )
            else:
                actions.append(
                    {
                        "role": role,
                        "path": path_value,
                        "exists": exists,
                        "action": "manual_review",
                        "applyable": False,
                        "safety_classification": "unknown_role",
                        "reason": "unknown documentation role",
                    }
                )

    applyable_count = sum(1 for action in actions if action.get("applyable"))
    blocked_missing = [action for action in actions if action.get("blocked_reason")]
    plan_status = "proposed" if applyable_count or actions else "blocked"
    plan_reason = None if plan_status == "proposed" else "no_applyable_documentation_updates"
    return {
        "kind": DOCS_SYNC_PLAN_KIND,
        "status": plan_status,
        "reason": plan_reason,
        "project": payload.get("project") or root.name,
        "root": str(root),
        "task_id": task_id,
        "requested_task_id": requested_task_id,
        "task_title": task.get("title"),
        "new_status": status,
        "updated_at": now,
        "stage_id": stage_id,
        "phase": phase,
        "requirement_ids": accepted_requirement_ids,
        "evidence": _evidence_entries(evidence, now, actor),
        "note": note,
        "actor": actor,
        "documentation_roles": documentation,
        "docs_update_log": DOCS_UPDATE_LOG_RELATIVE_PATH,
        "actions": actions,
        "action_count": len(actions),
        "applyable_action_count": applyable_count,
        "blocked_action_count": len(blocked_missing),
        "manual_review_action_count": sum(1 for action in actions if action.get("safety_classification") == "manual_review"),
        "architecture_change": architecture_change,
    }


def _blocked_plan(
    root: Path,
    payload: dict[str, Any],
    documentation: dict[str, list[str]],
    task: dict[str, Any],
    requested_task_id: str,
    status: str,
    *,
    reason: str,
    message: str,
    evidence: list[str],
    requirement_ids: list[str] | None,
    note: str,
    actor: str,
    phase: str,
    stage_id: str,
) -> dict[str, Any]:
    now = utc_now_iso()
    task_id = str(task.get("id") or requested_task_id)
    return {
        "kind": DOCS_SYNC_PLAN_KIND,
        "status": "blocked",
        "reason": reason,
        "message": message,
        "project": payload.get("project") or root.name,
        "root": str(root),
        "task_id": task_id,
        "requested_task_id": requested_task_id,
        "new_status": status,
        "updated_at": now,
        "stage_id": stage_id,
        "phase": phase,
        "requirement_ids": _unique([*(_string_list(task.get("requirement_ids"))), *_string_list(requirement_ids)]),
        "evidence": _evidence_entries(evidence, now, actor),
        "note": note,
        "actor": actor,
        "documentation_roles": documentation,
        "docs_update_log": DOCS_UPDATE_LOG_RELATIVE_PATH,
        "actions": [],
        "action_count": 0,
        "applyable_action_count": 0,
    }


def _skipped_plan(
    root: Path,
    task_id: str,
    reason: str,
    message: str,
    *,
    documentation: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": DOCS_SYNC_PLAN_KIND,
        "status": "skipped",
        "reason": reason,
        "message": message,
        "project": root.name,
        "root": str(root),
        "task_id": task_id,
        "requested_task_id": task_id,
        "updated_at": utc_now_iso(),
        "documentation_roles": documentation or {},
        "actions": [],
        "action_count": 0,
        "applyable_action_count": 0,
    }


def _audit_status_links(
    checks: list[dict[str, Any]],
    role_texts: dict[str, list[tuple[str, str]]],
    tasks: list[dict[str, Any]],
    task_ids: dict[str, list[str]],
) -> None:
    for role in sorted(STATUS_ROLES & set(role_texts)):
        missing: list[dict[str, str]] = []
        stale: list[dict[str, str]] = []
        for task in tasks:
            expected = _normalize_status(str(task.get("status") or ""))
            identifiers = task_ids.get(str(task.get("id") or ""), [])
            if not identifiers:
                continue
            mentions = _role_mentions(role_texts.get(role, []), identifiers)
            if not mentions:
                missing.append({"task_id": str(task.get("id") or ""), "role": role})
                continue
            found_statuses = [
                status
                for _, line in mentions
                for status in [_line_status(line)]
                if status
            ]
            if expected and found_statuses and expected not in found_statuses:
                stale.append(
                    {
                        "task_id": str(task.get("id") or ""),
                        "role": role,
                        "ledger_status": expected,
                        "doc_status": ",".join(_unique(found_statuses)),
                    }
                )
        if missing:
            checks.append(
                {
                    "name": f"{role}_status_links",
                    "status": "warning",
                    "message": "status document does not mention every tracked task",
                    "missing": missing[:50],
                }
            )
        elif tasks:
            checks.append(
                {
                    "name": f"{role}_status_links",
                    "status": "passed",
                    "message": "tracked tasks are mentioned",
                }
            )
        if stale:
            checks.append(
                {
                    "name": f"{role}_stale_status_links",
                    "status": "warning",
                    "message": "documentation status appears stale relative to the task ledger",
                    "stale": stale[:50],
                }
            )


def _audit_task_package_consistency(
    checks: list[dict[str, Any]],
    role_texts: dict[str, list[tuple[str, str]]],
    tasks: list[dict[str, Any]],
) -> None:
    status_docs = [*role_texts.get("roadmap_status", []), *role_texts.get("development_progress", [])]
    package_docs = role_texts.get("task_packages", [])
    if not status_docs or not package_docs:
        checks.append(
            {
                "name": "task_package_status_consistency",
                "status": "warning",
                "message": "roadmap/development status and task package docs are not both declared",
            }
        )
        return
    inconsistent: list[dict[str, str]] = []
    for task in tasks:
        identifiers = _task_identifiers(task)
        status_mentions = [_line_status(line) for _, line in _role_mentions(status_docs, identifiers)]
        package_mentions = [_line_status(line) for _, line in _role_mentions(package_docs, identifiers)]
        status_values = [item for item in status_mentions if item]
        package_values = [item for item in package_mentions if item]
        if status_values and package_values and status_values[0] != package_values[0]:
            inconsistent.append(
                {
                    "task_id": str(task.get("id") or ""),
                    "status_doc": status_values[0],
                    "task_package": package_values[0],
                }
            )
    if inconsistent:
        checks.append(
            {
                "name": "task_package_status_consistency",
                "status": "warning",
                "message": "task package status differs from roadmap/development status",
                "inconsistent": inconsistent[:50],
            }
        )
    else:
        checks.append(
            {
                "name": "task_package_status_consistency",
                "status": "passed",
                "message": "task package statuses are consistent where declared",
            }
        )


def _audit_deployment_separation(
    checks: list[dict[str, Any]],
    documentation: dict[str, list[str]],
    role_texts: dict[str, list[tuple[str, str]]],
    completed_tasks: list[dict[str, Any]],
) -> None:
    deployment_paths = set(documentation.get("deployment_status", []))
    local_roles = {"development_progress", "actual_system_state", "roadmap_status"}
    local_paths = {
        path
        for role in local_roles
        for path in documentation.get(role, [])
    }
    if completed_tasks and not deployment_paths:
        checks.append(
            {
                "name": "deployment_status",
                "status": "warning",
                "message": "completed local work exists but no deployment status document is declared",
            }
        )
        return
    if deployment_paths & local_paths:
        checks.append(
            {
                "name": "local_deployment_status_separation",
                "status": "warning",
                "message": "deployment status shares a document with local implementation status",
                "paths": sorted(deployment_paths & local_paths),
            }
        )
    elif deployment_paths:
        checks.append(
            {
                "name": "local_deployment_status_separation",
                "status": "passed",
                "message": "deployment status is documented separately from local implementation status",
            }
        )
    deployment_text = "\n".join(text for _, text in role_texts.get("deployment_status", []))
    if deployment_paths and completed_tasks and not re.search(r"\b(deployed|not deployed|local only|local-only)\b", deployment_text, re.I):
        checks.append(
            {
                "name": "deployment_status_ambiguity",
                "status": "warning",
                "message": "deployment status document does not clearly distinguish local completion from deployment",
            }
        )
    elif deployment_paths:
        checks.append(
            {
                "name": "deployment_status_ambiguity",
                "status": "passed",
                "message": "deployment status distinguishes local completion from deployment",
            }
        )


def _audit_payload(
    root: Path,
    checks: list[dict[str, Any]],
    *,
    spec_tasks_path: Path,
    documentation: dict[str, list[str]],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_count = sum(1 for check in checks if check.get("status") == "error")
    warning_count = sum(1 for check in checks if check.get("status") == "warning")
    status = "failed" if error_count else "warning" if warning_count else "passed"
    tasks = _mapping_list((payload or {}).get("tasks"))
    return {
        "kind": DOCS_SYNC_AUDIT_KIND,
        "status": status,
        "project": (payload or {}).get("project") or root.name,
        "root": str(root),
        "spec_tasks_path": _relative(root, spec_tasks_path),
        "checked_at": utc_now_iso(),
        "error_count": error_count,
        "warning_count": warning_count,
        "task_count": len(tasks),
        "documentation_roles": documentation,
        "documentation_role_count": len(documentation),
        "checks": checks,
    }


def _managed_block_entry(plan: dict[str, Any], action: dict[str, Any], now: str) -> dict[str, str]:
    evidence_values = [
        str(item.get("value") or "")
        for item in plan.get("evidence", [])
        if isinstance(item, dict) and str(item.get("value") or "").strip()
    ]
    evidence_text = "; ".join(evidence_values[:3])
    if len(evidence_text) > 180:
        evidence_text = evidence_text[:177] + "..."
    return {
        "task_id": str(plan.get("task_id") or ""),
        "requested_task_id": str(plan.get("requested_task_id") or plan.get("task_id") or ""),
        "role": str(action.get("role") or ""),
        "status": str(plan.get("new_status") or ""),
        "updated_at": now,
        "evidence": evidence_text,
    }


def _upsert_managed_block(path: Path, entry: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    table = _managed_block_table(text, entry)
    block = "\n".join(
        [
            MANAGED_BLOCK_START,
            "## Engineering Orchestrator Documentation Sync",
            "",
            "| Task | Requested Task | Role | Status | Updated | Evidence |",
            "| --- | --- | --- | --- | --- | --- |",
            *table,
            MANAGED_BLOCK_END,
        ]
    )
    pattern = re.compile(
        rf"{re.escape(MANAGED_BLOCK_START)}.*?{re.escape(MANAGED_BLOCK_END)}",
        re.S,
    )
    if pattern.search(text):
        updated = pattern.sub(block, text)
    else:
        separator = "\n\n" if text.rstrip() else ""
        updated = f"{text.rstrip()}{separator}{block}\n"
    path.write_text(updated if updated.endswith("\n") else updated + "\n", encoding="utf-8")


def _managed_block_table(text: str, entry: dict[str, str]) -> list[str]:
    rows: dict[tuple[str, str], str] = {}
    in_block = False
    for line in text.splitlines():
        if line.strip() == MANAGED_BLOCK_START:
            in_block = True
            continue
        if line.strip() == MANAGED_BLOCK_END:
            in_block = False
            continue
        if not in_block or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] in {"Task", "---"}:
            continue
        rows[(cells[0], cells[2])] = line
    task = _escape_table_cell(entry["task_id"])
    role = _escape_table_cell(entry["role"])
    row = (
        f"| {task} | {_escape_table_cell(entry['requested_task_id'])} | {role} | "
        f"{_escape_table_cell(entry['status'])} | {_escape_table_cell(entry['updated_at'])} | "
        f"{_escape_table_cell(entry['evidence'])} |"
    )
    rows[(task, role)] = row
    return [rows[key] for key in sorted(rows)]


def _role_mentions(role_texts: list[tuple[str, str]], identifiers: list[str]) -> list[tuple[str, str]]:
    mentions: list[tuple[str, str]] = []
    for path, text in role_texts:
        for line in text.splitlines():
            if any(identifier and identifier in line for identifier in identifiers):
                mentions.append((path, line))
    return mentions


def _task_match_ids(tasks: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        str(task.get("id") or ""): _task_identifiers(task)
        for task in tasks
        if str(task.get("id") or "").strip()
    }


def _task_identifiers(task: dict[str, Any]) -> list[str]:
    aliases = [str(item).strip() for item in task.get("aliases", []) if str(item).strip()] if isinstance(task.get("aliases"), list) else []
    return _unique(
        [
            str(task.get("id") or "").strip(),
            str(task.get("roadmap_task_id") or "").strip(),
            *aliases,
        ]
    )


def _line_status(line: str) -> str:
    lower = line.lower().replace("-", " ")
    for raw, normalized in sorted(STATUS_WORDS.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = r"(?<![a-z0-9_])" + re.escape(raw.replace("_", " ")) + r"(?![a-z0-9_])"
        if re.search(pattern, lower):
            return normalized
    return ""


def _normalize_status(value: str) -> str:
    return STATUS_WORDS.get(value.strip().lower().replace("-", " "), value.strip().lower())


def _evidence_entries(evidence: list[str], now: str, actor: str) -> list[dict[str, str]]:
    return [
        {
            "kind": "record",
            "value": item,
            "recorded_at": now,
            "actor": actor,
        }
        for item in _string_list(evidence)
    ]


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _escape_table_cell(value: str) -> str:
    return str(value).replace("|", "/").replace("\n", " ")

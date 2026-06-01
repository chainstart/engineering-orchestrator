from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import append_jsonl, load_mapping, write_mapping


SPEC_TASKS_RELATIVE_PATH = ".engineering/spec_tasks.yaml"
SPEC_UPDATE_LOG_RELATIVE_PATH = "docs/spec_update_log.jsonl"
SPEC_SYNC_AUDIT_KIND = "engineering-harness.spec-sync-audit.v1"
SPEC_SYNC_UPDATE_KIND = "engineering-harness.spec-sync-update.v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def audit_spec_system(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    tasks_path = root / SPEC_TASKS_RELATIVE_PATH
    checks: list[dict[str, Any]] = []

    def add_check(name: str, status: str, message: str, **metadata: Any) -> None:
        check = {"name": name, "status": status, "message": message}
        check.update({key: value for key, value in metadata.items() if value is not None})
        checks.append(check)

    if not tasks_path.exists():
        add_check(
            "spec_tasks",
            "error",
            f"missing {SPEC_TASKS_RELATIVE_PATH}",
            path=SPEC_TASKS_RELATIVE_PATH,
        )
        return _audit_payload(root, checks, spec_tasks_path=tasks_path)

    try:
        payload = load_mapping(tasks_path)
    except Exception as exc:
        add_check("spec_tasks_parse", "error", str(exc), path=SPEC_TASKS_RELATIVE_PATH)
        return _audit_payload(root, checks, spec_tasks_path=tasks_path)

    add_check("spec_tasks", "passed", "spec task ledger exists", path=SPEC_TASKS_RELATIVE_PATH)

    source_spec = str(payload.get("source_spec") or "").strip()
    if source_spec:
        _check_relative_file(root, source_spec, checks, "source_spec")
    else:
        add_check("source_spec", "warning", "source_spec is not declared")

    status_doc = str(payload.get("status_doc") or "").strip()
    if status_doc:
        _check_relative_file(root, status_doc, checks, "status_doc")
    else:
        add_check("status_doc", "warning", "status_doc is not declared")

    decision_log_dir = str(payload.get("decision_log_dir") or "docs/decisions").strip()
    decision_path = root / decision_log_dir
    if decision_path.is_dir():
        add_check("decision_log_dir", "passed", "decision log directory exists", path=decision_log_dir)
    else:
        add_check("decision_log_dir", "warning", "decision log directory is missing", path=decision_log_dir)

    requirements = _mapping_list(payload.get("requirements"))
    requirement_ids = [str(item.get("id") or "").strip() for item in requirements if isinstance(item, dict)]
    duplicate_requirement_ids = _duplicates([item for item in requirement_ids if item])
    if not requirements:
        add_check("requirements", "warning", "requirements list is empty")
    elif duplicate_requirement_ids:
        add_check(
            "requirements_unique",
            "error",
            "duplicate requirement ids found",
            duplicates=duplicate_requirement_ids,
        )
    else:
        add_check("requirements", "passed", f"{len(requirements)} requirement(s) declared")

    known_requirements = set(requirement_ids)
    tasks = _mapping_list(payload.get("tasks"))
    task_ids = [str(item.get("id") or "").strip() for item in tasks if isinstance(item, dict)]
    duplicate_task_ids = _duplicates([item for item in task_ids if item])
    if not tasks:
        add_check("tasks", "warning", "tasks list is empty")
    elif duplicate_task_ids:
        add_check("tasks_unique", "error", "duplicate task ids found", duplicates=duplicate_task_ids)
    else:
        add_check("tasks", "passed", f"{len(tasks)} task(s) declared")

    missing_task_refs: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for requirement_id in _string_list(task.get("requirement_ids")):
            if known_requirements and requirement_id not in known_requirements:
                missing_task_refs.append({"task_id": task.get("id"), "requirement_id": requirement_id})
    if missing_task_refs:
        add_check(
            "task_requirement_refs",
            "error",
            "task references unknown requirement ids",
            missing_refs=missing_task_refs,
        )
    elif tasks:
        add_check("task_requirement_refs", "passed", "task requirement refs resolve")

    return _audit_payload(root, checks, spec_tasks_path=tasks_path, payload=payload)


def record_spec_task_update(
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
    apply: bool = False,
) -> dict[str, Any]:
    root = project_root.resolve()
    tasks_path = root / SPEC_TASKS_RELATIVE_PATH
    payload = load_mapping(tasks_path)
    tasks = _mapping_list(payload.get("tasks"))
    task = _find_task_record(tasks, task_id)
    if task is None:
        raise KeyError(f"spec task not found for task id or roadmap_task_id: {task_id}")

    now = utc_now_iso()
    previous_status = str(task.get("status") or "unknown")
    known_requirement_ids = {
        str(item.get("id") or "").strip()
        for item in _mapping_list(payload.get("requirements"))
        if str(item.get("id") or "").strip()
    }
    requested_requirement_ids = _string_list(requirement_ids)
    ignored_requirement_ids = [
        item for item in requested_requirement_ids if known_requirement_ids and item not in known_requirement_ids
    ]
    accepted_requirement_ids = [
        item for item in requested_requirement_ids if not known_requirement_ids or item in known_requirement_ids
    ]
    requirement_ids = _unique([*(_string_list(task.get("requirement_ids"))), *accepted_requirement_ids])
    evidence_entries = [
        {
            "kind": "record",
            "value": item,
            "recorded_at": now,
            "actor": actor,
        }
        for item in _string_list(evidence)
    ]
    update_record = {
        "kind": SPEC_SYNC_UPDATE_KIND,
        "updated_at": now,
        "project": payload.get("project") or root.name,
        "task_id": task.get("id") or task_id,
        "requested_task_id": task_id,
        "stage_id": stage_id,
        "phase": phase,
        "previous_status": previous_status,
        "new_status": status,
        "requirement_ids": requirement_ids,
        "ignored_requirement_ids": ignored_requirement_ids,
        "evidence": evidence_entries,
        "note": note,
        "actor": actor,
        "spec_tasks_path": SPEC_TASKS_RELATIVE_PATH,
    }
    update_record = {key: value for key, value in update_record.items() if value not in ("", [], None)}

    if not apply:
        proposed = dict(update_record)
        proposed["status"] = "proposed"
        return proposed

    task["status"] = status
    task["updated_at"] = now
    if requirement_ids:
        task["requirement_ids"] = requirement_ids
    if note:
        task["last_note"] = note
    if phase:
        task["last_phase"] = phase
    if stage_id:
        task["stage_id"] = stage_id
    if evidence_entries:
        existing_evidence = _mapping_list(task.get("evidence"))
        task["evidence"] = [*existing_evidence, *evidence_entries]
    payload["updated_at"] = now
    payload["tasks"] = tasks
    write_mapping(tasks_path, payload)
    append_jsonl(root / SPEC_UPDATE_LOG_RELATIVE_PATH, update_record)

    updated = dict(update_record)
    updated["status"] = "updated"
    updated["spec_update_log"] = SPEC_UPDATE_LOG_RELATIVE_PATH
    return updated


def _audit_payload(
    root: Path,
    checks: list[dict[str, Any]],
    *,
    spec_tasks_path: Path,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_count = sum(1 for check in checks if check.get("status") == "error")
    warning_count = sum(1 for check in checks if check.get("status") == "warning")
    status = "failed" if error_count else "warning" if warning_count else "passed"
    tasks = _mapping_list((payload or {}).get("tasks"))
    requirements = _mapping_list((payload or {}).get("requirements"))
    return {
        "kind": SPEC_SYNC_AUDIT_KIND,
        "status": status,
        "project": (payload or {}).get("project") or root.name,
        "root": str(root),
        "spec_tasks_path": _relative(root, spec_tasks_path),
        "checked_at": utc_now_iso(),
        "error_count": error_count,
        "warning_count": warning_count,
        "requirement_count": len(requirements),
        "task_count": len(tasks),
        "checks": checks,
    }


def _check_relative_file(root: Path, value: str, checks: list[dict[str, Any]], name: str) -> None:
    path = root / value
    if path.is_file():
        checks.append({"name": name, "status": "passed", "message": "file exists", "path": value})
    else:
        checks.append({"name": name, "status": "error", "message": "file is missing", "path": value})


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


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
        if value not in result:
            result.append(value)
    return result


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _find_task_record(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    for item in tasks:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == task_id:
            return item
    for item in tasks:
        if not isinstance(item, dict):
            continue
        if str(item.get("roadmap_task_id") or "") == task_id:
            return item
    return None

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import load_mapping, write_mapping


ROADMAP_RELATIVE_PATH = ".engineering/roadmap.yaml"
COMPLETED_ROADMAP_STATUSES = {"completed", "complete", "done", "passed"}


def record_roadmap_task_completion(root: Path, *, task_id: str, status: str = "completed") -> dict[str, Any]:
    roadmap_path = root / ROADMAP_RELATIVE_PATH
    if not roadmap_path.exists():
        return {"status": "skipped", "reason": "roadmap_not_configured", "task_id": task_id}

    roadmap = load_mapping(roadmap_path)
    result = _update_task_in_roadmap(roadmap, task_id=task_id, status=status)
    if result["status"] in {"applied", "unchanged"}:
        result["roadmap_path"] = ROADMAP_RELATIVE_PATH
    if result["status"] == "applied":
        write_mapping(roadmap_path, roadmap)
    return result


def _update_task_in_roadmap(roadmap: dict[str, Any], *, task_id: str, status: str) -> dict[str, Any]:
    for milestone in _mapping_items(roadmap.get("milestones")):
        task_result = _update_task_list(
            milestone.get("tasks"),
            task_id=task_id,
            status=status,
            parent=milestone,
            parent_kind="milestone",
        )
        if task_result is not None:
            return task_result

    continuation = roadmap.get("continuation")
    if isinstance(continuation, dict):
        for stage in _mapping_items(continuation.get("stages")):
            task_result = _update_task_list(
                stage.get("tasks"),
                task_id=task_id,
                status=status,
                parent=stage,
                parent_kind="continuation_stage",
            )
            if task_result is not None:
                return task_result

    return {"status": "skipped", "reason": "task_not_found_in_roadmap", "task_id": task_id}


def _update_task_list(
    tasks: Any,
    *,
    task_id: str,
    status: str,
    parent: dict[str, Any],
    parent_kind: str,
) -> dict[str, Any] | None:
    task_items = _mapping_items(tasks)
    for task in task_items:
        if str(task.get("id") or "") != task_id:
            continue

        previous_status = str(task.get("status") or "")
        changed_fields = []
        if previous_status != status:
            task["status"] = status
            changed_fields.append("task.status")

        parent_previous_status = str(parent.get("status") or "")
        if task_items and all(_is_completed_task(item) for item in task_items):
            if parent_previous_status != "completed":
                parent["status"] = "completed"
                changed_fields.append(f"{parent_kind}.status")

        return {
            "status": "applied" if changed_fields else "unchanged",
            "task_id": task_id,
            "previous_status": previous_status,
            "new_status": str(task.get("status") or ""),
            "parent_id": str(parent.get("id") or ""),
            "parent_kind": parent_kind,
            "parent_previous_status": parent_previous_status,
            "parent_new_status": str(parent.get("status") or ""),
            "changed_fields": changed_fields,
        }
    return None


def _mapping_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _is_completed_task(task: dict[str, Any]) -> bool:
    return str(task.get("status") or "").strip().lower() in COMPLETED_ROADMAP_STATUSES

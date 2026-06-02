from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .core import (
    BLOCKED_STATUSES,
    COMPLETED_STATUSES,
    Harness,
    HarnessTask,
    slug_now,
    supervisor_gate_stop_status,
    utc_now,
)
from .io import load_mapping, write_json


PARALLEL_DRIVE_SCHEMA_VERSION = 1
PARALLEL_DRIVE_KIND = "engineering-harness.parallel-drive-run-manifest"
PARALLEL_DRIVE_STATE_FILENAME = "parallel-drive.json"
PARALLEL_DRIVE_WORKTREE_DIRNAME = "parallel-drive-worktrees"
PARALLEL_DRIVE_REPORT_DIRNAME = "parallel-drives"
PARALLEL_DRIVE_POLL_SECONDS = 0.2
PARALLEL_DRIVE_BRANCH_PREFIX = "engo/parallel"


def parallel_drive_state_path(project_root: Path) -> Path:
    return project_root.resolve() / ".engineering" / "state" / PARALLEL_DRIVE_STATE_FILENAME


def parallel_drive_report_dir(project_root: Path) -> Path:
    return project_root.resolve() / ".engineering" / "reports" / "tasks" / PARALLEL_DRIVE_REPORT_DIRNAME


def load_parallel_drive_state(project_root: Path) -> dict[str, Any] | None:
    path = parallel_drive_state_path(project_root)
    if not path.exists():
        return None
    try:
        return load_mapping(path)
    except Exception:
        return None


def parallel_drive_runtime_summary(project_root: Path) -> dict[str, Any]:
    state = load_parallel_drive_state(project_root)
    if not isinstance(state, dict):
        return {
            "schema_version": PARALLEL_DRIVE_SCHEMA_VERSION,
            "kind": "engineering-harness.parallel-drive-runtime",
            "status": "not_found",
            "active": False,
            "state_path": _project_relative(project_root.resolve(), parallel_drive_state_path(project_root)),
        }
    workers = state.get("workers") if isinstance(state.get("workers"), list) else []
    return {
        "schema_version": PARALLEL_DRIVE_SCHEMA_VERSION,
        "kind": "engineering-harness.parallel-drive-runtime",
        "status": state.get("status", "unknown"),
        "active": bool(state.get("active", False)),
        "run_id": state.get("run_id"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "finished_at": state.get("finished_at"),
        "last_heartbeat_at": state.get("last_heartbeat_at"),
        "heartbeat_count": int(state.get("heartbeat_count", 0) or 0),
        "current_activity": state.get("current_activity"),
        "max_workers": state.get("max_workers"),
        "max_tasks": state.get("max_tasks"),
        "time_budget_seconds": state.get("time_budget_seconds"),
        "tasks_started": len([item for item in workers if isinstance(item, dict) and item.get("started_at")]),
        "tasks_merged": len([item for item in workers if isinstance(item, dict) and item.get("merge", {}).get("status") == "merged"]),
        "tasks_preserved": len(
            [
                item
                for item in workers
                if isinstance(item, dict) and item.get("preservation", {}).get("preserved")
            ]
        ),
        "workers": [
            {
                "task_id": item.get("task_id"),
                "status": item.get("status"),
                "branch": item.get("branch"),
                "worktree_path": item.get("worktree_path"),
                "pid": item.get("pid"),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
                "last_heartbeat_at": item.get("last_heartbeat_at"),
                "merge_status": (item.get("merge") or {}).get("status")
                if isinstance(item.get("merge"), dict)
                else None,
                "cleanup_status": (item.get("cleanup") or {}).get("status")
                if isinstance(item.get("cleanup"), dict)
                else None,
                "report": item.get("report"),
                "manifest": item.get("manifest"),
            }
            for item in workers
            if isinstance(item, dict)
        ],
        "latest_report": state.get("parallel_drive_report"),
        "latest_report_json": state.get("parallel_drive_report_json"),
        "state_path": state.get("state_path")
        or _project_relative(project_root.resolve(), parallel_drive_state_path(project_root)),
    }


def plan_parallel_drive(
    project_root: Path,
    *,
    max_workers: int = 1,
    max_tasks: int = 1,
    base_ref: str | None = None,
    allow_dirty_worktree: bool = False,
) -> dict[str, Any]:
    root = project_root.resolve()
    harness = Harness(root)
    repo = _repo_root(root)
    state = harness.load_state()
    task_metadata = _roadmap_task_metadata(harness.roadmap)
    completed_task_ids = _completed_task_ids(harness, state)
    checkpoint_readiness = harness.checkpoint_readiness()
    pending = _pending_task_candidates(harness, state, task_metadata)
    eligible, skipped = _eligible_tasks(pending, completed_task_ids=completed_task_ids)
    selected = _select_planned_tasks(eligible, max_tasks=max_tasks)
    lanes = _planned_lanes(selected, max_workers=max_workers)
    dispatch_waves = _dispatch_waves(selected)
    status = "planned"
    messages: list[str] = []
    if repo is None:
        status = "blocked"
        messages.append("project root is not inside a git repository")
    if checkpoint_readiness.get("blocking") and not allow_dirty_worktree:
        status = "blocked"
        messages.append("project has unresolved dirty worktree state")
    if not selected and status == "planned":
        unresolved_dependency_skips = [
            item
            for item in skipped
            if any(reason.get("code") == "dependency_not_satisfied" for reason in item.get("skip_reasons", []))
        ]
        if unresolved_dependency_skips:
            status = "blocked"
            messages.append("pending tasks have unresolved dependencies")
        else:
            status = "empty"
            messages.append("no eligible pending roadmap tasks were found")
    return {
        "schema_version": PARALLEL_DRIVE_SCHEMA_VERSION,
        "kind": "engineering-harness.parallel-drive-plan",
        "status": status,
        "planned_at": utc_now(),
        "project_root": str(root),
        "repository_root": str(repo) if repo is not None else None,
        "base": {
            "ref": base_ref or _current_branch(root) or "HEAD",
            "current_branch": _current_branch(root),
        },
        "limits": {
            "max_workers": _positive_int(max_workers, 1),
            "max_tasks": _positive_int(max_tasks, 1),
            "allow_dirty_worktree": bool(allow_dirty_worktree),
        },
        "checkpoint_readiness": checkpoint_readiness,
        "completed_task_ids": sorted(completed_task_ids),
        "pending_count": len(pending),
        "eligible_count": len(eligible),
        "selected_count": len(selected),
        "selected_tasks": [_task_plan_payload(item) for item in selected],
        "skipped_tasks": [_task_plan_payload(item) for item in skipped],
        "lanes": lanes,
        "dispatch_waves": dispatch_waves,
        "message": "; ".join(messages) if messages else "parallel drive plan is ready",
    }


def run_parallel_drive(
    project_root: Path,
    *,
    max_workers: int = 1,
    max_tasks: int = 1,
    time_budget_seconds: int = 0,
    base_ref: str | None = None,
    allow_dirty_worktree: bool = False,
    allow_live: bool = False,
    allow_manual: bool = False,
    allow_agent: bool = False,
    poll_interval_seconds: float = PARALLEL_DRIVE_POLL_SECONDS,
    resume: bool = False,
    supervisor_gate: Any = None,
    supervisor_decision: Path | str | None = None,
    supervisor_gate_risk_threshold: int | None = None,
) -> tuple[int, dict[str, Any]]:
    root = project_root.resolve()
    harness = Harness(root)
    supervisor_gate_settings = harness.supervisor_gate_settings(
        gate_values=supervisor_gate,
        decision_path=supervisor_decision,
        risk_threshold=supervisor_gate_risk_threshold,
    )
    supervisor_gates: list[dict[str, Any]] = []
    milestone_gates_recorded: set[str] = set()
    started_at = utc_now()
    max_workers = _positive_int(max_workers, 1)
    max_tasks = _positive_int(max_tasks, 1)
    time_budget_seconds = max(0, int(time_budget_seconds or 0))
    poll_interval_seconds = max(0.05, float(poll_interval_seconds or PARALLEL_DRIVE_POLL_SECONDS))
    run_id = f"{slug_now()}-{os.getpid()}"
    state_path = parallel_drive_state_path(root)

    plan = plan_parallel_drive(
        root,
        max_workers=max_workers,
        max_tasks=max_tasks,
        base_ref=base_ref,
        allow_dirty_worktree=allow_dirty_worktree,
    )
    existing_state = load_parallel_drive_state(root)
    resume_payload = _resume_payload(existing_state, resume=resume)
    if plan["status"] == "blocked":
        payload = _base_payload(
            harness,
            run_id=run_id,
            started_at=started_at,
            status="blocked",
            message=plan["message"],
            max_workers=max_workers,
            max_tasks=max_tasks,
            time_budget_seconds=time_budget_seconds,
            base_ref=base_ref,
            plan=plan,
            resume_payload=resume_payload,
        )
        payload["supervisor_gates"] = supervisor_gates
        if harness.supervisor_gate_enabled(supervisor_gate_settings, "blocked_task"):
            gate = harness.invoke_supervisor_gate(
                gate_type="blocked_task",
                reason=f"parallel-drive planning blocked: {plan['message']}",
                settings=supervisor_gate_settings,
                source="parallel-drive",
                risk_metadata={"plan_status": plan["status"], "plan_message": plan["message"]},
                apply_drive_control=False,
            )
            supervisor_gates.append(gate)
            stop_status = supervisor_gate_stop_status(gate)
            if stop_status:
                payload["status"] = stop_status
                payload["message"] = _parallel_supervisor_gate_stop_message(gate)
        _write_parallel_state(root, payload, activity="parallel-drive-blocked")
        payload["parallel_drive_report"] = write_parallel_drive_report(root, payload)
        _write_parallel_state(root, payload, activity="parallel-drive-blocked")
        return 1, payload

    if plan["status"] == "empty":
        payload = _base_payload(
            harness,
            run_id=run_id,
            started_at=started_at,
            status="completed",
            message=plan["message"],
            max_workers=max_workers,
            max_tasks=max_tasks,
            time_budget_seconds=time_budget_seconds,
            base_ref=base_ref,
            plan=plan,
            resume_payload=resume_payload,
        )
        payload["supervisor_gates"] = supervisor_gates
        _write_parallel_state(root, payload, activity="parallel-drive-empty")
        payload["parallel_drive_report"] = write_parallel_drive_report(root, payload)
        _write_parallel_state(root, payload, activity="parallel-drive-empty")
        return 0, payload

    repo_root = Path(str(plan["repository_root"])).resolve()
    base = str(plan.get("base", {}).get("ref") or base_ref or _current_branch(root) or "HEAD")
    switch = _ensure_base_checked_out(repo_root, base)
    if switch["returncode"] != 0:
        payload = _base_payload(
            harness,
            run_id=run_id,
            started_at=started_at,
            status="blocked",
            message=f"could not switch to base ref `{base}`",
            max_workers=max_workers,
            max_tasks=max_tasks,
            time_budget_seconds=time_budget_seconds,
            base_ref=base,
            plan=plan,
            resume_payload=resume_payload,
        )
        payload["base_checkout"] = switch
        payload["supervisor_gates"] = supervisor_gates
        if harness.supervisor_gate_enabled(supervisor_gate_settings, "blocked_task"):
            gate = harness.invoke_supervisor_gate(
                gate_type="blocked_task",
                reason=f"parallel-drive base checkout blocked for `{base}`",
                settings=supervisor_gate_settings,
                source="parallel-drive",
                risk_metadata={"base_checkout": switch},
                apply_drive_control=False,
            )
            supervisor_gates.append(gate)
            stop_status = supervisor_gate_stop_status(gate)
            if stop_status:
                payload["status"] = stop_status
                payload["message"] = _parallel_supervisor_gate_stop_message(gate)
        _write_parallel_state(root, payload, activity="parallel-drive-base-checkout-failed")
        payload["parallel_drive_report"] = write_parallel_drive_report(root, payload)
        _write_parallel_state(root, payload, activity="parallel-drive-base-checkout-failed")
        return 1, payload

    task_metadata = _roadmap_task_metadata(harness.roadmap)
    pending = _pending_task_candidates(harness, harness.load_state(), task_metadata)
    selected_ids = [str(item["task"].id) for item in _select_planned_tasks(pending, max_tasks=max_tasks)]
    pending_queue = [item for item in pending if str(item["task"].id) in selected_ids]
    active: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    completed_task_ids = _completed_task_ids(harness, harness.load_state())
    deadline = time.monotonic() + time_budget_seconds if time_budget_seconds else None
    status = "completed"
    message = "Parallel drive completed."
    timed_out = False
    gate_stopped = False
    launched_count = 0

    payload = _base_payload(
        harness,
        run_id=run_id,
        started_at=started_at,
        status="running",
        message="parallel drive running",
        max_workers=max_workers,
        max_tasks=max_tasks,
        time_budget_seconds=time_budget_seconds,
        base_ref=base,
        plan=plan,
        resume_payload=resume_payload,
    )
    payload["workers"] = workers
    payload["supervisor_gates"] = supervisor_gates
    _write_parallel_state(root, payload, activity="parallel-drive-started")

    try:
        if harness.supervisor_gate_enabled(supervisor_gate_settings, "operator_request"):
            gate = harness.invoke_supervisor_gate(
                gate_type="operator_request",
                reason="explicit operator request before parallel-drive scheduling",
                settings=supervisor_gate_settings,
                source="parallel-drive",
                risk_metadata={"planned_tasks": selected_ids},
                apply_drive_control=False,
            )
            supervisor_gates.append(gate)
            payload["supervisor_gates"] = supervisor_gates
            stop_status = supervisor_gate_stop_status(gate)
            if stop_status:
                status = stop_status
                message = _parallel_supervisor_gate_stop_message(gate)
                gate_stopped = True
                pending_queue.clear()
                active.clear()
        while pending_queue or active:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                timed_out = True
                status = "timeout"
                message = "Parallel drive time budget expired."
                for worker in active:
                    _terminate_worker(worker)
                    _finish_worker_process(worker)
                    worker["status"] = "timeout"
                    worker["finished_at"] = utc_now()
                    worker["preservation"] = _preservation_payload(worker, "worker timed out before completion")
                active.clear()
                break

            launched = False
            while len(active) < max_workers and pending_queue and launched_count < max_tasks:
                next_index = _next_dispatchable_index(
                    pending_queue,
                    active,
                    completed_task_ids=completed_task_ids,
                )
                if next_index is None:
                    break
                item = pending_queue.pop(next_index)
                task = item["task"]
                if (
                    harness.supervisor_gate_enabled(supervisor_gate_settings, "deployment_sensitive_task")
                    and harness.task_declares_deployment_sensitive(task)
                ):
                    gate = harness.invoke_supervisor_gate(
                        gate_type="deployment_sensitive_task",
                        reason=f"parallel-drive task `{task.id}` declares a sensitive deployment or secret gate",
                        settings=supervisor_gate_settings,
                        task=task,
                        source="parallel-drive",
                        apply_drive_control=False,
                    )
                    supervisor_gates.append(gate)
                    payload["supervisor_gates"] = supervisor_gates
                    stop_status = supervisor_gate_stop_status(gate)
                    if stop_status:
                        status = stop_status
                        message = _parallel_supervisor_gate_stop_message(gate)
                        gate_stopped = True
                        pending_queue.clear()
                        break
                worker = _launch_worker(
                    root,
                    repo_root,
                    run_id=run_id,
                    base_ref=base,
                    task=task,
                    index=launched_count + 1,
                    allow_live=allow_live,
                    allow_manual=allow_manual,
                    allow_agent=allow_agent,
                )
                workers.append(worker)
                launched_count += 1
                launched = True
                if worker.get("process") is not None:
                    active.append(worker)
                else:
                    worker["preservation"] = _preservation_payload(worker, "worker worktree could not be created")
                _write_parallel_state(root, payload, activity=f"worker-started:{task.id}")

            completed_workers = []
            for worker in active:
                if worker["process"].poll() is None:
                    worker["last_heartbeat_at"] = utc_now()
                    worker["heartbeat_count"] = int(worker.get("heartbeat_count", 0) or 0) + 1
                    continue
                completed_workers.append(worker)

            for worker in completed_workers:
                active.remove(worker)
                _finish_worker_process(worker)
                _collect_worker_result(root, worker)
                if worker.get("result_status") in COMPLETED_STATUSES and worker.get("exit_code") == 0:
                    merge = _merge_successful_worker(
                        root,
                        repo_root,
                        worker,
                        allow_live=allow_live,
                        allow_manual=allow_manual,
                        allow_agent=allow_agent,
                    )
                    worker["merge"] = merge
                    if merge.get("status") == "merged":
                        completed_task_ids.add(str(worker["task_id"]))
                        _copy_worker_task_state(root, worker)
                        cleanup = _cleanup_merged_worker(repo_root, worker)
                        worker["cleanup"] = cleanup
                        worker["status"] = "merged" if cleanup.get("status") in {"cleaned", "partially_cleaned"} else "cleanup_failed"
                    else:
                        worker["status"] = "merge_failed"
                        worker["preservation"] = _preservation_payload(worker, merge.get("message"))
                else:
                    worker["status"] = str(worker.get("result_status") or "failed")
                    worker["preservation"] = _preservation_payload(worker, "worker task did not complete successfully")
                worker["finished_at"] = worker.get("finished_at") or utc_now()
                _annotate_worker_manifest(root, worker)
                try:
                    worker_task = harness.task_by_id(str(worker["task_id"]))
                except KeyError:
                    worker_task = None
                worker_result = {
                    "status": worker.get("result_status") or worker.get("status"),
                    "worker_status": worker.get("status"),
                    "exit_code": worker.get("exit_code"),
                    "report": worker.get("report"),
                    "manifest": worker.get("manifest"),
                    "branch": worker.get("branch"),
                }
                if worker_task is not None and str(worker.get("result_status")) not in COMPLETED_STATUSES:
                    gate_type = "blocked_task" if worker.get("result_status") == "blocked" else "failed_task"
                    if harness.supervisor_gate_enabled(supervisor_gate_settings, gate_type):
                        gate = harness.invoke_supervisor_gate(
                            gate_type=gate_type,
                            reason=f"parallel-drive task `{worker_task.id}` finished with `{worker.get('result_status')}`",
                            settings=supervisor_gate_settings,
                            task=worker_task,
                            result=worker_result,
                            source="parallel-drive",
                            apply_drive_control=False,
                        )
                        supervisor_gates.append(gate)
                        payload["supervisor_gates"] = supervisor_gates
                        if gate.get("applied") and gate.get("decision") == "retry":
                            pending_queue.append(
                                {
                                    "task": worker_task,
                                    "task_id": worker_task.id,
                                    "status": "pending",
                                    "attempts": 0,
                                    "max_attempts": worker_task.max_attempts,
                                    "file_scope": list(worker_task.file_scope),
                                    "dependencies": _task_dependencies(task_metadata.get(worker_task.id, {})),
                                    "skip_reasons": [],
                                }
                            )
                        else:
                            stop_status = supervisor_gate_stop_status(gate)
                            if stop_status:
                                status = stop_status
                                message = _parallel_supervisor_gate_stop_message(gate)
                                gate_stopped = True
                                pending_queue.clear()
                if worker_task is not None and worker.get("status") in {"merged", "cleanup_failed"}:
                    if (
                        harness.supervisor_gate_enabled(supervisor_gate_settings, "quality_gate_completion")
                        and harness.task_declares_quality_gate(worker_task)
                    ):
                        gate = harness.invoke_supervisor_gate(
                            gate_type="quality_gate_completion",
                            reason=f"parallel-drive task `{worker_task.id}` completed a declared quality-gate",
                            settings=supervisor_gate_settings,
                            task=worker_task,
                            result=worker_result,
                            source="parallel-drive",
                            apply_drive_control=False,
                        )
                        supervisor_gates.append(gate)
                        payload["supervisor_gates"] = supervisor_gates
                        stop_status = supervisor_gate_stop_status(gate)
                        if stop_status:
                            status = stop_status
                            message = _parallel_supervisor_gate_stop_message(gate)
                            gate_stopped = True
                            pending_queue.clear()
                    if (
                        worker_task.milestone_id not in milestone_gates_recorded
                        and harness.supervisor_gate_enabled(supervisor_gate_settings, "milestone_completion")
                        and Harness(root).milestone_completed(worker_task.milestone_id)
                    ):
                        gate = harness.invoke_supervisor_gate(
                            gate_type="milestone_completion",
                            reason=f"parallel-drive milestone `{worker_task.milestone_id}` completed",
                            settings=supervisor_gate_settings,
                            task=worker_task,
                            result=worker_result,
                            source="parallel-drive",
                            risk_metadata={"milestone_id": worker_task.milestone_id},
                            apply_drive_control=False,
                        )
                        supervisor_gates.append(gate)
                        milestone_gates_recorded.add(worker_task.milestone_id)
                        payload["supervisor_gates"] = supervisor_gates
                        stop_status = supervisor_gate_stop_status(gate)
                        if stop_status:
                            status = stop_status
                            message = _parallel_supervisor_gate_stop_message(gate)
                            gate_stopped = True
                            pending_queue.clear()
                _write_parallel_state(root, payload, activity=f"worker-finished:{worker['task_id']}")

            if not launched and not completed_workers:
                if not active:
                    if pending_queue:
                        status = "blocked"
                        message = "No remaining pending task can be dispatched because dependencies are unresolved."
                    break
                time.sleep(poll_interval_seconds)
            else:
                _write_parallel_state(root, payload, activity="parallel-drive-running")

        failed_workers = [
            worker
            for worker in workers
            if str(worker.get("status")) not in {"merged", "cleanup_failed"}
        ]
        cleanup_failures = [worker for worker in workers if str(worker.get("status")) == "cleanup_failed"]
        if gate_stopped:
            pass
        elif timed_out:
            status = "timeout"
        elif failed_workers:
            status = "failed"
            message = f"{len(failed_workers)} parallel worker(s) failed or were preserved."
        elif cleanup_failures:
            status = "completed_with_cleanup_warnings"
            message = f"{len(cleanup_failures)} merged worker(s) had cleanup warnings."
        elif launched_count >= max_tasks and pending_queue:
            status = "budget_exhausted"
            message = f"Parallel task budget exhausted after {launched_count} task(s)."
        elif launched_count == 0:
            status = "completed"
            message = "No dispatchable parallel task remained."
        else:
            status = "completed"
            message = f"Parallel drive merged {launched_count} task(s)."
    finally:
        for worker in active:
            if worker["process"].poll() is None:
                _terminate_worker(worker)
            _finish_worker_process(worker)
            worker.setdefault("status", "interrupted")
            worker.setdefault("preservation", _preservation_payload(worker, "parallel drive interrupted"))

    final_status = Harness(root).status_summary()
    if harness.supervisor_gate_enabled(supervisor_gate_settings, "budget_risk_threshold"):
        triggered, gate_risk_metadata = harness.budget_risk_gate_triggered(
            status=status,
            settings=supervisor_gate_settings,
            final_status=final_status,
        )
        if triggered:
            gate = harness.invoke_supervisor_gate(
                gate_type="budget_risk_threshold",
                reason="parallel-drive reached a configured budget or risk threshold",
                settings=supervisor_gate_settings,
                source="parallel-drive",
                risk_metadata=gate_risk_metadata,
                apply_drive_control=False,
            )
            supervisor_gates.append(gate)
            stop_status = supervisor_gate_stop_status(gate)
            if stop_status:
                status = stop_status
                message = _parallel_supervisor_gate_stop_message(gate)
                gate_stopped = True
            final_status = Harness(root).status_summary()

    payload["status"] = status
    payload["message"] = message
    payload["finished_at"] = utc_now()
    payload["active"] = False
    payload["final_status"] = final_status
    payload["workers"] = [_serializable_worker(worker) for worker in workers]
    payload["supervisor_gates"] = supervisor_gates
    payload["summary"] = _parallel_summary(payload)
    payload["parallel_drive_report"] = write_parallel_drive_report(root, payload)
    _write_parallel_state(root, payload, activity="parallel-drive-finished")
    exit_code = 0 if status in {"completed", "budget_exhausted", "completed_with_cleanup_warnings"} else 1
    return exit_code, payload


def write_parallel_drive_report(project_root: Path, payload: dict[str, Any]) -> str:
    root = project_root.resolve()
    report_dir = parallel_drive_report_dir(root)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{payload.get('run_id') or slug_now()}-parallel-drive"
    report_path = report_dir / f"{stem}.md"
    json_path = report_path.with_suffix(".json")
    payload["parallel_drive_report"] = _project_relative(root, report_path)
    payload["parallel_drive_report_json"] = _project_relative(root, json_path)
    lines = [
        "# Parallel Development Drive Report",
        "",
        f"- Project: `{payload.get('project')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Message: {payload.get('message')}",
        f"- Started: {payload.get('started_at')}",
        f"- Finished: {payload.get('finished_at') or 'running'}",
        f"- Max workers: `{payload.get('max_workers')}`",
        f"- Max tasks: `{payload.get('max_tasks')}`",
        f"- Base: `{payload.get('base', {}).get('ref')}`",
        "",
        "## Native Parallel Plan",
        "",
        f"- Selected tasks: `{payload.get('plan', {}).get('selected_count', 0)}`",
        f"- Eligible tasks: `{payload.get('plan', {}).get('eligible_count', 0)}`",
        f"- Pending tasks: `{payload.get('plan', {}).get('pending_count', 0)}`",
        f"- Checkpoint readiness: `{payload.get('plan', {}).get('checkpoint_readiness', {}).get('reason', 'unknown')}`",
        "",
    ]
    supervisor_gates = payload.get("supervisor_gates") if isinstance(payload.get("supervisor_gates"), list) else []
    lines.extend(["## Supervisor Gates", ""])
    if not supervisor_gates:
        lines.append("No supervisor gate was invoked for this parallel-drive report.")
    for gate in supervisor_gates:
        if not isinstance(gate, dict):
            continue
        lines.extend(
            [
                (
                    f"- Supervisor gate `{gate.get('gate_type')}`: "
                    f"`{gate.get('application_status')}` - {gate.get('application_reason')}"
                ),
                f"  - Context: `{gate.get('context_path')}`",
                f"  - Decision: `{gate.get('decision_path')}`",
                f"  - Decision status: `{gate.get('decision_status')}` action=`{gate.get('decision')}`",
                f"  - Applied: `{str(bool(gate.get('applied'))).lower()}`",
            ]
        )
    lines.extend(
        [
            "",
            "Machine-readable supervisor gate records:",
            "",
            "```json",
            json.dumps(_json_safe(supervisor_gates), indent=2, sort_keys=True),
            "```",
            "",
            "## Workers",
            "",
        ]
    )
    workers = payload.get("workers") if isinstance(payload.get("workers"), list) else []
    if not workers:
        lines.append("No workers were launched.")
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        lines.extend(
            [
                f"- Task `{worker.get('task_id')}` status=`{worker.get('status')}` branch=`{worker.get('branch')}`",
                f"  - Worktree: `{worker.get('worktree_path')}`",
                f"  - Result: `{worker.get('result_status')}` exit=`{worker.get('exit_code')}`",
                f"  - Report: `{worker.get('report') or 'none'}`",
                f"  - Manifest: `{worker.get('manifest') or 'none'}`",
            ]
        )
        merge = worker.get("merge") if isinstance(worker.get("merge"), dict) else {}
        cleanup = worker.get("cleanup") if isinstance(worker.get("cleanup"), dict) else {}
        preservation = worker.get("preservation") if isinstance(worker.get("preservation"), dict) else {}
        if merge:
            lines.append(f"  - Merge: `{merge.get('status')}` {merge.get('message') or ''}")
        if cleanup:
            lines.append(f"  - Cleanup: `{cleanup.get('status')}` {cleanup.get('message') or ''}")
        if preservation:
            lines.append(f"  - Preserved: `{str(bool(preservation.get('preserved'))).lower()}` {preservation.get('reason') or ''}")
    lines.extend(
        [
            "",
            "## Machine-Readable Parallel Drive",
            "",
            "```json",
            json.dumps(_json_safe(payload), indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(json_path, _json_safe(payload))
    return payload["parallel_drive_report"]


def _parallel_supervisor_gate_stop_message(gate: dict[str, Any]) -> str:
    gate_type = str(gate.get("gate_type") or "supervisor_gate")
    decision = str(gate.get("decision") or "unknown")
    reason = str(gate.get("application_reason") or gate.get("reason") or "no reason recorded")
    return f"Supervisor gate `{gate_type}` stopped parallel-drive scheduling with `{decision}`: {reason}"


def _base_payload(
    harness: Harness,
    *,
    run_id: str,
    started_at: str,
    status: str,
    message: str,
    max_workers: int,
    max_tasks: int,
    time_budget_seconds: int,
    base_ref: str | None,
    plan: dict[str, Any],
    resume_payload: dict[str, Any],
) -> dict[str, Any]:
    root = harness.project_root
    return {
        "schema_version": PARALLEL_DRIVE_SCHEMA_VERSION,
        "kind": PARALLEL_DRIVE_KIND,
        "run_id": run_id,
        "project": str(harness.roadmap.get("project", root.name)),
        "root": str(root),
        "roadmap": str(harness.roadmap_path),
        "status": status,
        "active": status == "running",
        "message": message,
        "started_at": started_at,
        "updated_at": utc_now(),
        "finished_at": None if status == "running" else utc_now(),
        "last_heartbeat_at": utc_now(),
        "heartbeat_count": 1,
        "current_activity": status,
        "max_workers": max_workers,
        "max_tasks": max_tasks,
        "time_budget_seconds": time_budget_seconds,
        "base": deepcopy(plan.get("base", {"ref": base_ref})),
        "plan": plan,
        "resume": resume_payload,
        "workers": [],
        "state_path": _project_relative(root, parallel_drive_state_path(root)),
    }


def _write_parallel_state(project_root: Path, payload: dict[str, Any], *, activity: str) -> None:
    payload["updated_at"] = utc_now()
    payload["last_heartbeat_at"] = payload["updated_at"]
    payload["heartbeat_count"] = int(payload.get("heartbeat_count", 0) or 0) + 1
    payload["current_activity"] = activity
    if payload.get("status") == "running":
        payload["active"] = True
    write_json(parallel_drive_state_path(project_root), _json_safe(payload))


def _repo_root(project_root: Path) -> Path | None:
    result = _git(project_root, ["rev-parse", "--show-toplevel"])
    if result["returncode"] != 0 or not result["stdout"].strip():
        return None
    return Path(result["stdout"].strip()).resolve()


def _current_branch(project_root: Path) -> str | None:
    result = _git(project_root, ["branch", "--show-current"])
    branch = result["stdout"].strip()
    return branch or None


def _ensure_base_checked_out(repo_root: Path, base_ref: str) -> dict[str, Any]:
    current = _current_branch(repo_root)
    if current == base_ref:
        return {"returncode": 0, "stdout": "", "stderr": "", "message": "base branch already checked out"}
    return _git(repo_root, ["switch", base_ref])


def _pending_task_candidates(
    harness: Harness,
    state: dict[str, Any],
    task_metadata: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    state_tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    pending: list[dict[str, Any]] = []
    for task in harness.iter_tasks():
        task_state = state_tasks.get(task.id) if isinstance(state_tasks.get(task.id), dict) else {}
        status = str(task_state.get("status", task.status))
        attempts = int(task_state.get("attempts", 0) or 0)
        dependencies = _task_dependencies(task_metadata.get(task.id, {}))
        item = {
            "task": task,
            "task_id": task.id,
            "status": status,
            "attempts": attempts,
            "max_attempts": task.max_attempts,
            "file_scope": list(task.file_scope),
            "dependencies": dependencies,
            "skip_reasons": [],
        }
        if status in COMPLETED_STATUSES or status in BLOCKED_STATUSES:
            item["skip_reasons"].append(
                {"code": "terminal_status", "message": f"task status is {status}"}
            )
        elif attempts >= task.max_attempts:
            item["skip_reasons"].append(
                {"code": "attempts_exhausted", "message": "task has exhausted attempts"}
            )
        pending.append(item)
    return pending


def _eligible_tasks(
    pending: list[dict[str, Any]],
    *,
    completed_task_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = []
    skipped = []
    known_ids = {str(item.get("task_id")) for item in pending}
    for item in pending:
        reasons = list(item.get("skip_reasons", []))
        unresolved = [
            dependency
            for dependency in item.get("dependencies", [])
            if dependency not in completed_task_ids and dependency in known_ids
        ]
        if unresolved:
            reasons.append(
                {
                    "code": "dependency_not_satisfied",
                    "message": "task dependency has not completed",
                    "dependencies": unresolved,
                }
            )
        item["skip_reasons"] = reasons
        if reasons:
            skipped.append(item)
        else:
            eligible.append(item)
    return eligible, skipped


def _select_planned_tasks(items: list[dict[str, Any]], *, max_tasks: int) -> list[dict[str, Any]]:
    selected = []
    for item in items:
        if item.get("skip_reasons"):
            continue
        selected.append(item)
        if len(selected) >= max_tasks:
            break
    return selected


def _planned_lanes(items: list[dict[str, Any]], *, max_workers: int) -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = [
        {"lane": lane_index + 1, "tasks": [], "file_scopes": [], "_items": []}
        for lane_index in range(max(1, max_workers))
    ]
    for item in items:
        target = next(
            (
                lane
                for lane in lanes
                if any(_scopes_overlap(item.get("file_scope", []), existing.get("file_scope", [])) for existing in lane["_items"])
            ),
            None,
        )
        if target is None:
            target = min(lanes, key=lambda lane: len(lane["tasks"]))
        target["tasks"].append(str(item.get("task_id")))
        target["file_scopes"].append(list(item.get("file_scope", [])))
        target["_items"].append(item)
    return [{key: value for key, value in lane.items() if key != "_items"} for lane in lanes]


def _dispatch_waves(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = list(items)
    waves: list[dict[str, Any]] = []
    while remaining:
        wave: list[dict[str, Any]] = []
        next_remaining: list[dict[str, Any]] = []
        for item in remaining:
            if any(_scopes_overlap(item.get("file_scope", []), existing.get("file_scope", [])) for existing in wave):
                next_remaining.append(item)
            else:
                wave.append(item)
        waves.append(
            {
                "wave": len(waves) + 1,
                "tasks": [str(item.get("task_id")) for item in wave],
                "file_scopes": [list(item.get("file_scope", [])) for item in wave],
            }
        )
        remaining = next_remaining
    return waves


def _next_dispatchable_index(
    pending_queue: list[dict[str, Any]],
    active: list[dict[str, Any]],
    *,
    completed_task_ids: set[str],
) -> int | None:
    for index, item in enumerate(pending_queue):
        dependencies = set(str(dep) for dep in item.get("dependencies", []))
        if dependencies - completed_task_ids:
            continue
        task = item["task"]
        if any(_scopes_overlap(task.file_scope, worker.get("file_scope", [])) for worker in active):
            continue
        return index
    return None


def _launch_worker(
    project_root: Path,
    repo_root: Path,
    *,
    run_id: str,
    base_ref: str,
    task: HarnessTask,
    index: int,
    allow_live: bool,
    allow_manual: bool,
    allow_agent: bool,
) -> dict[str, Any]:
    branch = _worker_branch_name(task.id, run_id, index)
    worktree = (
        project_root
        / ".engineering"
        / "state"
        / PARALLEL_DRIVE_WORKTREE_DIRNAME
        / run_id
        / _slug(task.id)
    )
    add_result = _git(repo_root, ["worktree", "add", "-b", branch, str(worktree), base_ref])
    if add_result["returncode"] != 0:
        return {
            "run_id": run_id,
            "task_id": task.id,
            "task": task,
            "file_scope": list(task.file_scope),
            "branch": branch,
            "worktree_path": str(worktree),
            "status": "worktree_failed",
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "worktree_add": add_result,
            "exit_code": 1,
            "result_status": "failed",
        }
    log_dir = project_root / ".engineering" / "state" / "parallel-drive-worker-logs" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{_slug(task.id)}.stdout"
    stderr_path = log_dir / f"{_slug(task.id)}.stderr"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "engineering_orchestrator.cli",
        "run",
        "--project-root",
        str(worktree),
        "--task",
        task.id,
        "--max-tasks",
        "1",
        "--commit-after-task",
        "--git-message-template",
        "chore(engineering): complete {task_id}",
        "--json",
    ]
    if allow_live:
        command.append("--allow-live")
    if allow_manual:
        command.append("--allow-manual")
    if allow_agent:
        command.append("--allow-agent")
    process = subprocess.Popen(
        command,
        cwd=worktree,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
    )
    now = utc_now()
    return {
        "run_id": run_id,
        "task_id": task.id,
        "task": task,
        "file_scope": list(task.file_scope),
        "branch": branch,
        "worktree_path": str(worktree),
        "status": "running",
        "pid": process.pid,
        "started_at": now,
        "last_heartbeat_at": now,
        "heartbeat_count": 1,
        "command": command,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "process": process,
        "_stdout_handle": stdout_handle,
        "_stderr_handle": stderr_handle,
        "worktree_add": add_result,
    }


def _finish_worker_process(worker: dict[str, Any]) -> None:
    for key in ("_stdout_handle", "_stderr_handle"):
        handle = worker.pop(key, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
    process = worker.get("process")
    if process is not None:
        try:
            worker["exit_code"] = process.returncode if process.returncode is not None else process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            worker["exit_code"] = None
    worker.pop("process", None)


def _terminate_worker(worker: dict[str, Any]) -> None:
    process = worker.get("process")
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _collect_worker_result(project_root: Path, worker: dict[str, Any]) -> None:
    stdout = _read_text(Path(str(worker.get("stdout_path") or "")))
    stderr = _read_text(Path(str(worker.get("stderr_path") or "")))
    worker["stdout_tail"] = stdout[-4000:]
    worker["stderr_tail"] = stderr[-4000:]
    result = _parse_worker_json(stdout)
    worker["result"] = result
    first = result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else {}
    worker["result_status"] = str(first.get("status") or "failed")
    worker["message"] = first.get("message")
    if first:
        copied = _copy_worker_evidence(project_root, Path(str(worker["worktree_path"])), first)
        worker["copied_evidence"] = copied
        if copied.get("result"):
            first = copied["result"]
            worker["result"] = [first]
        worker["report"] = first.get("report")
        worker["manifest"] = first.get("manifest")
        worker["git"] = first.get("git")
    if worker.get("exit_code") is None:
        worker["exit_code"] = 1
    worker["finished_at"] = utc_now()


def _parse_worker_json(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for marker in ("[\n", "[{"):
        index = text.find(marker)
        if index >= 0:
            try:
                return json.loads(text[index:])
            except json.JSONDecodeError:
                continue
    return None


def _copy_worker_evidence(project_root: Path, worktree: Path, result: dict[str, Any]) -> dict[str, Any]:
    copied_result = deepcopy(result)
    copied_paths: dict[str, str] = {}
    for key in ("report", "manifest"):
        relative = copied_result.get(key)
        if not relative:
            continue
        source = worktree / str(relative)
        if not source.exists():
            continue
        target = _unique_target(project_root / str(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_result[key] = _project_relative(project_root, target)
        copied_paths[str(relative)] = copied_result[key]
    manifest_rel = copied_result.get("manifest")
    if manifest_rel:
        manifest_path = project_root / str(manifest_rel)
        if manifest_path.exists():
            try:
                manifest = load_mapping(manifest_path)
                manifest["project_root"] = str(project_root)
                manifest["report_path"] = copied_result.get("report") or manifest.get("report_path")
                manifest["manifest_path"] = copied_result.get("manifest") or manifest.get("manifest_path")
                artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
                for artifact in artifacts:
                    if not isinstance(artifact, dict):
                        continue
                    path = artifact.get("path")
                    if path in copied_paths:
                        artifact["path"] = copied_paths[path]
                write_json(manifest_path, manifest)
            except Exception:
                pass
    try:
        Harness(project_root).rebuild_manifest_index()
    except Exception:
        pass
    return {"paths": copied_paths, "result": copied_result}


def _merge_successful_worker(
    project_root: Path,
    repo_root: Path,
    worker: dict[str, Any],
    *,
    allow_live: bool,
    allow_manual: bool,
    allow_agent: bool,
) -> dict[str, Any]:
    branch = str(worker["branch"])
    task_id = str(worker["task_id"])
    merge_result = _git(repo_root, ["merge", "--no-ff", "--no-commit", branch])
    merge_payload: dict[str, Any] = {
        "status": "merge_started" if merge_result["returncode"] == 0 else "failed",
        "message": "branch merged into index for validation" if merge_result["returncode"] == 0 else "git merge failed",
        "branch": branch,
        "merge_result": merge_result,
    }
    if merge_result["returncode"] != 0:
        _git(repo_root, ["merge", "--abort"])
        return merge_payload

    validation = _validate_merge(project_root, task_id, allow_live=allow_live, allow_manual=allow_manual, allow_agent=allow_agent)
    merge_payload["validation"] = validation
    if validation.get("status") != "passed":
        abort = _git(repo_root, ["merge", "--abort"])
        merge_payload.update(
            {
                "status": "validation_failed",
                "message": validation.get("message") or "post-merge validation failed",
                "abort": abort,
            }
        )
        return merge_payload

    staged_changed = _git(repo_root, ["diff", "--cached", "--quiet"])
    worktree_changed = _git(repo_root, ["diff", "--quiet"])
    if staged_changed["returncode"] == 0 and worktree_changed["returncode"] == 0:
        merge_payload.update(
            {
                "status": "merged",
                "message": "branch was already represented on base after validation",
                "commit": _rev_parse(repo_root, "HEAD"),
            }
        )
        return merge_payload
    commit = _git(repo_root, ["commit", "-m", f"merge(engineering): complete {task_id}"])
    if commit["returncode"] != 0:
        abort = _git(repo_root, ["merge", "--abort"])
        merge_payload.update(
            {
                "status": "commit_failed",
                "message": "merge commit failed",
                "commit_result": commit,
                "abort": abort,
            }
        )
        return merge_payload
    merge_payload.update(
        {
            "status": "merged",
            "message": f"merged task branch {branch}",
            "commit_result": commit,
            "commit": _rev_parse(repo_root, "HEAD"),
        }
    )
    return merge_payload


def _validate_merge(
    project_root: Path,
    task_id: str,
    *,
    allow_live: bool,
    allow_manual: bool,
    allow_agent: bool,
) -> dict[str, Any]:
    harness = Harness(project_root)
    try:
        task = harness.task_by_id(task_id)
    except KeyError as exc:
        return {"status": "failed", "message": str(exc), "runs": []}
    runs = []
    acceptance_status, message = harness._run_command_group(
        task.acceptance,
        phase="merge-acceptance",
        runs=runs,
        dry_run=False,
        allow_live=allow_live,
        allow_manual=allow_manual,
        allow_agent=allow_agent,
        task=task,
        persist_state=False,
    )
    status = acceptance_status
    if status == "passed" and task.e2e:
        status, message = harness._run_command_group(
            task.e2e,
            phase="merge-e2e",
            runs=runs,
            dry_run=False,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
            task=task,
            persist_state=False,
        )
    return {
        "status": status,
        "message": message,
        "runs": [harness._command_run_result_payload(task, run) for run in runs],
    }


def _copy_worker_task_state(project_root: Path, worker: dict[str, Any]) -> None:
    worktree = Path(str(worker["worktree_path"]))
    worker_state_path = worktree / ".engineering" / "state" / "harness-state.json"
    if not worker_state_path.exists():
        return
    try:
        worker_state = load_mapping(worker_state_path)
    except Exception:
        return
    task_id = str(worker["task_id"])
    worker_task_state = (
        worker_state.get("tasks", {}).get(task_id)
        if isinstance(worker_state.get("tasks"), dict)
        else None
    )
    if not isinstance(worker_task_state, dict):
        return
    harness = Harness(project_root)
    state = harness.load_state()
    task_state = state.setdefault("tasks", {}).setdefault(task_id, {})
    task_state.update(deepcopy(worker_task_state))
    task_state["status"] = str(worker.get("result_status") or task_state.get("status") or "passed")
    if worker.get("report"):
        task_state["last_report"] = worker.get("report")
    if worker.get("manifest"):
        task_state["last_manifest"] = worker.get("manifest")
    task_state["parallel_drive"] = {
        "schema_version": PARALLEL_DRIVE_SCHEMA_VERSION,
        "run_id": worker.get("run_id"),
        "branch": worker.get("branch"),
        "worktree_path": worker.get("worktree_path"),
        "merged_at": utc_now(),
        "merge": deepcopy(worker.get("merge")),
        "cleanup": deepcopy(worker.get("cleanup")),
    }
    harness.save_state(state)
    harness.rebuild_manifest_index()


def _annotate_worker_manifest(project_root: Path, worker: dict[str, Any]) -> None:
    manifest = worker.get("manifest")
    if not manifest:
        return
    path = project_root / str(manifest)
    if not path.exists():
        return
    try:
        payload = load_mapping(path)
    except Exception:
        return
    payload["parallel_drive"] = {
        "schema_version": PARALLEL_DRIVE_SCHEMA_VERSION,
        "run_id": worker.get("run_id"),
        "task_id": worker.get("task_id"),
        "worker_status": worker.get("status"),
        "branch": worker.get("branch"),
        "worktree_path": worker.get("worktree_path"),
        "result_status": worker.get("result_status"),
        "exit_code": worker.get("exit_code"),
        "merge": deepcopy(worker.get("merge")) if isinstance(worker.get("merge"), dict) else None,
        "cleanup": deepcopy(worker.get("cleanup")) if isinstance(worker.get("cleanup"), dict) else None,
        "preservation": deepcopy(worker.get("preservation"))
        if isinstance(worker.get("preservation"), dict)
        else None,
    }
    write_json(path, payload)


def _cleanup_merged_worker(repo_root: Path, worker: dict[str, Any]) -> dict[str, Any]:
    worktree_path = Path(str(worker["worktree_path"]))
    branch = str(worker["branch"])
    remove = _git(repo_root, ["worktree", "remove", "--force", str(worktree_path)])
    delete = _git(repo_root, ["branch", "-d", branch])
    status = "cleaned" if remove["returncode"] == 0 and delete["returncode"] == 0 else "partially_cleaned"
    return {
        "status": status,
        "message": "merged worktree and branch cleaned" if status == "cleaned" else "cleanup completed with warnings",
        "worktree_remove": remove,
        "branch_delete": delete,
        "worktree_exists": worktree_path.exists(),
        "branch_exists": _rev_parse(repo_root, branch) is not None,
    }


def _preservation_payload(worker: dict[str, Any], reason: Any) -> dict[str, Any]:
    return {
        "preserved": True,
        "reason": str(reason or "worker branch preserved for operator inspection"),
        "branch": worker.get("branch"),
        "worktree_path": worker.get("worktree_path"),
        "report": worker.get("report"),
        "manifest": worker.get("manifest"),
    }


def _completed_task_ids(harness: Harness, state: dict[str, Any]) -> set[str]:
    completed: set[str] = set()
    state_tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    for task in harness.iter_tasks():
        task_state = state_tasks.get(task.id) if isinstance(state_tasks.get(task.id), dict) else {}
        status = str(task_state.get("status", task.status))
        if status in COMPLETED_STATUSES:
            completed.add(task.id)
    return completed


def _roadmap_task_metadata(roadmap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for task in _roadmap_task_dicts(roadmap):
        task_id = str(task.get("id") or "").strip()
        if task_id:
            metadata[task_id] = task
    return metadata


def _roadmap_task_dicts(roadmap: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for milestone in roadmap.get("milestones", []) if isinstance(roadmap.get("milestones"), list) else []:
        if isinstance(milestone, dict) and isinstance(milestone.get("tasks"), list):
            tasks.extend(task for task in milestone["tasks"] if isinstance(task, dict))
    continuation = roadmap.get("continuation") if isinstance(roadmap.get("continuation"), dict) else {}
    for stage in continuation.get("stages", []) if isinstance(continuation.get("stages"), list) else []:
        if isinstance(stage, dict) and isinstance(stage.get("tasks"), list):
            tasks.extend(task for task in stage["tasks"] if isinstance(task, dict))
    return tasks


def _task_dependencies(task: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("depends_on", "dependencies", "depends", "after", "requires"):
        raw = task.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(raw)
    normalized = []
    for item in values:
        text = str(item).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _task_plan_payload(item: dict[str, Any]) -> dict[str, Any]:
    task = item.get("task")
    task_id = task.id if isinstance(task, HarnessTask) else item.get("task_id")
    return {
        "task_id": task_id,
        "title": task.title if isinstance(task, HarnessTask) else None,
        "status": item.get("status"),
        "attempts": item.get("attempts"),
        "max_attempts": item.get("max_attempts"),
        "file_scope": list(item.get("file_scope", [])),
        "dependencies": list(item.get("dependencies", [])),
        "skip_reasons": deepcopy(item.get("skip_reasons", [])),
    }


def _scopes_overlap(left: Any, right: Any) -> bool:
    left_patterns = [_normalize_scope_pattern(item) for item in (left or ())]
    right_patterns = [_normalize_scope_pattern(item) for item in (right or ())]
    if not left_patterns or not right_patterns:
        return True
    for left_pattern in left_patterns:
        for right_pattern in right_patterns:
            if _scope_patterns_overlap(left_pattern, right_pattern):
                return True
    return False


def _scope_patterns_overlap(left: str, right: str) -> bool:
    if left in {"", "**", "**/*"} or right in {"", "**", "**/*"}:
        return True
    if left == right:
        return True
    left_root = _static_scope_root(left)
    right_root = _static_scope_root(right)
    if not left_root or not right_root:
        return True
    if left_root == right_root:
        return True
    if left_root.startswith(f"{right_root}/") or right_root.startswith(f"{left_root}/"):
        return True
    try:
        return PurePosixPath(left_root).match(right) or PurePosixPath(right_root).match(left)
    except ValueError:
        return True


def _static_scope_root(pattern: str) -> str:
    parts = []
    for part in pattern.split("/"):
        if any(char in part for char in "*?[]"):
            break
        if part:
            parts.append(part)
    return "/".join(parts)


def _normalize_scope_pattern(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _worker_branch_name(task_id: str, run_id: str, index: int) -> str:
    short_run = _slug(run_id)[-20:]
    return f"{PARALLEL_DRIVE_BRANCH_PREFIX}/{_slug(task_id)}-{index}-{short_run}"


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip()).strip("-._")
    return text[:80] or "task"


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _parallel_summary(payload: dict[str, Any]) -> dict[str, Any]:
    workers = payload.get("workers") if isinstance(payload.get("workers"), list) else []
    status_counts: dict[str, int] = {}
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        status = str(worker.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "worker_count": len(workers),
        "status_counts": dict(sorted(status_counts.items())),
        "merged_count": status_counts.get("merged", 0),
        "preserved_count": len(
            [
                worker
                for worker in workers
                if isinstance(worker, dict) and worker.get("preservation", {}).get("preserved")
            ]
        ),
    }


def _resume_payload(existing_state: dict[str, Any] | None, *, resume: bool) -> dict[str, Any]:
    if not resume:
        return {"requested": False, "status": "not_requested"}
    if not isinstance(existing_state, dict):
        return {"requested": True, "status": "not_found", "message": "no previous parallel drive state found"}
    return {
        "requested": True,
        "status": "loaded",
        "previous_run_id": existing_state.get("run_id"),
        "previous_status": existing_state.get("status"),
        "previous_active": bool(existing_state.get("active", False)),
        "previous_workers": [
            {
                "task_id": item.get("task_id"),
                "status": item.get("status"),
                "branch": item.get("branch"),
                "worktree_path": item.get("worktree_path"),
            }
            for item in existing_state.get("workers", [])
            if isinstance(item, dict)
        ],
    }


def _serializable_worker(worker: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _json_safe(value)
        for key, value in worker.items()
        if key not in {"task", "process", "_stdout_handle", "_stderr_handle"}
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, HarnessTask):
        return value.id
    if isinstance(value, subprocess.Popen):
        return {"pid": value.pid, "returncode": value.returncode}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return str(value)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{int(time.time())}{suffix}")


def _project_relative(project_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    result = _git(repo_root, ["rev-parse", "--verify", ref])
    return result["stdout"].strip() if result["returncode"] == 0 and result["stdout"].strip() else None


def _git(cwd: Path, args: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
        "args": ["git", *args],
    }

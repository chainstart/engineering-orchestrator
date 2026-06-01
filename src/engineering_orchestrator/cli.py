from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import (
    COMPLETED_STATUSES,
    Harness,
    REPLAY_GUARD_SCHEMA_VERSION,
    discover_projects,
    init_project,
    parse_utc_timestamp,
    project_from_root,
    slug_now,
    utc_now,
)
from .docs_sync import (
    audit_documentation_system,
    has_documentation_roles,
    record_documentation_update,
)
from .goal_planner import DEFAULT_GOAL_STAGE_COUNT, materialize_goal_roadmap, plan_goal_roadmap
from .io import load_mapping, write_json
from .merge_planner import plan_parallel_merges, write_merge_plan_report
from .parallel_drive import plan_parallel_drive, run_parallel_drive
from .profiles import list_profiles
from .spec_sync import SPEC_TASKS_RELATIVE_PATH, audit_spec_system, record_spec_task_update


WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION = 1
WORKSPACE_DISPATCH_LEASE_DIRNAME = "workspace-dispatch-lease"
WORKSPACE_DISPATCH_LEASE_STALE_SECONDS_ENV = "ENGINEERING_HARNESS_WORKSPACE_DISPATCH_LEASE_STALE_AFTER_SECONDS"
DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS = 3600
WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY = "fair"
WORKSPACE_DISPATCH_PATH_ORDER_SCHEDULER_POLICY = "path-order"
WORKSPACE_DISPATCH_SCHEDULER_POLICIES = (
    WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY,
    WORKSPACE_DISPATCH_PATH_ORDER_SCHEDULER_POLICY,
)
WORKSPACE_DISPATCH_HISTORY_LIMIT = 50
WORKSPACE_DISPATCH_RECENT_HISTORY_LIMIT = 10
WORKSPACE_DISPATCH_SELECTED_COOLDOWN_SECONDS = 3600
WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS_ENV = (
    "ENGINEERING_HARNESS_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS"
)
DEFAULT_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS = 3600
MAX_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS = 86400
WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_PENALTY = -10000
DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION = 1
DAEMON_SUPERVISOR_RUNTIME_KIND = "engineering-harness.daemon-supervisor-runtime"
DAEMON_SUPERVISOR_RUNTIME_STATE_FILENAME = "daemon-supervisor-runtime.json"
DAEMON_SUPERVISOR_RUNTIME_REPORT_DIRNAME = "daemon-supervisor-runtime"
DAEMON_SUPERVISOR_RUNTIME_STALE_SECONDS_ENV = "ENGINEERING_HARNESS_DAEMON_SUPERVISOR_STALE_AFTER_SECONDS"
DEFAULT_DAEMON_SUPERVISOR_RUNTIME_STALE_SECONDS = 3600
DAEMON_SUPERVISOR_RUNTIME_HISTORY_LIMIT = 100
DOCS_SYNC_COMPLETION_STATUSES = {"completed", "done", "passed"}


def resolve_project_root(args: argparse.Namespace) -> Path:
    if getattr(args, "project_root", None):
        return Path(args.project_root).resolve()
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    project_name = getattr(args, "project", None)
    if not project_name:
        raise ValueError("Provide --project-root or --project")
    for project in discover_projects(workspace):
        if project.name == project_name or project.root.name == project_name or str(project.root).endswith(project_name):
            return project.root
    raise ValueError(f"Project not found in {workspace}: {project_name}")


def cmd_profiles(args: argparse.Namespace) -> int:
    payload = list_profiles()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in payload:
            print(f"{item['id']}: {item['description']}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    projects = discover_projects(Path(args.workspace), max_depth=args.max_depth)
    payload = [
        {
            "name": project.name,
            "root": str(project.root),
            "configured": project.configured,
            "roadmap": str(project.roadmap_path) if project.roadmap_path else None,
            "profile": project.profile,
            "kind": project.kind,
        }
        for project in projects
    ]
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in payload:
            marker = "configured" if item["configured"] else "candidate"
            profile = item["profile"] or "unknown"
            print(f"{item['name']} [{marker}, {profile}] {item['root']}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    result = init_project(Path(args.project_root), args.profile, name=args.name, force=args.force)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Initialized {result['project']} with profile {result['profile']}")
        print(f"Roadmap: {result['roadmap']}")
    return 0


def cmd_plan_goal(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    project_name = args.name or project_root.name
    goal_text = _goal_text_from_args(args)
    if args.materialize:
        result = materialize_goal_roadmap(
            project_root=project_root,
            project_name=project_name,
            profile=args.profile,
            goal_text=goal_text,
            blueprint_path=args.blueprint,
            constraints=args.constraint,
            desired_experience_kind=args.experience_kind,
            stage_count=args.stage_count,
            force=args.force,
        )
    else:
        result = plan_goal_roadmap(
            project_root=project_root,
            project_name=project_name,
            profile=args.profile,
            goal_text=goal_text,
            blueprint_path=args.blueprint,
            constraints=args.constraint,
            desired_experience_kind=args.experience_kind,
            stage_count=args.stage_count,
        )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        action = "Materialized" if result["materialized"] else "Proposed"
        print(f"{action} starter roadmap for {result['project']} ({result['profile']})")
        print(f"Roadmap: {result['roadmap_path']}")
        print(f"Experience: {result['experience'].get('kind')}")
        milestones = result["roadmap"].get("milestones", [])
        continuation = result["roadmap"].get("continuation", {})
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        print(f"Baseline milestones: {len(milestones)}")
        print(f"Continuation stages: {len(stages)}")
        if not result["materialized"]:
            print("Run again with --materialize to write .engineering/roadmap.yaml.")
    return 0


def _goal_text_from_args(args: argparse.Namespace) -> str:
    if args.goal_file:
        return Path(args.goal_file).read_text(encoding="utf-8")
    return str(args.goal or "")


def _format_spec_coverage_line(spec: dict) -> str | None:
    if not isinstance(spec, dict):
        return None
    status = str(spec.get("status") or "unknown")
    configured = bool(spec.get("configured"))
    if not configured:
        if status == "unconfigured":
            return None
        return f"Spec coverage: {status}"

    known = int(spec.get("known_requirement_count", 0) or 0)
    covered = int(spec.get("covered_requirement_count", 0) or 0)
    referenced = int(spec.get("referenced_requirement_count", 0) or 0)
    unknown = int(spec.get("unknown_requirement_count", 0) or 0)
    parts = [
        f"Spec coverage: {status}",
        f"{covered}/{known} covered",
        f"refs={referenced}",
        f"unknown={unknown}",
    ]
    path = str(spec.get("path") or "").strip()
    if path:
        parts.append(f"path={path}")
    return " ".join(parts)


def cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "workspace", None) and not getattr(args, "project_root", None) and not getattr(args, "project", None):
        summaries = []
        for project in discover_projects(Path(args.workspace), max_depth=args.max_depth):
            if not project.configured:
                continue
            try:
                summaries.append(Harness(project.root, project.roadmap_path).status_summary())
            except Exception as exc:
                summaries.append({"project": project.name, "root": str(project.root), "error": str(exc)})
        if args.json:
            print(json.dumps(summaries, indent=2, sort_keys=True))
        else:
            for summary in summaries:
                if "error" in summary:
                    print(f"{summary['project']}: error - {summary['error']}")
                    continue
                next_task = summary.get("next_task")
                next_id = next_task["id"] if next_task else "none"
                manifest_count = summary.get("manifest_index", {}).get("manifest_count", 0)
                line = f"{summary['project']}: next={next_id} manifests={manifest_count}"
                spec_line = _format_spec_coverage_line(summary.get("spec", {}))
                if spec_line:
                    line = f"{line} spec={spec_line.removeprefix('Spec coverage: ')}"
                print(f"{line} root={summary['root']}")
        return 0

    root = resolve_project_root(args)
    summary = Harness(root).status_summary()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Project: {summary['project']}")
        print(f"Profile: {summary.get('profile') or 'unknown'}")
        print(f"Root: {summary['root']}")
        print(f"Roadmap: {summary['roadmap']}")
        print(f"Run manifests: {summary.get('manifest_index', {}).get('manifest_count', 0)}")
        spec_line = _format_spec_coverage_line(summary.get("spec", {}))
        if spec_line:
            print(spec_line)
        drive_control = summary.get("drive_control", {})
        approval_queue = summary.get("approval_queue", {})
        print(f"Drive control: {drive_control.get('status', 'idle')}")
        checkpoint_readiness = (
            summary.get("checkpoint_readiness")
            if isinstance(summary.get("checkpoint_readiness"), dict)
            else {}
        )
        if checkpoint_readiness:
            print(
                "Checkpoint readiness: "
                f"{checkpoint_readiness.get('reason', 'unknown')} "
                f"blocking={str(bool(checkpoint_readiness.get('blocking'))).lower()}"
            )
        watchdog = drive_control.get("watchdog", {}) if isinstance(drive_control, dict) else {}
        if watchdog:
            print(f"Drive watchdog: {watchdog.get('status', 'idle')} - {watchdog.get('message', '')}")
        if drive_control.get("current_activity"):
            print(f"Drive activity: {drive_control.get('current_activity')}")
        if drive_control.get("current_task"):
            current_task = drive_control.get("current_task", {})
            print(f"Drive task: {current_task.get('id', 'unknown')}")
        if drive_control.get("last_progress_message"):
            print(f"Drive progress: {drive_control.get('last_progress_message')}")
        executor_diagnostics = (
            summary.get("executor_diagnostics")
            if isinstance(summary.get("executor_diagnostics"), dict)
            else {}
        )
        if executor_diagnostics:
            print(
                "Executors: "
                f"ready={executor_diagnostics.get('ready_count', 0)} "
                f"action_required={executor_diagnostics.get('action_required_count', 0)}"
            )
            openhands = next(
                (
                    item
                    for item in executor_diagnostics.get("executors", [])
                    if isinstance(item, dict) and item.get("id") == "openhands"
                ),
                None,
            )
            if openhands:
                print(f"OpenHands executor: {openhands.get('status', 'unknown')}")
        print(f"Pending approvals: {approval_queue.get('pending_count', 0)}")
        print(f"Stale approvals: {approval_queue.get('stale_count', 0)}")
        runtime_dashboard = summary.get("runtime_dashboard") if isinstance(summary.get("runtime_dashboard"), dict) else {}
        if runtime_dashboard:
            frontend = (
                runtime_dashboard.get("domain_frontend")
                if isinstance(runtime_dashboard.get("domain_frontend"), dict)
                else {}
            )
            if frontend:
                print(
                    "Frontend plan: "
                    f"{frontend.get('experience_kind', 'unknown')} "
                    f"domain={frontend.get('domain', 'unknown')} "
                    f"status={frontend.get('status', 'unknown')}"
                )
            browser_ux = (
                runtime_dashboard.get("browser_user_experience")
                if isinstance(runtime_dashboard.get("browser_user_experience"), dict)
                else {}
            )
            if browser_ux:
                print(
                    "Browser UX gates: "
                    f"{browser_ux.get('status', 'unknown')} "
                    f"configured={browser_ux.get('configured_gate_count', 0)} "
                    f"journeys={browser_ux.get('journey_count', 0)}"
                )
            current_task = runtime_dashboard.get("current_task") if isinstance(runtime_dashboard.get("current_task"), dict) else None
            if current_task:
                phase = current_task.get("phase") or "none"
                print(f"Runtime task: {current_task.get('id', 'unknown')} phase={phase}")
            failure_isolation = (
                runtime_dashboard.get("failure_isolation")
                if isinstance(runtime_dashboard.get("failure_isolation"), dict)
                else {}
            )
            print(f"Unresolved isolated failures: {failure_isolation.get('unresolved_count', 0)}")
            workspace_dispatch = (
                runtime_dashboard.get("workspace_dispatch")
                if isinstance(runtime_dashboard.get("workspace_dispatch"), dict)
                else {}
            )
            print(f"Workspace dispatch: {workspace_dispatch.get('status', 'not_found')}")
            daemon_supervisor = (
                runtime_dashboard.get("daemon_supervisor_runtime")
                if isinstance(runtime_dashboard.get("daemon_supervisor_runtime"), dict)
                else {}
            )
            if daemon_supervisor:
                stop_reason = (
                    daemon_supervisor.get("stop_reason")
                    if isinstance(daemon_supervisor.get("stop_reason"), dict)
                    else {}
                )
                print(
                    "Daemon supervisor: "
                    f"{daemon_supervisor.get('status', 'not_found')} "
                    f"stop={stop_reason.get('code', 'none')}"
                )
            parallel_drive = (
                runtime_dashboard.get("parallel_drive")
                if isinstance(runtime_dashboard.get("parallel_drive"), dict)
                else {}
            )
            if parallel_drive and parallel_drive.get("status") != "not_found":
                print(
                    "Parallel drive: "
                    f"{parallel_drive.get('status', 'unknown')} "
                    f"merged={parallel_drive.get('tasks_merged', 0)} "
                    f"preserved={parallel_drive.get('tasks_preserved', 0)}"
                )
            goal_gap = runtime_dashboard.get("goal_gap") if isinstance(runtime_dashboard.get("goal_gap"), dict) else {}
            actions = goal_gap.get("next_actions") if isinstance(goal_gap.get("next_actions"), list) else []
            if actions and isinstance(actions[0], dict):
                print(f"Goal-gap next action: {actions[0].get('id')} - {actions[0].get('title')}")
        print("")
        for milestone in summary["milestones"]:
            print(
                f"- {milestone['id']}: {milestone['done']}/{milestone['total']} done, "
                f"{milestone['pending']} pending, {milestone['failed']} failed, {milestone['blocked']} blocked"
            )
        next_task = summary.get("next_task")
        print("")
        print(f"Next task: {next_task['id']} - {next_task['title']}" if next_task else "Next task: none")
    return 0


def cmd_operator_console(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    payload = harness.write_operator_console_artifact() if args.write else harness.operator_console_summary()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        queue = payload.get("queue_state", {}) if isinstance(payload.get("queue_state"), dict) else {}
        failures = payload.get("failures", {}) if isinstance(payload.get("failures"), dict) else {}
        approvals = payload.get("approvals", {}) if isinstance(payload.get("approvals"), dict) else {}
        artifact = payload.get("artifact", {}) if isinstance(payload.get("artifact"), dict) else {}
        print(f"Project: {payload.get('project')}")
        print(f"Snapshot: {payload.get('snapshot_at') or 'none'}")
        print(f"Pending tasks: {queue.get('pending_count', 0)}")
        print(f"Pending approvals: {approvals.get('pending_count', 0)}")
        print(f"Unresolved failures: {failures.get('unresolved_count', 0)}")
        if args.write:
            print(f"Operator console JSON: {artifact.get('json_path')}")
            print(f"Operator console report: {artifact.get('markdown_path')}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    payload = harness.validate_roadmap()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Validation: {payload['status']}")
        print(f"Errors: {payload['error_count']}")
        for error in payload["errors"]:
            print(f"- error: {error}")
        print(f"Warnings: {payload['warning_count']}")
        for warning in payload["warnings"]:
            print(f"- warning: {warning}")
    return 0 if payload["status"] == "passed" else 1


def cmd_next(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    payload = harness.task_payload(harness.next_task())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload is None:
        print("No pending task.")
    else:
        print(f"{payload['id']}: {payload['title']}")
        print(f"Milestone: {payload['milestone_id']}")
        print("Acceptance:")
        for command in payload["acceptance"]:
            print(f"- {command['name']}: {command['command']}")
    return 0


def cmd_merge_plan(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    payload = plan_parallel_merges(
        root,
        roadmap=harness.roadmap,
        base_ref=args.base,
        branches=args.branch or (),
        worktrees=[str(path) for path in (args.worktree or ())],
        task_ids=args.task or (),
        post_merge_acceptance=args.post_merge_acceptance or (),
    )
    if args.write:
        write_merge_plan_report(root, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Merge planner: {payload['status']}")
        print(f"Base: {payload.get('base', {}).get('ref') or 'unknown'}")
        print(f"Candidates: {payload.get('candidate_count', 0)}")
        print(f"Dirty paths: {len(payload.get('dirty_paths', []))}")
        print(f"Changed files: {len(payload.get('changed_files', []))}")
        conflicts = payload.get("likely_conflicts", {}) if isinstance(payload.get("likely_conflicts"), dict) else {}
        print(f"Likely conflicts: {conflicts.get('count', 0)}")
        if payload.get("merge_plan_report"):
            print(f"Report: {payload['merge_plan_report']}")
            print(f"Report JSON: {payload['merge_plan_report_json']}")
        order = payload.get("recommended_merge_order", [])
        if order:
            print("Merge order:")
            for item in order:
                print(f"- {item['position']}: {item['candidate_id']}")
        acceptance = payload.get("post_merge_acceptance", {})
        print(f"Post-merge acceptance commands: {acceptance.get('command_count', 0)}")
    return 1 if payload.get("status") == "failed" else 0


def cmd_parallel_drive(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    if args.plan_only:
        payload = plan_parallel_drive(
            root,
            max_workers=args.max_workers,
            max_tasks=args.max_tasks,
            base_ref=args.base,
            allow_dirty_worktree=args.allow_dirty_worktree,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Parallel drive plan: {payload['status']} - {payload['message']}")
            print(f"Selected tasks: {payload.get('selected_count', 0)}")
            for task in payload.get("selected_tasks", []):
                print(f"- {task['task_id']}: {', '.join(task.get('file_scope', []))}")
        return 0 if payload.get("status") in {"planned", "empty"} else 1

    exit_code, payload = run_parallel_drive(
        root,
        max_workers=args.max_workers,
        max_tasks=args.max_tasks,
        time_budget_seconds=args.time_budget_seconds,
        base_ref=args.base,
        allow_dirty_worktree=args.allow_dirty_worktree,
        allow_live=args.allow_live,
        allow_manual=args.allow_manual,
        allow_agent=args.allow_agent,
        poll_interval_seconds=args.poll_interval_seconds,
        resume=args.resume,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Parallel drive: {payload['status']} - {payload['message']}")
        print(f"Workers: {len(payload.get('workers', []))}")
        print(f"Report: {payload.get('parallel_drive_report')}")
        print(f"Report JSON: {payload.get('parallel_drive_report_json')}")
    return exit_code


def cmd_run(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    results = []
    seen_task_ids: set[str] = set()
    for _ in range(args.max_tasks):
        task = harness.task_by_id(args.task) if args.task else harness.next_task()
        if task is None or task.id in seen_task_ids:
            break
        seen_task_ids.add(task.id)
        result = harness.run_task(
            task,
            dry_run=args.dry_run,
            allow_live=args.allow_live,
            allow_manual=args.allow_manual,
            allow_agent=args.allow_agent,
        )
        if not args.dry_run:
            result["spec_sync"] = maybe_sync_completed_spec_task(root, task, result, phase="run")
            result["docs_sync"] = maybe_sync_completed_documentation(root, task, result, phase="run")
            attach_post_task_sync_evidence(root, result)
            harness.rebuild_manifest_index()
        maybe_checkpoint_task(harness, task, result, args, dry_run=args.dry_run)
        results.append(result)
        if args.task or args.dry_run or result["status"] not in COMPLETED_STATUSES:
            break
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        if not results:
            print("No pending task.")
        for result in results:
            print(f"{result['task']['id']}: {result['status']} - {result['message']}")
            print(f"Report: {result['report']}")
    if any(result["status"] in {"failed", "blocked"} for result in results):
        return 1
    return 0


def maybe_checkpoint_task(
    harness: Harness,
    task,
    result: dict,
    args: argparse.Namespace,
    *,
    dry_run: bool = False,
    checkpoint_defer: dict | None = None,
) -> None:
    if dry_run or not checkpoint_requested(args) or result["status"] not in COMPLETED_STATUSES:
        return
    harness.drive_heartbeat(activity="git-checkpoint", message=f"checkpointing task {task.id}", task=task)
    if checkpoint_defer:
        result["git"] = harness.defer_git_checkpoint(
            task,
            reason=str(checkpoint_defer.get("message") or "task checkpoint deferred by roadmap materialization boundary"),
            metadata=checkpoint_defer,
        )
        return
    result["git"] = harness.git_checkpoint(
        task,
        push=bool(getattr(args, "push_after_task", False)),
        remote=str(getattr(args, "git_remote", "origin")),
        branch=getattr(args, "git_branch", None),
        message_template=str(getattr(args, "git_message_template", "chore(engineering): complete {task_id}")),
    )


def checkpoint_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "commit_after_task", False) or getattr(args, "push_after_task", False))


def materialization_checkpoint_should_defer_tasks(checkpoint: dict) -> bool:
    if checkpoint.get("status") in {"committed"}:
        return bool(checkpoint.get("push") and checkpoint.get("push_status") == "failed")
    return checkpoint.get("status") in {"deferred", "failed"}


def materialization_task_checkpoint_defer_payload(checkpoint: dict) -> dict:
    reason = str(checkpoint.get("reason") or checkpoint.get("status") or "unknown")
    dirty_before = checkpoint.get("dirty_before_paths") or []
    unrelated = checkpoint.get("unrelated_dirty_paths") or []
    if reason == "preexisting_dirty_worktree":
        detail = "pre-existing user changes were present before roadmap materialization"
    elif reason in {"preexisting_unrelated_dirty_paths", "checkpoint_readiness_blocked"}:
        detail = "unrelated dirty paths were present before roadmap materialization"
    elif reason == "unrelated_dirty_paths":
        detail = "unrelated dirty paths appeared beside the orchestrator-owned roadmap materialization"
    elif reason == "git_push_failed":
        detail = "the roadmap materialization commit could not be pushed before generated tasks"
    else:
        detail = f"roadmap materialization checkpoint status was {checkpoint.get('status', 'unknown')}"
    if dirty_before:
        detail = f"{detail}: {', '.join(str(path) for path in dirty_before[:8])}"
    elif unrelated:
        detail = f"{detail}: {', '.join(str(path) for path in unrelated[:8])}"
    return {
        "message": f"task checkpoint deferred because roadmap materialization checkpoint was deferred: {detail}",
        "materialization_checkpoint_status": checkpoint.get("status"),
        "materialization_checkpoint_reason": checkpoint.get("reason"),
        "materialization_paths": checkpoint.get("materialization_paths", []),
        "dirty_before_paths": dirty_before,
        "unrelated_dirty_paths": unrelated,
        "materialization_checkpoint": checkpoint,
    }


def checkpoint_requested_payload(payload: dict) -> bool:
    if payload.get("checkpoint_requested"):
        return True
    if any("git" in result for result in payload.get("results", [])):
        return True
    return any("materialization_checkpoint_intent" in item for item in payload.get("continuations", []))


def drive_replay_guard_payload(payload: dict, final_status: dict) -> dict:
    result_guards = [
        deepcopy(result["replay_guard"])
        for result in payload.get("results", [])
        if isinstance(result, dict) and isinstance(result.get("replay_guard"), dict)
    ]
    reused_phases: list[dict] = []
    for guard in result_guards:
        phases = guard.get("reused_phases")
        if isinstance(phases, list):
            reused_phases.extend(deepcopy(item) for item in phases if isinstance(item, dict))
    status_guard = (
        deepcopy(final_status.get("replay_guard"))
        if isinstance(final_status.get("replay_guard"), dict)
        else {}
    )
    if not reused_phases:
        phases = status_guard.get("reused_phases")
        if isinstance(phases, list):
            reused_phases.extend(deepcopy(item) for item in phases if isinstance(item, dict))
    return {
        "schema_version": REPLAY_GUARD_SCHEMA_VERSION,
        "kind": "engineering-harness.drive-replay-guard",
        "status": "reused" if reused_phases else "none",
        "result_count": len(result_guards),
        "reused_phase_count": len(reused_phases),
        "reused_phases": reused_phases,
        "result_guards": result_guards,
        "status_summary": status_guard,
    }


def write_drive_report(harness: Harness, payload: dict) -> str:
    report_dir = harness.report_dir / "drives"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{slug_now()}-drive.md"
    json_path = report_path.with_suffix(".json")
    payload["drive_report"] = str(report_path.relative_to(harness.project_root))
    payload["drive_report_json"] = str(json_path.relative_to(harness.project_root))
    final_status = payload.get("final_status") if isinstance(payload.get("final_status"), dict) else {}
    payload["approval_queue"] = (
        final_status.get("approval_queue")
        if isinstance(final_status.get("approval_queue"), dict)
        else payload.get("approval_queue", {})
    )
    payload["failure_isolation"] = harness.drive_failure_isolation_summary(
        payload,
        final_status=final_status if final_status else None,
    )
    payload["replay_guard"] = drive_replay_guard_payload(payload, final_status)
    lines = [
        "# Harness Drive Report",
        "",
        f"- Project: `{payload['project']}`",
        f"- Status: `{payload['status']}`",
        f"- Started: {payload['started_at']}",
        f"- Finished: {payload['finished_at']}",
        f"- Tasks run: {len(payload['results'])}",
        f"- Message: {payload['message']}",
        "",
        "## Stale Running Recovery",
        "",
    ]
    drive_control = final_status.get("drive_control") if isinstance(final_status.get("drive_control"), dict) else {}
    recovery = payload.get("stale_running_recovery")
    if not isinstance(recovery, dict):
        recovery = (
            drive_control.get("stale_running_recovery")
            if isinstance(drive_control.get("stale_running_recovery"), dict)
            else None
        )
    preflight = payload.get("stale_running_preflight")
    if not isinstance(preflight, dict):
        preflight = (
            drive_control.get("stale_running_preflight")
            if isinstance(drive_control.get("stale_running_preflight"), dict)
            else None
        )
    block = (
        drive_control.get("stale_running_block")
        if isinstance(drive_control.get("stale_running_block"), dict)
        else None
    )
    if isinstance(recovery, dict):
        lines.extend(
            [
                f"- Status: `{recovery.get('status')}`",
                f"- Reason: `{recovery.get('reason')}`",
                f"- Previous pid: `{recovery.get('previous_pid') or 'unknown'}`",
                f"- Heartbeat age seconds: `{recovery.get('heartbeat_age_seconds')}`",
                f"- Threshold seconds: `{recovery.get('threshold_seconds')}`",
                f"- Recovered at: {recovery.get('recovered_at')}",
                f"- Recommended follow-up: {recovery.get('recommended_follow_up')}",
                "",
                "Machine-readable stale running recovery:",
                "",
                "```json",
                json.dumps(recovery, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    elif isinstance(block, dict) or (isinstance(preflight, dict) and preflight.get("status") == "blocked"):
        block_payload = block if isinstance(block, dict) else preflight
        lines.extend(
            [
                f"- Status: `{block_payload.get('status')}`",
                f"- Reason: `{block_payload.get('reason')}`",
                f"- Message: {block_payload.get('message')}",
                "",
                "Machine-readable stale running preflight:",
                "",
                "```json",
                json.dumps(block_payload, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    else:
        lines.extend(["No stale running recovery was needed for this drive.", ""])
    replay_guard = payload.get("replay_guard", {})
    reused_phases = replay_guard.get("reused_phases") if isinstance(replay_guard, dict) else []
    lines.extend(
        [
            "## Phase Replay Guard",
            "",
            f"- Status: `{replay_guard.get('status', 'none') if isinstance(replay_guard, dict) else 'none'}`",
            f"- Reused phases: `{len(reused_phases) if isinstance(reused_phases, list) else 0}`",
            "",
            "Machine-readable replay guard:",
            "",
            "```json",
            json.dumps(replay_guard, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    lines.extend(
        [
        "## Task Results",
        "",
        ]
    )
    if not payload["results"]:
        lines.append("No task was executed.")
    for result in payload["results"]:
        task = result["task"]
        lines.extend(
            [
                f"- `{task['id']}`: `{result['status']}` - {result['message']}",
                f"  - Report: `{result['report']}`",
            ]
        )
        if "git" in result:
            git = result["git"]
            lines.append(f"  - Git: `{git.get('status')}` - {git.get('message')}")
            if git.get("commit"):
                lines.append(f"  - Commit: `{git['commit']}`")
            if git.get("push_status"):
                lines.append(f"  - Push: `{git.get('push_status')}` `{git.get('push_remote')}/{git.get('push_branch')}`")
        if isinstance(result.get("spec_sync"), dict):
            spec_sync = result["spec_sync"]
            lines.append(f"  - Spec sync: `{spec_sync.get('status')}` - {spec_sync.get('reason', '')}")
        if isinstance(result.get("docs_sync"), dict):
            docs_sync = result["docs_sync"]
            lines.append(f"  - Docs sync: `{docs_sync.get('status')}` - {docs_sync.get('reason', '')}")
            if docs_sync.get("docs_update_log"):
                lines.append(f"  - Docs update log: `{docs_sync.get('docs_update_log')}`")
    continuations = payload.get("continuations", [])
    lines.extend(["", "## Continuations", ""])
    if not continuations:
        lines.append("No continuation was requested.")
    for item in continuations:
        lines.extend(
            [
                f"- `{item['status']}` - {item['message']}",
                f"  - Tasks added: `{item.get('tasks_added', 0)}`",
            ]
        )
        for milestone in item.get("milestones_added", []):
            lines.append(f"  - Milestone: `{milestone.get('id')}` {milestone.get('title')} ({milestone.get('tasks')} task(s))")
        readiness = item.get("checkpoint_readiness_before_materialization")
        if isinstance(readiness, dict):
            lines.append(
                "  - Checkpoint readiness before materialization: "
                f"`{readiness.get('reason')}` blocking=`{str(bool(readiness.get('blocking'))).lower()}`"
            )
        intent = item.get("materialization_checkpoint_intent")
        if intent:
            lines.append(
                "  - Materialization checkpoint intent: "
                f"`{intent.get('status')}` - {intent.get('message')}"
            )
        checkpoint = item.get("materialization_checkpoint")
        if checkpoint:
            lines.append(
                "  - Materialization checkpoint: "
                f"`{checkpoint.get('status')}` - {checkpoint.get('message')}"
            )
            if checkpoint.get("reason"):
                lines.append(f"  - Materialization checkpoint reason: `{checkpoint.get('reason')}`")
            if checkpoint.get("commit"):
                lines.append(f"  - Materialization commit: `{checkpoint.get('commit')}`")
            if checkpoint.get("dirty_before_paths"):
                dirty = ", ".join(str(path) for path in checkpoint.get("dirty_before_paths", [])[:8])
                lines.append(f"  - Dirty before materialization: `{dirty}`")
    lines.extend(["", "## Checkpoint Boundaries", ""])
    if not checkpoint_requested_payload(payload):
        lines.append("Task checkpointing was not requested for this drive.")
    else:
        lines.append(
            "Rolling roadmap materialization is checkpointed before generated task execution when it can be "
            "isolated from pre-existing user changes."
        )
        deferred_results = [
            result
            for result in payload.get("results", [])
            if (result.get("git") or {}).get("status") == "deferred"
        ]
        if not deferred_results:
            lines.append("No task checkpoint deferral was recorded.")
        for result in deferred_results:
            task = result.get("task", {})
            git = result.get("git", {})
            lines.append(f"- Task `{task.get('id')}` checkpoint deferred: {git.get('message')}")
    checkpoint_readiness = payload.get("checkpoint_readiness") if isinstance(payload.get("checkpoint_readiness"), dict) else {}
    lines.extend(["", "## Checkpoint Readiness", ""])
    if not checkpoint_readiness:
        lines.append("No checkpoint readiness evidence was recorded.")
    else:
        lines.extend(
            [
                f"- Ready: `{str(bool(checkpoint_readiness.get('ready'))).lower()}`",
                f"- Blocking: `{str(bool(checkpoint_readiness.get('blocking'))).lower()}`",
                f"- Reason: `{checkpoint_readiness.get('reason')}`",
                f"- Dirty paths: `{len(checkpoint_readiness.get('dirty_paths', []))}`",
                f"- Blocking paths: `{len(checkpoint_readiness.get('blocking_paths', []))}`",
                f"- Safe-to-checkpoint paths: `{len(checkpoint_readiness.get('safe_to_checkpoint_paths', []))}`",
                f"- Recommended action: {checkpoint_readiness.get('recommended_action')}",
                "",
                "```json",
                json.dumps(checkpoint_readiness, indent=2, sort_keys=True),
                "```",
            ]
        )
    self_iterations = payload.get("self_iterations", [])
    lines.extend(["", "## Self Iterations", ""])
    if not self_iterations:
        lines.append("No self-iteration planner was requested.")
    for item in self_iterations:
        lines.extend(
            [
                f"- `{item['status']}` - {item['message']}",
                f"  - Stages: `{item.get('stage_count_before')}` -> `{item.get('stage_count_after')}`",
                f"  - Pending stages after: `{item.get('pending_stage_count_after')}`",
            ]
        )
        if item.get("report"):
            lines.append(f"  - Report: `{item['report']}`")
        readiness = item.get("checkpoint_readiness") if isinstance(item.get("checkpoint_readiness"), dict) else {}
        if readiness:
            lines.append(
                "  - Checkpoint readiness: "
                f"`{readiness.get('reason')}` "
                f"blocking=`{str(bool(readiness.get('blocking'))).lower()}` "
                f"dirty_paths=`{len(readiness.get('dirty_paths', []))}` "
                f"blocking_paths=`{len(readiness.get('blocking_paths', []))}`"
            )
            if readiness.get("recommended_action"):
                lines.append(f"  - Recommended operator action: {readiness.get('recommended_action')}")
    approval_queue = payload.get("approval_queue", {})
    lines.extend(["", "## Approval Leases", ""])
    if not approval_queue:
        lines.append("No approval queue state was recorded.")
    else:
        lines.extend(
            [
                f"- Pending approvals: `{approval_queue.get('pending_count', 0)}`",
                f"- Approved leases: `{approval_queue.get('approved_count', 0)}`",
                f"- Stale approvals: `{approval_queue.get('stale_count', 0)}`",
                f"- Lease TTL seconds: `{approval_queue.get('lease_ttl_seconds')}`",
                "",
            ]
        )
        stale_reasons = approval_queue.get("stale_reasons", {})
        if isinstance(stale_reasons, dict) and stale_reasons:
            lines.extend(["Stale reasons:", ""])
            for reason, count in sorted(stale_reasons.items()):
                lines.append(f"- `{count}` {reason}")
            lines.append("")
        lines.extend(
            [
                "Machine-readable approval queue:",
                "",
                "```json",
                json.dumps(approval_queue, indent=2, sort_keys=True),
                "```",
            ]
        )
    failure_isolation = payload.get("failure_isolation", {})
    lines.extend(["", "## Failure Isolation", ""])
    if not failure_isolation or int(failure_isolation.get("unresolved_count", 0) or 0) == 0:
        lines.append("No unresolved isolated task failure was recorded.")
    else:
        lines.append(f"Unresolved isolated failures: `{failure_isolation.get('unresolved_count', 0)}`")
        for item in failure_isolation.get("latest_isolated_failures", []):
            lines.append(
                "- "
                f"`{item.get('task_id')}` `{item.get('phase')}` `{item.get('failure_kind')}` - "
                f"{item.get('local_next_action')}"
            )
    lines.extend(
        [
            "",
            "Machine-readable failure isolation:",
            "",
            "```json",
            json.dumps(failure_isolation, indent=2, sort_keys=True),
            "```",
        ]
    )
    retrospective = payload.get("goal_gap_retrospective")
    lines.extend(["", "## Goal-Gap Retrospective", ""])
    if not retrospective:
        lines.append("No goal-gap retrospective was generated.")
    else:
        request = retrospective.get("request_self_iteration", {})
        recommendation = "yes" if request.get("recommended") else "no"
        lines.extend(
            [
                f"- Goal: {retrospective.get('goal')}",
                f"- Stop class: `{retrospective.get('trigger', {}).get('stop_class')}`",
                f"- Request self-iteration: `{recommendation}` - {request.get('reason')}",
                "",
                "Completed reliability capabilities:",
                "",
            ]
        )
        capabilities = retrospective.get("completed_reliability_capabilities", [])
        if not capabilities:
            lines.append("- None recorded.")
        for item in capabilities:
            lines.append(f"- `{item.get('id')}`: {item.get('title')} ({item.get('detail')})")
        lines.extend(["", "Remaining risks:", ""])
        risks = retrospective.get("remaining_risks", [])
        if not risks:
            lines.append("- None recorded.")
        for item in risks:
            lines.append(f"- `{item.get('severity')}` `{item.get('id')}`: {item.get('summary')}")
        lines.extend(["", "Likely next stage themes:", ""])
        themes = retrospective.get("likely_next_stage_themes", [])
        if not themes:
            lines.append("- None recorded.")
        for item in themes:
            sources = ", ".join(str(source) for source in item.get("source_risks", []))
            lines.append(f"- `{item.get('id')}`: {item.get('title')} ({sources})")
        lines.extend(
            [
                "",
                "Machine-readable retrospective:",
                "",
                "```json",
                json.dumps(retrospective, indent=2, sort_keys=True),
                "```",
            ]
        )
    lines.extend(["", "## Final Status", "", "```json", json.dumps(payload["final_status"], indent=2, sort_keys=True), "```", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(json_path, payload)
    return payload["drive_report"]


def cmd_advance(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    result = harness.advance_roadmap(max_new_milestones=args.max_new_milestones, reason=args.reason)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Advance status: {result['status']} - {result['message']}")
        print(f"Tasks added: {result.get('tasks_added', 0)}")
        for milestone in result.get("milestones_added", []):
            print(f"- {milestone['id']}: {milestone['title']} ({milestone['tasks']} task(s))")
    return 0 if result["status"] in {"advanced", "exhausted", "disabled"} else 1


def cmd_frontend_tasks(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    if args.materialize:
        result = harness.materialize_frontend_tasks(milestone_id=args.milestone_id, reason=args.reason)
    else:
        result = harness.frontend_task_plan(milestone_id=args.milestone_id)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Frontend tasks: {result['status']} - {result['message']}")
        experience = result.get("experience") or {}
        if experience:
            print(f"Experience: {experience.get('kind') or 'unknown'} ({experience.get('source') or 'unknown'})")
        milestone = result.get("milestone") or {}
        if milestone:
            print(f"Milestone: {milestone.get('id')} - {milestone.get('title')}")
        for task in result.get("tasks", []):
            print(f"- {task.get('id')}: {task.get('title')}")
            print(f"  file_scope: {', '.join(task.get('file_scope', []))}")
            print(f"  acceptance: {len(task.get('acceptance', []))} check(s)")
            print(f"  e2e: {len(task.get('e2e', []))} journey check(s)")
        if not args.materialize and result.get("status") == "proposed":
            print("Run again with --materialize to append the milestone to the roadmap.")
    return 0 if result["status"] in {"proposed", "materialized", "skipped"} else 1


def cmd_spec_backlog(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    if args.from_stage < 1:
        raise ValueError("--from-stage must be positive")
    source_args = []
    for attr in ("source", "spec"):
        values = getattr(args, attr, None)
        if values:
            source_args.extend(values)
    source_paths = [str(source) for source in source_args] if source_args else None
    if args.materialize:
        result = harness.materialize_spec_backlog(
            source_paths=source_paths,
            include_blueprint=args.include_blueprint,
            from_stage=args.from_stage,
            reason=args.reason,
        )
    else:
        result = harness.spec_backlog_plan(
            source_paths=source_paths,
            include_blueprint=args.include_blueprint,
            from_stage=args.from_stage,
        )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Spec backlog: {result['status']} - {result.get('message', 'proposal ready')}")
        print(f"Sources: {result.get('source_count', 0)}")
        print(f"Stages: {result.get('stage_count', 0)}")
        print(f"Tasks: {result.get('task_count', 0)}")
        skipped = int(result.get("skipped_stage_count", 0) or 0)
        if skipped:
            print(f"Skipped existing stages: {skipped}")
        for stage in result.get("stages", []):
            print(f"- {stage['id']}: {stage['title']} ({len(stage.get('tasks', []))} task(s))")
        if not args.materialize:
            print("Run again with --materialize to append these stages to continuation.stages.")
    return 0 if result["status"] in {"proposed", "materialized", "up_to_date"} else 1


def cmd_spec_sync(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    if args.spec_sync_action == "audit":
        result = audit_spec_system(root)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"Spec sync audit: {result['status']}")
            print(f"Requirements: {result.get('requirement_count', 0)}")
            print(f"Tasks: {result.get('task_count', 0)}")
            for check in result.get("checks", []):
                print(f"- {check.get('status')}: {check.get('name')} - {check.get('message')}")
        return 0 if result["status"] in {"passed", "warning"} else 1

    if args.spec_sync_action == "record":
        result = record_spec_task_update(
            root,
            task_id=args.task_id,
            status=args.status,
            evidence=args.evidence,
            requirement_ids=args.requirement_id,
            note=args.note,
            actor=args.actor,
            phase=args.phase,
            stage_id=args.stage_id,
            apply=args.apply,
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            action = "Updated" if result["status"] == "updated" else "Proposed update for"
            print(f"{action} spec task {result['task_id']}: {result['previous_status']} -> {result['new_status']}")
            if not args.apply:
                print("Run again with --apply to write the task ledger and update log.")
        return 0

    raise ValueError(f"Unknown spec-sync action: {args.spec_sync_action}")


def cmd_docs_sync(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    if args.docs_sync_action == "audit":
        result = audit_documentation_system(root)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"Docs sync audit: {result['status']}")
            print(f"Documentation roles: {result.get('documentation_role_count', 0)}")
            print(f"Tasks: {result.get('task_count', 0)}")
            for check in result.get("checks", []):
                print(f"- {check.get('status')}: {check.get('name')} - {check.get('message')}")
        return 0 if result["status"] in {"passed", "warning"} else 1

    if args.docs_sync_action in {"propose", "record"}:
        result = record_documentation_update(
            root,
            task_id=args.task_id,
            status=args.status,
            evidence=args.evidence,
            requirement_ids=args.requirement_id,
            note=args.note,
            actor=args.actor,
            phase=args.phase,
            stage_id=args.stage_id,
            architecture_change=args.architecture_change,
            apply=args.apply if args.docs_sync_action == "record" else False,
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            if result["status"] == "applied":
                print(f"Applied docs sync for {result['task_id']}: {result.get('new_status')}")
                print(f"Docs update log: {result.get('docs_update_log')}")
            elif result["status"] == "proposed":
                print(f"Proposed docs sync for {result['task_id']}: {result.get('new_status')}")
                print(f"Applyable actions: {result.get('applyable_action_count', 0)}")
                if args.docs_sync_action == "record" and not args.apply:
                    print("Run again with --apply to write safe managed blocks and the docs update log.")
            else:
                print(f"Docs sync {result['status']}: {result.get('reason')} - {result.get('message', '')}")
        return 0 if result["status"] in {"applied", "proposed", "skipped"} else 1

    raise ValueError(f"Unknown docs-sync action: {args.docs_sync_action}")


def maybe_sync_completed_spec_task(root: Path, task, result: dict, *, phase: str = "task") -> dict:
    if result.get("status") not in COMPLETED_STATUSES:
        return {"status": "skipped", "reason": "task_not_completed"}
    if not (root / SPEC_TASKS_RELATIVE_PATH).exists():
        return {"status": "skipped", "reason": "spec_tasks_not_configured"}
    evidence = []
    if result.get("report"):
        evidence.append(f"task report: {result['report']}")
    if result.get("manifest"):
        evidence.append(f"task manifest: {result['manifest']}")
    if result.get("message"):
        evidence.append(f"task result: {result['message']}")
    try:
        return record_spec_task_update(
            root,
            task_id=task.id,
            status="completed",
            evidence=evidence,
            requirement_ids=list(getattr(task, "spec_refs", ()) or ()),
            note="Recorded automatically after a completed orchestrator task.",
            actor="engineering-harness:auto",
            phase=phase,
            apply=True,
        )
    except KeyError as exc:
        return {"status": "skipped", "reason": "task_not_tracked_in_spec_tasks", "message": str(exc)}
    except Exception as exc:
        return {"status": "failed", "reason": "spec_sync_error", "message": str(exc)}


def maybe_sync_completed_documentation(root: Path, task, result: dict, *, phase: str = "task") -> dict:
    if result.get("status") not in DOCS_SYNC_COMPLETION_STATUSES:
        return {"status": "skipped", "reason": "task_not_completed"}
    if not (root / SPEC_TASKS_RELATIVE_PATH).exists():
        return {"status": "skipped", "reason": "spec_tasks_not_configured"}
    if not has_documentation_roles(root):
        return {"status": "skipped", "reason": "documentation_roles_not_configured"}
    evidence = []
    if result.get("report"):
        evidence.append(f"task report: {result['report']}")
    if result.get("manifest"):
        evidence.append(f"task manifest: {result['manifest']}")
    if result.get("message"):
        evidence.append(f"task result: {result['message']}")
    successful_runs = [
        run
        for run in result.get("runs", [])
        if isinstance(run, dict)
        and run.get("status") in DOCS_SYNC_COMPLETION_STATUSES
        and run.get("returncode") in (None, 0)
    ]
    if not successful_runs:
        return {
            "status": "blocked",
            "reason": "completion_evidence_required",
            "message": "documentation sync requires at least one successful local command run",
        }
    for run in successful_runs:
        evidence.append(f"{run.get('phase', 'phase')} {run.get('name', 'command')}: {run.get('status')}")
    try:
        return record_documentation_update(
            root,
            task_id=task.id,
            status="completed",
            evidence=evidence,
            requirement_ids=list(getattr(task, "spec_refs", ()) or ()),
            note="Recorded automatically after a completed orchestrator task.",
            actor="engineering-harness:auto",
            phase=phase,
            apply=True,
        )
    except Exception as exc:
        return {"status": "blocked", "reason": "docs_sync_error", "message": str(exc)}


def attach_post_task_sync_evidence(root: Path, result: dict) -> None:
    manifest_value = str(result.get("manifest") or "")
    report_value = str(result.get("report") or "")
    sync_payload = {
        key: result.get(key)
        for key in ("spec_sync", "docs_sync")
        if isinstance(result.get(key), dict)
    }
    if not sync_payload:
        return
    manifest_path = root / manifest_value if manifest_value else None
    if manifest_path is not None and manifest_path.is_file():
        manifest = load_mapping(manifest_path)
        manifest.update(sync_payload)
        manifest["target_sync"] = sync_payload
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
        docs_sync = sync_payload.get("docs_sync", {})
        if isinstance(docs_sync, dict):
            for path in _docs_sync_artifact_paths(docs_sync):
                artifact = {"kind": "docs_sync_artifact", "path": path}
                if artifact not in artifacts:
                    artifacts.append(artifact)
        manifest["artifacts"] = artifacts
        write_json(manifest_path, manifest)
    report_path = root / report_value if report_value else None
    if report_path is not None and report_path.is_file():
        append_post_task_sync_report_section(report_path, sync_payload)


def append_post_task_sync_report_section(report_path: Path, sync_payload: dict[str, dict]) -> None:
    text = report_path.read_text(encoding="utf-8", errors="ignore")
    marker = "## Target Synchronization"
    section = [
        "",
        marker,
        "",
    ]
    spec_sync = sync_payload.get("spec_sync", {})
    docs_sync = sync_payload.get("docs_sync", {})
    if isinstance(spec_sync, dict):
        section.append(f"- Spec sync: `{spec_sync.get('status', 'unknown')}` - {spec_sync.get('reason', '')}")
    if isinstance(docs_sync, dict):
        section.append(f"- Docs sync: `{docs_sync.get('status', 'unknown')}` - {docs_sync.get('reason', '')}")
        if docs_sync.get("docs_update_log"):
            section.append(f"- Docs update log: `{docs_sync.get('docs_update_log')}`")
    section.extend(
        [
            "",
            "```json",
            json.dumps(sync_payload, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    if marker in text:
        text = text.split(marker, 1)[0].rstrip()
    report_path.write_text(text.rstrip() + "\n" + "\n".join(section), encoding="utf-8")


def _docs_sync_artifact_paths(payload: dict) -> list[str]:
    paths = []
    if payload.get("docs_update_log"):
        paths.append(str(payload["docs_update_log"]))
    for key in ("updated_paths",):
        values = payload.get(key)
        if isinstance(values, list):
            paths.extend(str(item) for item in values if str(item).strip())
    for key in ("applied_actions", "actions"):
        for item in payload.get(key, []) if isinstance(payload.get(key), list) else []:
            if isinstance(item, dict) and item.get("path"):
                paths.append(str(item["path"]))
    return list(dict.fromkeys(paths))


def cmd_self_iterate(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    result = harness.run_self_iteration(
        reason=args.reason,
        allow_agent=args.allow_agent,
        allow_live=args.allow_live,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Self-iteration status: {result['status']} - {result['message']}")
        print(f"Stages: {result.get('stage_count_before')} -> {result.get('stage_count_after')}")
        print(f"Pending stages after: {result.get('pending_stage_count_after')}")
        if result.get("report"):
            print(f"Report: {result['report']}")
    return 0 if result["status"] in {"planned", "disabled"} else 1


def cmd_pause(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    payload = Harness(root).set_drive_control("pause", reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive control: {payload['status']} - {payload['message']}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    payload = Harness(root).set_drive_control("resume", reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive control: {payload['status']} - {payload['message']}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    payload = Harness(root).set_drive_control("cancel", reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive control: {payload['status']} - {payload['message']}")
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    status_filter = None if args.all else args.status
    payload = Harness(root).approval_queue_summary(status_filter=status_filter)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Approvals: {len(payload['items'])} shown, {payload['pending_count']} pending, "
            f"{payload.get('stale_count', 0)} stale"
        )
        for item in payload["items"]:
            detail = item.get("name") or item.get("phase") or item.get("decision_kind")
            stale = f" ({item.get('stale_reason')})" if item.get("status") == "stale" else ""
            print(
                f"- {item['id']}: {item.get('status')} {item.get('approval_kind')} "
                f"{item.get('task_id')} {detail} - {item.get('reason')}{stale}"
            )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    if args.all:
        payload = harness.approve_all_pending(approved_by=args.approved_by, reason=args.reason)
    else:
        if not args.approval_id:
            raise ValueError("Provide an approval id or use --all")
        payload = harness.approve_approval(args.approval_id, approved_by=args.approved_by, reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Approval status: {payload['status']} - {payload['message']}")
    return 0 if payload["status"] in {"approved", "consumed"} else 1


def run_project_drive(root: Path, args: argparse.Namespace) -> tuple[int, dict]:
    root = root.resolve()
    harness = Harness(root)
    started_at = utc_now()
    drive_start_checkpoint_readiness = harness.checkpoint_readiness()
    start = harness.start_drive()
    if not start["started"]:
        final_status = harness.status_summary()
        payload = {
            "project": final_status["project"],
            "root": str(root),
            "status": start["status"],
            "message": start["message"],
            "started_at": started_at,
            "finished_at": utc_now(),
            "results": [],
            "continuations": [],
            "self_iterations": [],
            "checkpoint_readiness": final_status.get("checkpoint_readiness", {}),
            "drive_control": start.get("drive_control"),
            "stale_running_preflight": start.get("stale_running_preflight"),
            "stale_running_recovery": start.get("stale_running_recovery"),
            "final_status": final_status,
        }
        payload["goal_gap_retrospective"] = harness.drive_goal_gap_retrospective(payload, final_status=final_status)
        payload["drive_report"] = write_drive_report(harness, payload)
        return (0 if start["status"] == "paused" else 1), payload

    deadline = time.monotonic() + args.time_budget_seconds if args.time_budget_seconds else None
    harness.drive_heartbeat(activity="drive-loop", message="drive loop started", clear_task=True)
    results = []
    continuations = []
    self_iterations = []
    continuation_count = 0
    self_iteration_count = 0
    no_progress_count = 0
    task_checkpoint_defer: dict | None = None
    status = "completed"
    message = "No pending task."

    while True:
        harness.drive_heartbeat(activity="drive-loop", message="checking drive controls and budgets", clear_task=True)
        control = harness.drive_control_summary()
        if control.get("cancel_requested") or control.get("status") == "cancelled":
            status = "cancelled"
            message = "Drive cancelled by control state."
            break
        if control.get("pause_requested") or control.get("status") == "paused":
            status = "paused"
            message = "Drive paused by control state."
            break
        if deadline is not None and time.monotonic() >= deadline:
            status = "timeout"
            message = "Time budget expired."
            break
        if len(results) >= args.max_tasks:
            status = "budget_exhausted"
            message = f"Task budget exhausted after {args.max_tasks} task(s)."
            break
        harness.drive_heartbeat(activity="task-selection", message="selecting next roadmap task", clear_task=True)
        task = harness.next_task()
        if task is None:
            isolated_failures = harness.latest_isolated_failures_summary()
            if int(isolated_failures.get("unresolved_count", 0) or 0) > 0:
                status = "isolated_failure"
                message = "Unresolved isolated task failure exists; resolve it before adding continuation work."
                break
            if not args.rolling and not args.self_iterate:
                status = "completed"
                message = "Roadmap queue is empty."
                break
            continuation = None
            if args.rolling:
                if continuation_count >= args.max_continuations:
                    if not args.self_iterate:
                        status = "budget_exhausted"
                        message = f"Continuation budget exhausted after {args.max_continuations} continuation(s)."
                        break
                else:
                    harness.drive_heartbeat(
                        activity="continuation-materialization",
                        message="materializing continuation because roadmap queue is empty",
                        clear_task=True,
                    )
                    checkpoint_intent = None
                    checkpoint_readiness = harness.checkpoint_readiness()
                    continuation_summary = harness.continuation_summary()
                    has_pending_continuation = (
                        int(continuation_summary.get("pending_stage_count", 0) or 0) > 0
                    )
                    if checkpoint_requested(args):
                        checkpoint_intent = harness.roadmap_materialization_checkpoint_intent(
                            reason="rolling_drive_queue_empty",
                            push=bool(getattr(args, "push_after_task", False)),
                            remote=str(getattr(args, "git_remote", "origin")),
                            branch=getattr(args, "git_branch", None),
                        )
                        if has_pending_continuation and checkpoint_readiness.get("blocking"):
                            block_message = (
                                "roadmap materialization blocked because checkpoint readiness found "
                                "unrelated dirty paths before the boundary"
                            )
                            materialization_checkpoint = harness.defer_roadmap_materialization_checkpoint(
                                checkpoint_intent,
                                reason="checkpoint_readiness_blocked",
                                message=block_message,
                            )
                            continuation = {
                                "status": "blocked",
                                "message": block_message,
                                "milestones_added": [],
                                "tasks_added": 0,
                                "checkpoint_readiness_before_materialization": checkpoint_readiness,
                                "materialization_checkpoint_intent": checkpoint_intent,
                                "materialization_checkpoint": materialization_checkpoint,
                            }
                            continuations.append(continuation)
                            status = "blocked"
                            message = block_message
                            break
                    continuation = harness.advance_roadmap(
                        max_new_milestones=args.continuation_batch_size,
                        reason="rolling_drive_queue_empty",
                    )
                    continuation["checkpoint_readiness_before_materialization"] = checkpoint_readiness
                    if checkpoint_intent is not None:
                        continuation["materialization_checkpoint_intent"] = checkpoint_intent
                        materialization_checkpoint = harness.git_checkpoint_roadmap_materialization(
                            checkpoint_intent,
                            continuation,
                            push=bool(getattr(args, "push_after_task", False)),
                            remote=str(getattr(args, "git_remote", "origin")),
                            branch=getattr(args, "git_branch", None),
                        )
                        continuation["materialization_checkpoint"] = materialization_checkpoint
                        if materialization_checkpoint_should_defer_tasks(materialization_checkpoint):
                            task_checkpoint_defer = materialization_task_checkpoint_defer_payload(
                                materialization_checkpoint
                            )
                    continuations.append(continuation)
                    if continuation["status"] == "advanced" and continuation.get("tasks_added", 0) > 0:
                        continuation_count += 1
                        no_progress_count = 0
                        harness = Harness(root)
                        continue
            if args.self_iterate:
                if self_iteration_count >= args.max_self_iterations:
                    status = "budget_exhausted"
                    message = f"Self-iteration budget exhausted after {args.max_self_iterations} iteration(s)."
                    break
                harness.drive_heartbeat(
                    activity="self-iteration",
                    message="running self-iteration because roadmap queue is empty",
                    clear_task=True,
                )
                iteration = harness.run_self_iteration(
                    reason="drive_queue_empty",
                    allow_agent=args.allow_agent,
                    allow_live=args.allow_live,
                    checkpoint_readiness=drive_start_checkpoint_readiness if not results else None,
                )
                self_iterations.append(iteration)
                if iteration["status"] == "planned" and int(iteration.get("pending_stage_count_after", 0)) > 0:
                    self_iteration_count += 1
                    no_progress_count = 0
                    harness = Harness(root)
                    continue
                no_progress_count += 1
                if no_progress_count >= args.no_progress_limit:
                    status = "stalled"
                    message = f"No-progress limit reached after {no_progress_count} self-iteration attempt(s): {iteration['message']}"
                    break
                status = "stalled" if iteration["status"] not in {"disabled"} else "completed"
                message = iteration["message"]
                break
            if continuation is None:
                status = "completed"
                message = "Roadmap queue is empty."
                break
            no_progress_count += 1
            if continuation["status"] in {"disabled", "exhausted"}:
                status = "completed"
                message = continuation["message"]
                break
            if no_progress_count >= args.no_progress_limit:
                status = "stalled"
                message = f"No-progress limit reached after {no_progress_count} continuation attempt(s)."
                break
            status = "stalled"
            message = continuation["message"]
            break
        harness.drive_heartbeat(
            activity="task-execution",
            message=f"running task {task.id}",
            task=task,
        )
        result = harness.run_task(
            task,
            allow_live=args.allow_live,
            allow_manual=args.allow_manual,
            allow_agent=args.allow_agent,
        )
        result["spec_sync"] = maybe_sync_completed_spec_task(root, task, result, phase="drive")
        result["docs_sync"] = maybe_sync_completed_documentation(root, task, result, phase="drive")
        attach_post_task_sync_evidence(root, result)
        harness.rebuild_manifest_index()
        maybe_checkpoint_task(harness, task, result, args, checkpoint_defer=task_checkpoint_defer)
        results.append(result)
        harness.drive_heartbeat(
            activity="task-execution",
            message=f"task {task.id} finished with status {result['status']}",
            task=task,
        )
        if result["status"] not in COMPLETED_STATUSES:
            status = result["status"]
            message = f"Stopped at task {task.id}: {result['message']}"
            break
        if args.stop_after_each:
            status = "paused"
            message = f"Stopped after task {task.id} because --stop-after-each was set."
            break

    harness.drive_heartbeat(activity="drive-finishing", message=message, clear_task=True)
    harness.finish_drive(status=status, message=message)
    final_status = harness.status_summary()
    payload = {
        "project": final_status["project"],
        "root": str(root),
        "status": status,
        "message": message,
        "started_at": started_at,
        "finished_at": utc_now(),
        "results": results,
        "continuations": continuations,
        "self_iterations": self_iterations,
        "checkpoint_requested": checkpoint_requested(args),
        "checkpoint_readiness": final_status.get("checkpoint_readiness", {}),
        "drive_control": final_status.get("drive_control"),
        "stale_running_preflight": start.get("stale_running_preflight"),
        "stale_running_recovery": start.get("stale_running_recovery")
        or (
            final_status.get("drive_control", {}).get("stale_running_recovery")
            if isinstance(final_status.get("drive_control"), dict)
            else None
        ),
        "final_status": final_status,
    }
    payload["goal_gap_retrospective"] = harness.drive_goal_gap_retrospective(payload, final_status=final_status)
    payload["drive_report"] = write_drive_report(harness, payload)
    return (0 if status in {"completed", "paused", "budget_exhausted"} else 1), payload


def print_drive_payload(payload: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Drive status: {payload['status']} - {payload['message']}")
    print(f"Tasks run: {len(payload.get('results', []))}")
    print(f"Continuations: {len(payload.get('continuations', []))}")
    print(f"Self-iterations: {len(payload.get('self_iterations', []))}")
    checkpoint_readiness = payload.get("checkpoint_readiness") if isinstance(payload.get("checkpoint_readiness"), dict) else {}
    if checkpoint_readiness:
        print(
            "Checkpoint readiness: "
            f"{checkpoint_readiness.get('reason', 'unknown')} "
            f"blocking={str(bool(checkpoint_readiness.get('blocking'))).lower()}"
        )
    print(f"Drive report: {payload['drive_report']}")
    final_status = payload.get("final_status") if isinstance(payload.get("final_status"), dict) else {}
    next_task = final_status.get("next_task") if isinstance(final_status, dict) else None
    print(f"Next task: {next_task['id'] if next_task else 'none'}")


def cmd_drive(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    exit_code, payload = run_project_drive(root, args)
    print_drive_payload(payload, json_output=args.json)
    return exit_code


def _workspace_drive_args(args: argparse.Namespace, project_root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=project_root,
        workspace=args.workspace,
        project=None,
        max_depth=args.max_depth,
        json=False,
        max_tasks=args.max_tasks,
        time_budget_seconds=args.time_budget_seconds,
        rolling=args.rolling,
        self_iterate=args.self_iterate,
        max_continuations=args.max_continuations,
        max_self_iterations=args.max_self_iterations,
        continuation_batch_size=args.continuation_batch_size,
        no_progress_limit=args.no_progress_limit,
        allow_live=args.allow_live,
        allow_manual=args.allow_manual,
        allow_agent=args.allow_agent,
        stop_after_each=False,
        commit_after_task=False,
        push_after_task=False,
        git_remote="origin",
        git_branch=None,
        git_message_template="chore(engineering): complete {task_id}",
    )


def _path_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except OSError:
        return False


def _dispatch_skip(code: str, message: str, **evidence) -> dict:
    payload = {"code": code, "message": message}
    payload.update({key: value for key, value in evidence.items() if value is not None})
    return payload


def _add_dispatch_skip(item: dict, code: str, message: str, **evidence) -> None:
    item.setdefault("skip_reasons", []).append(_dispatch_skip(code, message, **evidence))


def _workspace_task_counts(summary: dict) -> dict:
    counts = {"total": 0, "done": 0, "blocked": 0, "failed": 0, "pending": 0}
    for milestone in summary.get("milestones", []):
        if not isinstance(milestone, dict):
            continue
        for key in counts:
            counts[key] += int(milestone.get(key, 0) or 0)
    return counts


def _workspace_scheduler_policy(args: argparse.Namespace) -> str:
    value = getattr(args, "scheduler_policy", WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY)
    return value if value in WORKSPACE_DISPATCH_SCHEDULER_POLICIES else WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY


def _coerce_bounded_nonnegative_int(value, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return default
    return min(number, maximum)


def _workspace_dispatch_nonproductive_backoff_seconds(args: argparse.Namespace) -> int:
    cli_value = getattr(args, "nonproductive_backoff_seconds", None)
    if cli_value is not None:
        return _coerce_bounded_nonnegative_int(
            cli_value,
            DEFAULT_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS,
            MAX_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS,
        )
    return _coerce_bounded_nonnegative_int(
        os.environ.get(WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS_ENV),
        DEFAULT_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS,
        MAX_WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_SECONDS,
    )


def _compact_workspace_status_summary(summary: dict) -> dict:
    drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
    watchdog = drive_control.get("watchdog") if isinstance(drive_control.get("watchdog"), dict) else {}
    stale_running_recovery = (
        deepcopy(drive_control.get("stale_running_recovery"))
        if isinstance(drive_control.get("stale_running_recovery"), dict)
        else None
    )
    stale_running_preflight = (
        deepcopy(drive_control.get("stale_running_preflight"))
        if isinstance(drive_control.get("stale_running_preflight"), dict)
        else None
    )
    stale_running_block = (
        deepcopy(drive_control.get("stale_running_block"))
        if isinstance(drive_control.get("stale_running_block"), dict)
        else None
    )
    approval_queue = summary.get("approval_queue") if isinstance(summary.get("approval_queue"), dict) else {}
    failure_isolation = summary.get("failure_isolation") if isinstance(summary.get("failure_isolation"), dict) else {}
    next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
    continuation = summary.get("continuation") if isinstance(summary.get("continuation"), dict) else {}
    self_iteration = summary.get("self_iteration") if isinstance(summary.get("self_iteration"), dict) else {}
    checkpoint_readiness = (
        summary.get("checkpoint_readiness") if isinstance(summary.get("checkpoint_readiness"), dict) else {}
    )
    return {
        "project": summary.get("project"),
        "profile": summary.get("profile"),
        "root": summary.get("root"),
        "roadmap": summary.get("roadmap"),
        "task_counts": _workspace_task_counts(summary),
        "checkpoint_readiness": checkpoint_readiness,
        "next_task": (
            {
                "id": next_task.get("id"),
                "title": next_task.get("title"),
                "milestone_id": next_task.get("milestone_id"),
            }
            if next_task
            else None
        ),
        "continuation": {
            "enabled": bool(continuation.get("enabled", False)),
            "stage_count": int(continuation.get("stage_count", 0) or 0),
            "pending_stage_count": int(continuation.get("pending_stage_count", 0) or 0),
            "next_stage": continuation.get("next_stage"),
        },
        "self_iteration": {
            "enabled": bool(self_iteration.get("enabled", False)),
            "completed_count": int(self_iteration.get("completed_count", 0) or 0),
            "max_iterations": self_iteration.get("max_iterations"),
            "latest_assessment": deepcopy(self_iteration.get("latest_assessment"))
            if isinstance(self_iteration.get("latest_assessment"), dict)
            else None,
        },
        "drive_control": {
            "status": drive_control.get("status", "idle"),
            "active": bool(drive_control.get("active", False)),
            "pause_requested": bool(drive_control.get("pause_requested", False)),
            "cancel_requested": bool(drive_control.get("cancel_requested", False)),
            "stale": bool(drive_control.get("stale", False)),
            "stale_reason": drive_control.get("stale_reason") or watchdog.get("reason"),
            "pid": drive_control.get("pid"),
            "started_at": drive_control.get("started_at"),
            "last_heartbeat_at": drive_control.get("last_heartbeat_at"),
            "heartbeat_count": int(drive_control.get("heartbeat_count", 0) or 0),
            "stale_after_seconds": drive_control.get("stale_after_seconds"),
            "current_activity": drive_control.get("current_activity"),
            "watchdog_status": watchdog.get("status"),
            "watchdog_message": watchdog.get("message"),
            "watchdog": {
                "schema_version": watchdog.get("schema_version"),
                "status": watchdog.get("status"),
                "stale": bool(watchdog.get("stale", False)),
                "reason": watchdog.get("reason"),
                "pid_alive": watchdog.get("pid_alive"),
                "heartbeat_at": watchdog.get("heartbeat_at"),
                "heartbeat_age_seconds": watchdog.get("heartbeat_age_seconds"),
                "threshold_seconds": watchdog.get("threshold_seconds"),
            },
            "stale_running_recovery": stale_running_recovery,
            "stale_running_preflight": stale_running_preflight,
            "stale_running_block": stale_running_block,
        },
        "approval_queue": {
            "pending_count": int(approval_queue.get("pending_count", 0) or 0),
            "stale_count": int(approval_queue.get("stale_count", 0) or 0),
        },
        "failure_isolation": {
            "unresolved_count": int(failure_isolation.get("unresolved_count", 0) or 0),
            "has_unresolved": bool(failure_isolation.get("has_unresolved", False)),
            "latest_isolated_failures": failure_isolation.get("latest_isolated_failures", []),
        },
    }


def _workspace_dispatch_resource_budget(args: argparse.Namespace, summary: dict | None = None) -> dict:
    summary = summary if isinstance(summary, dict) else {}
    task_counts = summary.get("task_counts") if isinstance(summary.get("task_counts"), dict) else {}
    continuation = summary.get("continuation") if isinstance(summary.get("continuation"), dict) else {}
    self_iteration = summary.get("self_iteration") if isinstance(summary.get("self_iteration"), dict) else {}
    next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
    return {
        "schema_version": 1,
        "kind": "engineering-harness.workspace-dispatch-resource-budget",
        "per_invocation": {
            "max_tasks": args.max_tasks,
            "time_budget_seconds": args.time_budget_seconds,
            "rolling": bool(args.rolling),
            "self_iterate": bool(args.self_iterate),
            "max_continuations": args.max_continuations,
            "max_self_iterations": args.max_self_iterations,
            "continuation_batch_size": args.continuation_batch_size,
            "no_progress_limit": args.no_progress_limit,
        },
        "project_demand": {
            "pending_tasks": int(task_counts.get("pending", 0) or 0),
            "pending_continuations": int(continuation.get("pending_stage_count", 0) or 0),
            "self_iteration_enabled": bool(self_iteration.get("enabled", False)),
            "next_task_id": next_task.get("id") if next_task else None,
        },
        "capability_budget": {
            "allow_live": bool(args.allow_live),
            "allow_manual": bool(args.allow_manual),
            "allow_agent": bool(args.allow_agent),
            "commit_after_task": False,
            "push_after_task": False,
        },
    }


def _workspace_project_lease_summary(summary: dict) -> dict:
    drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
    watchdog = drive_control.get("watchdog") if isinstance(drive_control.get("watchdog"), dict) else {}
    active = bool(drive_control.get("active", False))
    stale = bool(drive_control.get("stale", False))
    status = str(drive_control.get("status") or "idle")
    protected = (active or status == "running") and not stale
    protection_reason = None
    if protected:
        protection_reason = "active_project_drive_lease"
    elif stale:
        protection_reason = str(drive_control.get("stale_reason") or watchdog.get("reason") or "stale")
    return {
        "schema_version": 1,
        "kind": "engineering-harness.project-drive-lease",
        "status": status,
        "active": active,
        "protected": protected,
        "protection_reason": protection_reason,
        "owner_pid": drive_control.get("pid"),
        "started_at": drive_control.get("started_at"),
        "last_heartbeat_at": drive_control.get("last_heartbeat_at"),
        "heartbeat_count": int(drive_control.get("heartbeat_count", 0) or 0),
        "stale": stale,
        "stale_reason": drive_control.get("stale_reason") or watchdog.get("reason"),
        "stale_after_seconds": drive_control.get("stale_after_seconds") or watchdog.get("threshold_seconds"),
        "current_activity": drive_control.get("current_activity"),
        "watchdog": watchdog,
    }


def _workspace_task_retry_summary(harness: Harness, summary: dict) -> dict:
    next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
    if not next_task:
        return {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-dispatch-task-retry",
            "next_task_id": None,
            "status": "no_pending_task",
            "attempts": 0,
            "max_attempts": 0,
            "attempts_remaining": 0,
            "exhausted": False,
        }
    task_id = str(next_task.get("id") or "")
    try:
        task = harness.task_by_id(task_id)
    except KeyError:
        return {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-dispatch-task-retry",
            "next_task_id": task_id,
            "status": "unknown",
            "attempts": 0,
            "max_attempts": 0,
            "attempts_remaining": 0,
            "exhausted": False,
        }
    state = harness.load_state()
    task_state = state.get("tasks", {}).get(task.id, {})
    if not isinstance(task_state, dict):
        task_state = {}
    attempts = _workspace_int(task_state.get("attempts")) or 0
    max_attempts = int(task.max_attempts)
    attempts_remaining = max(0, max_attempts - attempts)
    status = str(task_state.get("status", task.status))
    return {
        "schema_version": 1,
        "kind": "engineering-harness.workspace-dispatch-task-retry",
        "next_task_id": task.id,
        "status": status,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "attempts_remaining": attempts_remaining,
        "exhausted": attempts >= max_attempts,
    }


def _workspace_dispatch_queue_item(workspace: Path, project, args: argparse.Namespace, index: int) -> dict:
    item = {
        "index": index,
        "scheduler_rank": index,
        "project": project.name,
        "root": str(project.root),
        "roadmap": str(project.roadmap_path) if project.roadmap_path else None,
        "profile": project.profile,
        "kind": project.kind,
        "configured": project.configured,
        "eligible": False,
        "selected": False,
        "dispatch_status": "skipped",
        "skip_reasons": [],
        "scheduler_policy": _workspace_scheduler_policy(args),
        "score": None,
        "score_components": {},
        "priority": {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-dispatch-priority",
            "policy": _workspace_scheduler_policy(args),
            "score": None,
            "scheduler_rank": index,
            "starvation_prevention": {},
        },
        "resource_budget": _workspace_dispatch_resource_budget(args),
        "project_lease": {
            "schema_version": 1,
            "kind": "engineering-harness.project-drive-lease",
            "status": "not_evaluated",
            "active": False,
            "protected": False,
        },
        "retry_backoff_summary": {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-dispatch-retry-backoff-summary",
            "task_retry": None,
            "nonproductive_backoff": None,
            "backoff_active": False,
        },
        "selected_reason": None,
        "backoff": {
            "schema_version": 1,
            "kind": "workspace_dispatch_nonproductive_backoff",
            "decision": "not_evaluated",
            "active": False,
            "points": 0,
        },
    }
    if not _path_within(project.root, workspace):
        _add_dispatch_skip(
            item,
            "outside_local_scope",
            "project root resolves outside the requested workspace",
            workspace=str(workspace),
        )
        return item
    if project.roadmap_path is not None and not _path_within(project.roadmap_path, project.root):
        _add_dispatch_skip(
            item,
            "outside_local_scope",
            "roadmap path resolves outside the project root",
            workspace=str(workspace),
        )
        return item
    if not project.configured or project.roadmap_path is None:
        _add_dispatch_skip(item, "missing_roadmap", "project has no engineering roadmap")
        return item

    try:
        harness = Harness(project.root, project.roadmap_path)
        validation = harness.validate_roadmap()
    except Exception as exc:
        _add_dispatch_skip(item, "invalid_roadmap", f"roadmap could not be loaded: {exc}")
        return item
    item["roadmap_validation"] = validation
    if validation.get("status") != "passed":
        _add_dispatch_skip(
            item,
            "invalid_roadmap",
            "roadmap validation failed",
            errors=validation.get("errors", []),
        )
        return item

    stale_running_preflight = harness.recover_stale_running_preflight(reason="workspace_drive_selection_preflight")
    if isinstance(stale_running_preflight, dict) and stale_running_preflight.get("status") in {
        "recovered",
        "blocked",
        "recoverable",
    }:
        item["stale_running_preflight"] = stale_running_preflight
    if isinstance(stale_running_preflight, dict) and stale_running_preflight.get("status") == "recovered":
        item["stale_running_recovery"] = stale_running_preflight
    elif isinstance(stale_running_preflight, dict) and stale_running_preflight.get("status") == "blocked":
        item["stale_running_block"] = stale_running_preflight

    summary = harness.status_summary(refresh_approvals=False)
    compact_summary = _compact_workspace_status_summary(summary)
    item["summary"] = compact_summary
    item["resource_budget"] = _workspace_dispatch_resource_budget(args, compact_summary)
    item["project_lease"] = _workspace_project_lease_summary(compact_summary)
    item["retry_backoff_summary"]["task_retry"] = _workspace_task_retry_summary(harness, compact_summary)
    checkpoint_readiness = compact_summary.get("checkpoint_readiness", {})
    item["checkpoint_readiness"] = checkpoint_readiness
    if isinstance(checkpoint_readiness, dict) and checkpoint_readiness.get("blocking"):
        _add_dispatch_skip(
            item,
            "checkpoint_not_ready",
            "project has unrelated dirty git paths that must be resolved before unattended dispatch",
            reason=checkpoint_readiness.get("reason"),
            blocking_paths=checkpoint_readiness.get("blocking_paths", []),
            dirty_paths=checkpoint_readiness.get("dirty_paths", []),
            recommended_action=checkpoint_readiness.get("recommended_action"),
        )
    drive_control = compact_summary["drive_control"]
    stale_running_block = (
        drive_control.get("stale_running_block")
        if isinstance(drive_control.get("stale_running_block"), dict)
        else item.get("stale_running_block")
        if isinstance(item.get("stale_running_block"), dict)
        else None
    )
    if drive_control.get("pause_requested") or drive_control.get("status") == "paused":
        _add_dispatch_skip(item, "paused", "drive is paused for this project")
    if drive_control.get("cancel_requested") or drive_control.get("status") == "cancelled":
        _add_dispatch_skip(item, "cancelled", "drive is cancelled for this project")
    if drive_control.get("stale") or drive_control.get("status") == "stale":
        _add_dispatch_skip(
            item,
            "stale_running",
            "drive control is stale and must be resumed before dispatch",
            stale_reason=drive_control.get("stale_reason"),
            stale_running_block=stale_running_block,
        )
    elif drive_control.get("active") or drive_control.get("status") == "running":
        _add_dispatch_skip(
            item,
            "already_running",
            "a drive is already running for this project",
            stale_running_block=stale_running_block,
        )

    pending_approvals = int(compact_summary["approval_queue"].get("pending_count", 0) or 0)
    if pending_approvals > 0:
        _add_dispatch_skip(
            item,
            "waiting_on_approvals",
            "project has pending approval gates",
            pending_count=pending_approvals,
        )

    unresolved_failures = int(compact_summary["failure_isolation"].get("unresolved_count", 0) or 0)
    if unresolved_failures > 0:
        _add_dispatch_skip(
            item,
            "unresolved_isolated_failures",
            "project has unresolved isolated task failures",
            unresolved_count=unresolved_failures,
        )

    if compact_summary.get("next_task") is None and not args.rolling and not args.self_iterate:
        _add_dispatch_skip(item, "no_pending_task", "project has no pending roadmap task")

    item["eligible"] = not item["skip_reasons"]
    item["dispatch_status"] = "eligible" if item["eligible"] else "skipped"
    return item


def _read_json_mapping(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_score_path_key(item: dict) -> str:
    root = item.get("root")
    try:
        return str(Path(str(root)).resolve())
    except OSError:
        return str(root or "")


def _workspace_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _workspace_format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _workspace_timestamp_plus_seconds(value, seconds: int) -> str | None:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        return None
    return _workspace_format_utc(parsed + timedelta(seconds=seconds))


def _workspace_selected_drive_payload(selected: dict, dispatch_payload: dict) -> tuple[dict | None, dict]:
    drive = dispatch_payload.get("drive") if isinstance(dispatch_payload.get("drive"), dict) else None
    if drive is not None:
        return drive, {"kind": "workspace_dispatch_sidecar"}

    drive_report_json = selected.get("drive_report_json")
    root = selected.get("root")
    if not drive_report_json or not root:
        return None, {"kind": "missing_drive_report"}
    candidate = Path(str(drive_report_json))
    path = candidate if candidate.is_absolute() else Path(str(root)) / candidate
    drive = _read_json_mapping(path)
    if drive is None:
        return None, {"kind": "unreadable_project_drive_report", "path": str(drive_report_json)}
    return drive, {"kind": "project_drive_report", "path": str(drive_report_json)}


def _workspace_drive_progress_evidence(drive: dict | None) -> dict:
    drive = drive if isinstance(drive, dict) else {}
    results = drive.get("results") if isinstance(drive.get("results"), list) else []
    continuations = drive.get("continuations") if isinstance(drive.get("continuations"), list) else []
    self_iterations = drive.get("self_iterations") if isinstance(drive.get("self_iterations"), list) else []
    completed_result_count = sum(
        1
        for result in results
        if isinstance(result, dict) and str(result.get("status") or "") in COMPLETED_STATUSES
    )
    materialized_continuation_count = sum(
        1
        for continuation in continuations
        if (
            isinstance(continuation, dict)
            and continuation.get("status") == "advanced"
            and (_workspace_int(continuation.get("tasks_added")) or 0) > 0
        )
    )
    planned_self_iteration_count = sum(
        1
        for iteration in self_iterations
        if (
            isinstance(iteration, dict)
            and iteration.get("status") == "planned"
            and (_workspace_int(iteration.get("pending_stage_count_after")) or 0)
            > (_workspace_int(iteration.get("stage_count_before")) or 0)
        )
    )
    return {
        "result_count": len(results),
        "completed_result_count": completed_result_count,
        "continuation_count": len(continuations),
        "materialized_continuation_count": materialized_continuation_count,
        "self_iteration_count": len(self_iterations),
        "planned_self_iteration_count": planned_self_iteration_count,
        "useful_progress": (
            completed_result_count > 0
            or materialized_continuation_count > 0
            or planned_self_iteration_count > 0
        ),
    }


def _workspace_planner_validation_failed(drive: dict | None) -> bool:
    if not isinstance(drive, dict):
        return False
    self_iterations = drive.get("self_iterations") if isinstance(drive.get("self_iterations"), list) else []
    for iteration in self_iterations:
        if not isinstance(iteration, dict):
            continue
        validation = iteration.get("validation") if isinstance(iteration.get("validation"), dict) else {}
        if iteration.get("status") == "rejected" and validation.get("status") == "failed":
            return True
    return False


def _workspace_dispatch_outcome(
    workspace: Path,
    dispatch_payload: dict,
    selected: dict,
    report_path: Path,
) -> dict:
    drive, drive_source = _workspace_selected_drive_payload(selected, dispatch_payload)
    progress = _workspace_drive_progress_evidence(drive)
    drive_status = selected.get("drive_status") or (drive or {}).get("status")
    drive_exit_code = _workspace_int(selected.get("drive_exit_code"))
    dispatch_status = dispatch_payload.get("status")
    reason = "useful_progress" if progress["useful_progress"] else "no_nonproductive_signal"
    classification = "productive" if progress["useful_progress"] else "neutral"
    message = "selected drive produced useful local progress" if progress["useful_progress"] else "no backoff signal"

    if not progress["useful_progress"]:
        drive_status_text = str(drive_status or "")
        if _workspace_planner_validation_failed(drive):
            classification = "nonproductive"
            reason = "planner_validation_failed"
            message = "self-iteration planner output failed validation without materializing useful work"
        elif drive_status_text == "budget_exhausted":
            classification = "nonproductive"
            reason = "budget_without_progress"
            message = "drive exhausted budget without completing a task or materializing work"
        elif drive_status_text in {"cancelled", "paused", "stale", "timeout"}:
            classification = "nonproductive"
            reason = "interrupted"
            message = f"drive stopped as {drive_status_text} without useful progress"
        elif (
            dispatch_status == "drive_failed"
            or (drive_exit_code is not None and drive_exit_code != 0)
            or drive_status_text in {"blocked", "failed", "isolated_failure"}
        ):
            classification = "nonproductive"
            reason = "drive_failed"
            message = "selected project drive failed without useful progress"

    drive_report = selected.get("drive_report") or (drive or {}).get("drive_report")
    drive_report_json = selected.get("drive_report_json") or (drive or {}).get("drive_report_json")
    return {
        "schema_version": 1,
        "classification": classification,
        "productive": classification == "productive",
        "nonproductive": classification == "nonproductive",
        "reason": reason,
        "message": message,
        "dispatch_status": dispatch_status,
        "drive_status": drive_status,
        "drive_exit_code": drive_exit_code,
        "drive_report": drive_report,
        "drive_report_json": drive_report_json,
        "drive_evidence_source": drive_source,
        "progress": progress,
        "source_report": dispatch_payload.get("dispatch_report_json") or _workspace_relative(workspace, report_path),
        "source_dispatch_report": dispatch_payload.get("dispatch_report"),
    }


def _workspace_dispatch_history(workspace: Path) -> list[dict]:
    report_dir = workspace / ".engineering" / "reports" / "workspace-dispatches"
    if not report_dir.exists():
        return []
    paths = sorted(
        [path for path in report_dir.glob("*.json") if path.is_file()],
        key=lambda path: _workspace_relative(workspace, path),
        reverse=True,
    )[:WORKSPACE_DISPATCH_HISTORY_LIMIT]
    history: list[dict] = []
    for path in paths:
        payload = _read_json_mapping(path)
        if not isinstance(payload, dict):
            continue
        selected = payload.get("selected") if isinstance(payload.get("selected"), dict) else None
        if not selected:
            continue
        outcome = _workspace_dispatch_outcome(workspace, payload, selected, path)
        history.append(
            {
                "path": _workspace_relative(workspace, path),
                "dispatch_report": payload.get("dispatch_report"),
                "status": payload.get("status"),
                "finished_at": payload.get("finished_at") or payload.get("started_at"),
                "selected_project": selected.get("project"),
                "selected_root": selected.get("root"),
                "selected_score": selected.get("score"),
                "drive_status": selected.get("drive_status"),
                "drive_exit_code": selected.get("drive_exit_code"),
                "drive_report": selected.get("drive_report"),
                "drive_report_json": selected.get("drive_report_json"),
                "outcome": outcome,
            }
        )
    return history


def _workspace_same_root(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _workspace_history_succeeded(record: dict) -> bool:
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    if outcome.get("productive"):
        return True
    if outcome.get("nonproductive"):
        return False
    if record.get("status") == "dispatched" and record.get("drive_exit_code") in (None, 0):
        return True
    return str(record.get("drive_status") or "") in {"completed", "budget_exhausted", "paused"}


def _workspace_history_failed(record: dict) -> bool:
    if record.get("status") == "drive_failed":
        return True
    exit_code = record.get("drive_exit_code")
    try:
        return exit_code is not None and int(exit_code) != 0
    except (TypeError, ValueError):
        return False


def _workspace_latest_drive_retrospective(project_root: Path) -> dict | None:
    report_dir = project_root / ".engineering" / "reports" / "tasks" / "drives"
    if not report_dir.exists():
        return None
    paths = sorted(
        [path for path in report_dir.glob("*.json") if path.is_file()],
        key=lambda path: str(path),
        reverse=True,
    )
    for path in paths[:WORKSPACE_DISPATCH_HISTORY_LIMIT]:
        payload = _read_json_mapping(path)
        retrospective = payload.get("goal_gap_retrospective") if isinstance(payload, dict) else None
        if not isinstance(retrospective, dict):
            continue
        generated_at = retrospective.get("generated_at")
        return {
            "path": str(path.relative_to(project_root)),
            "generated_at": generated_at,
            "age_seconds": _workspace_dispatch_timestamp_age_seconds(generated_at),
            "retrospective": retrospective,
        }
    return None


def _workspace_goal_gap_score(project_root: Path) -> dict:
    severity_weights = {"critical": 120, "high": 90, "medium": 50, "low": 20, "info": 5}
    latest = _workspace_latest_drive_retrospective(project_root)
    if latest is None:
        return {
            "source": None,
            "generated_at": None,
            "age_seconds": None,
            "max_severity": None,
            "severity_points": 0,
            "age_points": 0,
            "points": 0,
        }
    retrospective = latest["retrospective"]
    severities = [
        str(item.get("severity") or "info")
        for item in retrospective.get("remaining_risks", [])
        if isinstance(item, dict)
    ]
    max_severity = None
    severity_points = 0
    for severity in severities:
        points = severity_weights.get(severity, 0)
        if points > severity_points:
            severity_points = points
            max_severity = severity
    age_seconds = latest.get("age_seconds")
    age_points = min(72, int(age_seconds // 3600)) if isinstance(age_seconds, int) else 0
    return {
        "source": latest.get("path"),
        "generated_at": latest.get("generated_at"),
        "age_seconds": age_seconds,
        "max_severity": max_severity,
        "severity_points": severity_points,
        "age_points": age_points,
        "points": severity_points + age_points,
    }


def _workspace_history_score(item: dict, history: list[dict]) -> dict:
    root = str(item.get("root") or "")
    project_history = [
        record for record in history if _workspace_same_root(str(record.get("selected_root") or ""), root)
    ]
    recent_history = history[:WORKSPACE_DISPATCH_RECENT_HISTORY_LIMIT]
    recent_project_history = [
        record for record in recent_history if _workspace_same_root(str(record.get("selected_root") or ""), root)
    ]
    last_selected = project_history[0] if project_history else None
    last_selected_at = last_selected.get("finished_at") if last_selected else None
    last_selected_age_seconds = _workspace_dispatch_timestamp_age_seconds(last_selected_at) if last_selected_at else None
    recent_success_count = sum(1 for record in recent_project_history if _workspace_history_succeeded(record))
    recent_failure_count = sum(1 for record in recent_project_history if _workspace_history_failed(record))
    never_selected_bonus = 80 if not project_history else 0
    recent_success_penalty = -25 * recent_success_count
    recent_failure_penalty = -75 * recent_failure_count
    age_points = min(48, int(last_selected_age_seconds // 3600)) if isinstance(last_selected_age_seconds, int) else 0
    points = never_selected_bonus + recent_success_penalty + recent_failure_penalty + age_points
    return {
        "has_workspace_history": bool(history),
        "selected_count": len(project_history),
        "recent_window_size": WORKSPACE_DISPATCH_RECENT_HISTORY_LIMIT,
        "recent_success_count": recent_success_count,
        "recent_failure_count": recent_failure_count,
        "last_selected_at": last_selected_at,
        "last_selected_age_seconds": last_selected_age_seconds,
        "last_selected_status": last_selected.get("status") if last_selected else None,
        "last_drive_status": last_selected.get("drive_status") if last_selected else None,
        "never_selected_bonus": never_selected_bonus,
        "recent_success_penalty": recent_success_penalty,
        "recent_failure_penalty": recent_failure_penalty,
        "age_points": age_points,
        "points": points,
    }


def _workspace_nonproductive_backoff_decision(item: dict, history: list[dict], args: argparse.Namespace) -> dict:
    threshold_seconds = _workspace_dispatch_nonproductive_backoff_seconds(args)
    decision = {
        "schema_version": 1,
        "kind": "workspace_dispatch_nonproductive_backoff",
        "decision": "disabled" if threshold_seconds <= 0 else "no_history",
        "active": False,
        "reason": None,
        "message": None,
        "source_report": None,
        "source_dispatch_report": None,
        "source_drive_report": None,
        "source_drive_report_json": None,
        "age_seconds": None,
        "threshold_seconds": threshold_seconds,
        "expires_at": None,
        "points": 0,
        "outcome": None,
    }
    if threshold_seconds <= 0:
        return decision

    root = str(item.get("root") or "")
    project_history = [
        record for record in history if _workspace_same_root(str(record.get("selected_root") or ""), root)
    ]
    if not project_history:
        return decision

    latest = project_history[0]
    outcome = latest.get("outcome") if isinstance(latest.get("outcome"), dict) else {}
    decision.update(
        {
            "reason": outcome.get("reason"),
            "message": outcome.get("message"),
            "source_report": outcome.get("source_report") or latest.get("path"),
            "source_dispatch_report": outcome.get("source_dispatch_report") or latest.get("dispatch_report"),
            "source_drive_report": outcome.get("drive_report") or latest.get("drive_report"),
            "source_drive_report_json": outcome.get("drive_report_json") or latest.get("drive_report_json"),
            "outcome": outcome,
        }
    )
    if not outcome.get("nonproductive"):
        decision["decision"] = "productive" if outcome.get("productive") else "not_nonproductive"
        return decision

    finished_at = latest.get("finished_at")
    age_seconds = _workspace_dispatch_timestamp_age_seconds(finished_at)
    expires_at = _workspace_timestamp_plus_seconds(finished_at, threshold_seconds)
    active = age_seconds is None or age_seconds < threshold_seconds
    decision.update(
        {
            "decision": "active_penalty" if active else "expired",
            "active": active,
            "age_seconds": age_seconds,
            "expires_at": expires_at,
            "points": WORKSPACE_DISPATCH_NONPRODUCTIVE_BACKOFF_PENALTY if active else 0,
        }
    )
    return decision


def _workspace_dispatch_priority_score(
    workspace: Path,
    item: dict,
    args: argparse.Namespace,
    history: list[dict],
) -> dict:
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    task_counts = summary.get("task_counts") if isinstance(summary.get("task_counts"), dict) else {}
    continuation = summary.get("continuation") if isinstance(summary.get("continuation"), dict) else {}
    pending_tasks = int(task_counts.get("pending", 0) or 0)
    pending_continuations = int(continuation.get("pending_stage_count", 0) or 0)
    pending_task_points = min(pending_tasks, 20) * 100
    continuation_points = min(pending_continuations, 20) * 60
    goal_gap = _workspace_goal_gap_score(Path(str(item["root"])))
    history_score = _workspace_history_score(item, history)
    nonproductive_backoff = _workspace_nonproductive_backoff_decision(item, history, args)
    last_selected_age_seconds = history_score.get("last_selected_age_seconds")
    cooldown_active = (
        isinstance(last_selected_age_seconds, int)
        and last_selected_age_seconds < WORKSPACE_DISPATCH_SELECTED_COOLDOWN_SECONDS
    )
    cooldown_penalty = -300 if cooldown_active else 0
    total = (
        pending_task_points
        + continuation_points
        + int(goal_gap.get("points", 0) or 0)
        + int(history_score.get("points", 0) or 0)
        + cooldown_penalty
        + int(nonproductive_backoff.get("points", 0) or 0)
    )
    components = {
        "schema_version": 1,
        "policy": WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY,
        "workspace": str(workspace),
        "pending_tasks": {"count": pending_tasks, "points": pending_task_points},
        "pending_continuations": {"count": pending_continuations, "points": continuation_points},
        "goal_gap": goal_gap,
        "workspace_history": history_score,
        "cooldown": {
            "window_seconds": WORKSPACE_DISPATCH_SELECTED_COOLDOWN_SECONDS,
            "active": cooldown_active,
            "last_selected_age_seconds": last_selected_age_seconds,
            "points": cooldown_penalty,
        },
        "nonproductive_backoff": nonproductive_backoff,
        "total": total,
    }
    return {"score": total, "components": components}


def _workspace_priority_evidence(item: dict, args: argparse.Namespace) -> dict:
    components = item.get("score_components") if isinstance(item.get("score_components"), dict) else {}
    history = components.get("workspace_history") if isinstance(components.get("workspace_history"), dict) else {}
    cooldown = components.get("cooldown") if isinstance(components.get("cooldown"), dict) else {}
    backoff = item.get("backoff") if isinstance(item.get("backoff"), dict) else {}
    return {
        "schema_version": 1,
        "kind": "engineering-harness.workspace-dispatch-priority",
        "policy": _workspace_scheduler_policy(args),
        "eligible": bool(item.get("eligible", False)),
        "selected": bool(item.get("selected", False)),
        "score": item.get("score"),
        "scheduler_rank": item.get("scheduler_rank"),
        "path_order_index": item.get("index"),
        "tie_breaker": "resolved_project_path",
        "starvation_prevention": {
            "selected_count": history.get("selected_count"),
            "never_selected_bonus": history.get("never_selected_bonus", 0),
            "last_selected_at": history.get("last_selected_at"),
            "last_selected_age_seconds": history.get("last_selected_age_seconds"),
            "age_points": history.get("age_points", 0),
            "cooldown_active": bool(cooldown.get("active", False)),
            "cooldown_points": cooldown.get("points", 0),
            "nonproductive_backoff_active": bool(backoff.get("active", False)),
            "nonproductive_backoff_points": backoff.get("points", 0),
        },
    }


def _workspace_retry_backoff_summary(item: dict) -> dict:
    existing = item.get("retry_backoff_summary") if isinstance(item.get("retry_backoff_summary"), dict) else {}
    task_retry = existing.get("task_retry") if isinstance(existing.get("task_retry"), dict) else None
    backoff = item.get("backoff") if isinstance(item.get("backoff"), dict) else None
    return {
        "schema_version": 1,
        "kind": "engineering-harness.workspace-dispatch-retry-backoff-summary",
        "task_retry": deepcopy(task_retry) if isinstance(task_retry, dict) else None,
        "nonproductive_backoff": deepcopy(backoff) if isinstance(backoff, dict) else None,
        "attempts_remaining": task_retry.get("attempts_remaining") if isinstance(task_retry, dict) else None,
        "retry_exhausted": bool(task_retry.get("exhausted", False)) if isinstance(task_retry, dict) else False,
        "backoff_active": bool(backoff.get("active", False)) if isinstance(backoff, dict) else False,
        "backoff_decision": backoff.get("decision") if isinstance(backoff, dict) else None,
        "backoff_reason": backoff.get("reason") if isinstance(backoff, dict) else None,
    }


def _finalize_workspace_dispatch_queue_evidence(queue: list[dict], args: argparse.Namespace) -> list[dict]:
    for item in queue:
        item["priority"] = _workspace_priority_evidence(item, args)
        item["retry_backoff_summary"] = _workspace_retry_backoff_summary(item)
    return queue


def _score_workspace_dispatch_queue(workspace: Path, queue: list[dict], args: argparse.Namespace) -> list[dict]:
    policy = _workspace_scheduler_policy(args)
    if policy == WORKSPACE_DISPATCH_PATH_ORDER_SCHEDULER_POLICY:
        for rank, item in enumerate(queue):
            item["scheduler_policy"] = policy
            item["scheduler_rank"] = rank
            if item.get("eligible"):
                item["score"] = 0
                item["score_components"] = {
                    "schema_version": 1,
                    "policy": policy,
                    "path_order_index": item.get("index"),
                    "total": 0,
                }
                item["backoff"] = {
                    "schema_version": 1,
                    "kind": "workspace_dispatch_nonproductive_backoff",
                    "decision": "not_evaluated_path_order",
                    "active": False,
                    "threshold_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
                    "points": 0,
                }
            else:
                item["score"] = None
                item["score_components"] = {
                    "schema_version": 1,
                    "policy": policy,
                    "blocked": True,
                    "skip_codes": [
                        reason.get("code")
                        for reason in item.get("skip_reasons", [])
                        if isinstance(reason, dict)
                    ],
                }
                item["backoff"] = {
                    "schema_version": 1,
                    "kind": "workspace_dispatch_nonproductive_backoff",
                    "decision": "blocked",
                    "active": False,
                    "threshold_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
                    "points": 0,
                }
        return _finalize_workspace_dispatch_queue_evidence(queue, args)

    history = _workspace_dispatch_history(workspace)
    for item in queue:
        item["scheduler_policy"] = policy
        if item.get("eligible"):
            score = _workspace_dispatch_priority_score(workspace, item, args, history)
            item["score"] = score["score"]
            item["score_components"] = score["components"]
            item["backoff"] = score["components"].get("nonproductive_backoff", {})
        else:
            item["score"] = None
            item["score_components"] = {
                "schema_version": 1,
                "policy": policy,
                "blocked": True,
                "skip_codes": [
                    reason.get("code")
                    for reason in item.get("skip_reasons", [])
                    if isinstance(reason, dict)
                ],
            }
            item["backoff"] = {
                "schema_version": 1,
                "kind": "workspace_dispatch_nonproductive_backoff",
                "decision": "blocked",
                "active": False,
                "threshold_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
                "points": 0,
            }

    eligible = sorted(
        [item for item in queue if item.get("eligible")],
        key=lambda item: (-int(item.get("score") or 0), _workspace_score_path_key(item)),
    )
    skipped = sorted([item for item in queue if not item.get("eligible")], key=lambda item: int(item.get("index", 0)))
    ordered = eligible + skipped
    for rank, item in enumerate(ordered):
        item["scheduler_rank"] = rank
    return _finalize_workspace_dispatch_queue_evidence(ordered, args)


def _workspace_selected_reason(selected: dict, args: argparse.Namespace) -> dict:
    policy = _workspace_scheduler_policy(args)
    if policy == WORKSPACE_DISPATCH_PATH_ORDER_SCHEDULER_POLICY:
        return {
            "code": "path_order_first_eligible",
            "message": "first eligible project by deterministic path order",
            "scheduler_policy": policy,
            "path_order_index": selected.get("index"),
        }
    return {
        "code": "highest_fair_score",
        "message": "highest fair scheduler score; resolved equal scores by deterministic path order",
        "scheduler_policy": policy,
        "score": selected.get("score"),
        "scheduler_rank": selected.get("scheduler_rank"),
        "tie_breaker": "resolved_project_path",
    }


def build_workspace_dispatch_queue(workspace: Path, args: argparse.Namespace) -> list[dict]:
    workspace = workspace.resolve()
    projects = discover_projects(workspace, max_depth=args.max_depth)
    queue = [
        _workspace_dispatch_queue_item(workspace, project, args, index)
        for index, project in enumerate(projects)
    ]
    return _score_workspace_dispatch_queue(workspace, queue, args)


def _workspace_stale_running_recoveries(queue: list[dict]) -> list[dict]:
    return [
        deepcopy(item["stale_running_recovery"])
        for item in queue
        if isinstance(item.get("stale_running_recovery"), dict)
    ]


def _workspace_stale_running_blocks(queue: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for item in queue:
        block = item.get("stale_running_block")
        if isinstance(block, dict):
            blocks.append(deepcopy(block))
            continue
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
        block = drive_control.get("stale_running_block")
        if isinstance(block, dict):
            blocks.append(deepcopy(block))
    return blocks



def _workspace_dispatch_report_path(workspace: Path) -> Path:
    report_dir = workspace / ".engineering" / "reports" / "workspace-dispatches"
    report_dir.mkdir(parents=True, exist_ok=True)
    base = report_dir / f"{slug_now()}-workspace-dispatch.md"
    candidate = base
    counter = 2
    while candidate.exists() or candidate.with_suffix(".json").exists():
        candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
    return candidate


def _workspace_relative(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _workspace_dispatch_limits(args: argparse.Namespace) -> dict:
    return {
        "scheduler_policy": _workspace_scheduler_policy(args),
        "nonproductive_backoff_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
        "max_tasks": args.max_tasks,
        "time_budget_seconds": args.time_budget_seconds,
        "rolling": bool(args.rolling),
        "self_iterate": bool(args.self_iterate),
        "max_continuations": args.max_continuations,
        "max_self_iterations": args.max_self_iterations,
        "continuation_batch_size": args.continuation_batch_size,
        "no_progress_limit": args.no_progress_limit,
        "allow_live": bool(args.allow_live),
        "allow_manual": bool(args.allow_manual),
        "allow_agent": bool(args.allow_agent),
        "push_after_task": False,
        "commit_after_task": False,
    }


def _workspace_dispatch_queue_summary(queue: list[dict], args: argparse.Namespace) -> dict:
    items = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        backoff = item.get("backoff") if isinstance(item.get("backoff"), dict) else {}
        project_lease = item.get("project_lease") if isinstance(item.get("project_lease"), dict) else {}
        items.append(
            {
                "project": item.get("project"),
                "root": item.get("root"),
                "scheduler_rank": item.get("scheduler_rank"),
                "eligible": bool(item.get("eligible", False)),
                "selected": bool(item.get("selected", False)),
                "dispatch_status": item.get("dispatch_status"),
                "score": item.get("score"),
                "skip_codes": [
                    str(reason.get("code"))
                    for reason in item.get("skip_reasons", [])
                    if isinstance(reason, dict) and reason.get("code")
                ],
                "backoff_active": bool(backoff.get("active", False)),
                "backoff_decision": backoff.get("decision"),
                "project_lease_status": project_lease.get("status"),
                "project_lease_active": bool(project_lease.get("active", False)),
            }
        )
    return {
        "schema_version": 1,
        "kind": "engineering-harness.workspace-dispatch-queue-summary",
        "scheduler_policy": _workspace_scheduler_policy(args),
        "item_count": len(queue),
        "eligible_count": sum(1 for item in queue if isinstance(item, dict) and item.get("eligible")),
        "skipped_count": sum(
            1
            for item in queue
            if isinstance(item, dict) and item.get("dispatch_status") == "skipped"
        ),
        "selected_project": next(
            (item.get("project") for item in queue if isinstance(item, dict) and item.get("selected")),
            None,
        ),
        "items": items,
    }


def _coerce_positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _workspace_dispatch_lease_stale_after_seconds(args: argparse.Namespace) -> int:
    cli_value = getattr(args, "lease_stale_after_seconds", None)
    if cli_value is not None:
        return _coerce_positive_int(cli_value, DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS)
    return _coerce_positive_int(
        os.environ.get(WORKSPACE_DISPATCH_LEASE_STALE_SECONDS_ENV),
        DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS,
    )


def _workspace_dispatch_lease_dir(workspace: Path) -> Path:
    return workspace / ".engineering" / "state" / WORKSPACE_DISPATCH_LEASE_DIRNAME


def _workspace_dispatch_lease_path(workspace: Path) -> Path:
    return _workspace_dispatch_lease_dir(workspace) / "lease.json"


def _workspace_dispatch_command_options(workspace: Path, args: argparse.Namespace) -> dict:
    payload = {
        "workspace": str(workspace),
        "max_depth": args.max_depth,
        "scheduler_policy": _workspace_scheduler_policy(args),
        "lease_stale_after_seconds": _workspace_dispatch_lease_stale_after_seconds(args),
    }
    payload.update(_workspace_dispatch_limits(args))
    return payload


def _workspace_dispatch_owner_pid(value) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _workspace_dispatch_process_is_running(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _workspace_dispatch_timestamp_age_seconds(value) -> int | None:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _write_workspace_dispatch_lease(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _read_workspace_dispatch_lease(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_dispatch_lease_snapshot(lease: dict | None) -> dict | None:
    if not isinstance(lease, dict):
        return None
    keys = [
        "schema_version",
        "kind",
        "status",
        "workspace",
        "owner_pid",
        "started_at",
        "last_heartbeat_at",
        "heartbeat_count",
        "selected_project",
        "command_options",
        "stale_after_seconds",
        "current_activity",
    ]
    return {key: lease.get(key) for key in keys if key in lease}


def _workspace_dispatch_lease_assessment(
    lease_dir: Path,
    lease_path: Path,
    *,
    stale_after_seconds: int,
) -> dict:
    checked_at = utc_now()
    lease = _read_workspace_dispatch_lease(lease_path)
    if lease is None:
        try:
            age_seconds = max(0, int(time.time() - lease_dir.stat().st_mtime))
        except OSError:
            age_seconds = None
        stale = age_seconds is not None and age_seconds > stale_after_seconds
        return {
            "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
            "status": "stale" if stale else "held",
            "stale": stale,
            "reason": "missing_lease_metadata" if stale else None,
            "message": (
                "workspace dispatch lease metadata is missing"
                if stale
                else "workspace dispatch lease metadata is being created"
            ),
            "checked_at": checked_at,
            "threshold_seconds": stale_after_seconds,
            "pid": None,
            "pid_alive": None,
            "heartbeat_at": None,
            "heartbeat_age_seconds": age_seconds,
            "holder": None,
        }

    threshold = _coerce_positive_int(lease.get("stale_after_seconds"), stale_after_seconds)
    pid = _workspace_dispatch_owner_pid(lease.get("owner_pid"))
    pid_alive = _workspace_dispatch_process_is_running(pid)
    heartbeat_at = lease.get("last_heartbeat_at")
    heartbeat_age_seconds = _workspace_dispatch_timestamp_age_seconds(heartbeat_at)
    stale = False
    reason = None
    message = "workspace dispatch lease is held by a live process with a fresh heartbeat"
    if pid is None:
        stale = True
        reason = "missing_pid"
        message = "workspace dispatch lease has no owner pid"
    elif pid_alive is False:
        stale = True
        reason = "pid_gone"
        message = f"workspace dispatch lease owner pid is not running: {pid}"
    elif heartbeat_age_seconds is None:
        stale = True
        reason = "missing_heartbeat"
        message = "workspace dispatch lease has no heartbeat"
    elif heartbeat_age_seconds > threshold:
        stale = True
        reason = "heartbeat_stale"
        message = f"workspace dispatch lease heartbeat is stale after {heartbeat_age_seconds}s"
    return {
        "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
        "status": "stale" if stale else "held",
        "stale": stale,
        "reason": reason,
        "message": message,
        "checked_at": checked_at,
        "threshold_seconds": threshold,
        "pid": pid,
        "pid_alive": pid_alive,
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "holder": _workspace_dispatch_lease_snapshot(lease),
    }


def _recover_workspace_dispatch_lease_dir(lease_dir: Path) -> dict:
    recovered_at = utc_now()
    recovery_dir = lease_dir.with_name(
        f"{lease_dir.name}.recovered-{os.getpid()}-{int(time.time() * 1_000_000)}"
    )
    try:
        os.rename(lease_dir, recovery_dir)
    except FileNotFoundError:
        return {
            "recovered": True,
            "recovered_at": recovered_at,
            "message": "workspace dispatch lease disappeared before recovery",
        }
    except OSError as exc:
        return {
            "recovered": False,
            "recovered_at": recovered_at,
            "message": f"could not recover stale workspace dispatch lease: {exc}",
        }
    shutil.rmtree(recovery_dir, ignore_errors=True)
    return {
        "recovered": True,
        "recovered_at": recovered_at,
        "message": "stale workspace dispatch lease was recovered",
    }


def _new_workspace_dispatch_lease(
    workspace: Path,
    args: argparse.Namespace,
    *,
    stale_after_seconds: int,
    recovery: dict | None,
) -> dict:
    now = utc_now()
    payload = {
        "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
        "kind": "engineering-harness.workspace-dispatch-lease",
        "status": "running",
        "workspace": str(workspace),
        "owner_pid": os.getpid(),
        "started_at": now,
        "last_heartbeat_at": now,
        "heartbeat_count": 1,
        "selected_project": None,
        "command_options": _workspace_dispatch_command_options(workspace, args),
        "stale_after_seconds": stale_after_seconds,
        "current_activity": "workspace-dispatch-starting",
    }
    if recovery:
        payload["recovered_from"] = recovery
    return payload


def acquire_workspace_dispatch_lease(workspace: Path, args: argparse.Namespace) -> dict:
    lease_dir = _workspace_dispatch_lease_dir(workspace)
    lease_path = _workspace_dispatch_lease_path(workspace)
    lease_dir.parent.mkdir(parents=True, exist_ok=True)
    stale_after_seconds = _workspace_dispatch_lease_stale_after_seconds(args)
    recovery: dict | None = None
    for _attempt in range(10):
        try:
            os.mkdir(lease_dir)
        except FileExistsError:
            assessment = _workspace_dispatch_lease_assessment(
                lease_dir,
                lease_path,
                stale_after_seconds=stale_after_seconds,
            )
            if not assessment.get("stale"):
                return {
                    "acquired": False,
                    "status": "held",
                    "lease_dir": lease_dir,
                    "lease_path": lease_path,
                    "assessment": assessment,
                }
            recovered = _recover_workspace_dispatch_lease_dir(lease_dir)
            recovery = {
                "reason": assessment.get("reason"),
                "message": assessment.get("message"),
                "assessment": assessment,
                "recovery": recovered,
            }
            if recovered.get("recovered"):
                continue
            return {
                "acquired": False,
                "status": "recovery_failed",
                "lease_dir": lease_dir,
                "lease_path": lease_path,
                "assessment": assessment,
                "recovery": recovery,
            }
        else:
            lease = _new_workspace_dispatch_lease(
                workspace,
                args,
                stale_after_seconds=stale_after_seconds,
                recovery=recovery,
            )
            _write_workspace_dispatch_lease(lease_path, lease)
            return {
                "acquired": True,
                "status": "acquired",
                "lease_dir": lease_dir,
                "lease_path": lease_path,
                "lease": lease,
                "recovery": recovery,
                "_lock": threading.Lock(),
            }
    return {
        "acquired": False,
        "status": "acquire_raced",
        "lease_dir": lease_dir,
        "lease_path": lease_path,
        "assessment": _workspace_dispatch_lease_assessment(
            lease_dir,
            lease_path,
            stale_after_seconds=stale_after_seconds,
        ),
    }


def _workspace_dispatch_lease_matches(lease: dict | None, acquisition: dict) -> bool:
    expected = acquisition.get("lease") if isinstance(acquisition.get("lease"), dict) else {}
    return (
        isinstance(lease, dict)
        and lease.get("owner_pid") == expected.get("owner_pid")
        and lease.get("started_at") == expected.get("started_at")
    )


def heartbeat_workspace_dispatch_lease(
    acquisition: dict,
    *,
    activity: str,
    selected_project: dict | None = None,
) -> dict | None:
    if not acquisition.get("acquired"):
        return None
    lock = acquisition.get("_lock")
    if lock is None:
        lock = threading.Lock()
        acquisition["_lock"] = lock
    with lock:
        lease_path = acquisition["lease_path"]
        current = _read_workspace_dispatch_lease(lease_path)
        if not _workspace_dispatch_lease_matches(current, acquisition):
            return None
        now = utc_now()
        current["last_heartbeat_at"] = now
        current["heartbeat_count"] = int(current.get("heartbeat_count", 0) or 0) + 1
        current["current_activity"] = activity
        if selected_project is not None:
            current["selected_project"] = selected_project
        _write_workspace_dispatch_lease(lease_path, current)
        acquisition["lease"] = current
        return current


def start_workspace_dispatch_lease_heartbeat(acquisition: dict) -> tuple[threading.Event, threading.Thread] | None:
    if not acquisition.get("acquired"):
        return None
    lease = acquisition.get("lease") if isinstance(acquisition.get("lease"), dict) else {}
    stale_after_seconds = _coerce_positive_int(
        lease.get("stale_after_seconds"),
        DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS,
    )
    interval = max(1, min(30, stale_after_seconds // 3 if stale_after_seconds > 3 else 1))
    stop_event = threading.Event()

    def run_heartbeat() -> None:
        while not stop_event.wait(interval):
            heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-running")

    thread = threading.Thread(target=run_heartbeat, name="workspace-dispatch-lease-heartbeat", daemon=True)
    thread.start()
    return stop_event, thread


def release_workspace_dispatch_lease(acquisition: dict) -> dict:
    released_at = utc_now()
    if not acquisition.get("acquired"):
        return {
            "status": "not_acquired",
            "released_at": released_at,
            "message": "workspace dispatch lease was not acquired by this process",
        }
    lock = acquisition.get("_lock")
    if lock is None:
        lock = threading.Lock()
        acquisition["_lock"] = lock
    with lock:
        lease_dir = acquisition["lease_dir"]
        lease_path = acquisition["lease_path"]
        current = _read_workspace_dispatch_lease(lease_path)
        if current is None:
            if not lease_dir.exists():
                return {
                    "status": "already_released",
                    "released_at": released_at,
                    "message": "workspace dispatch lease was already released",
                }
            return {
                "status": "missing_metadata",
                "released_at": released_at,
                "message": "workspace dispatch lease metadata is missing; release left directory in place",
            }
        if not _workspace_dispatch_lease_matches(current, acquisition):
            return {
                "status": "not_owner",
                "released_at": released_at,
                "message": "workspace dispatch lease is now owned by another process",
                "current_holder": _workspace_dispatch_lease_snapshot(current),
            }
        shutil.rmtree(lease_dir, ignore_errors=True)
        return {
            "status": "released",
            "released_at": released_at,
            "message": "workspace dispatch lease released",
        }


def workspace_dispatch_lease_payload(
    workspace: Path,
    acquisition: dict,
    *,
    release: dict | None = None,
) -> dict:
    lease_path = acquisition.get("lease_path")
    path = _workspace_relative(workspace, lease_path) if isinstance(lease_path, Path) else None
    if not acquisition.get("acquired"):
        return {
            "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
            "status": acquisition.get("status", "held"),
            "acquired": False,
            "path": path,
            "assessment": acquisition.get("assessment"),
            "recovery": acquisition.get("recovery"),
        }
    lease = acquisition.get("lease") if isinstance(acquisition.get("lease"), dict) else {}
    return {
        "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
        "status": (release or {}).get("status") or acquisition.get("status", "acquired"),
        "acquired": True,
        "path": path,
        "owner_pid": lease.get("owner_pid"),
        "started_at": lease.get("started_at"),
        "last_heartbeat_at": lease.get("last_heartbeat_at"),
        "heartbeat_count": lease.get("heartbeat_count"),
        "selected_project": lease.get("selected_project"),
        "command_options": lease.get("command_options"),
        "stale_after_seconds": lease.get("stale_after_seconds"),
        "current_activity": lease.get("current_activity"),
        "recovered": bool(acquisition.get("recovery")),
        "recovery": acquisition.get("recovery"),
        "release": release,
    }


def write_workspace_dispatch_report(workspace: Path, payload: dict) -> str:
    report_path = _workspace_dispatch_report_path(workspace)
    json_path = report_path.with_suffix(".json")
    payload["dispatch_report"] = _workspace_relative(workspace, report_path)
    payload["dispatch_report_json"] = _workspace_relative(workspace, json_path)
    lines = [
        "# Workspace Drive Dispatch Report",
        "",
        f"- Workspace: `{payload['workspace']}`",
        f"- Status: `{payload['status']}`",
        f"- Started: {payload['started_at']}",
        f"- Finished: {payload['finished_at']}",
        f"- Projects scanned: `{len(payload.get('queue', []))}`",
        f"- Eligible projects: `{payload.get('eligible_count', 0)}`",
        f"- Scheduler policy: `{payload.get('scheduler_policy') or payload.get('limits', {}).get('scheduler_policy')}`",
        f"- Selected project: `{(payload.get('selected') or {}).get('project') or 'none'}`",
        f"- Message: {payload['message']}",
        "",
        "## Workspace Dispatch Lease",
        "",
    ]
    lease = payload.get("lease") if isinstance(payload.get("lease"), dict) else {}
    if not lease:
        lines.append("No workspace dispatch lease evidence was recorded.")
    else:
        lines.extend(
            [
                f"- Status: `{lease.get('status')}`",
                f"- Acquired: `{str(bool(lease.get('acquired'))).lower()}`",
                f"- Path: `{lease.get('path')}`",
                f"- Owner pid: `{lease.get('owner_pid') or 'unknown'}`",
                f"- Stale after seconds: `{lease.get('stale_after_seconds') or 'unknown'}`",
            ]
        )
        selected_project = lease.get("selected_project")
        if isinstance(selected_project, dict):
            lines.append(f"- Lease selected project: `{selected_project.get('project')}`")
        assessment = lease.get("assessment") if isinstance(lease.get("assessment"), dict) else {}
        if assessment.get("reason"):
            lines.append(f"- Lease reason: `{assessment.get('reason')}`")
        release = lease.get("release") if isinstance(lease.get("release"), dict) else {}
        if release.get("released_at"):
            lines.append(f"- Released: {release.get('released_at')}")
        lines.extend(
            [
                "",
                "Machine-readable lease:",
                "",
                "```json",
                json.dumps(lease, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    recoveries = payload.get("stale_running_recoveries") if isinstance(payload.get("stale_running_recoveries"), list) else []
    blocks = payload.get("stale_running_blocks") if isinstance(payload.get("stale_running_blocks"), list) else []
    lines.extend(["## Project Stale Running Recovery", ""])
    if not recoveries and not blocks:
        lines.append("No project stale running recovery evidence was recorded.")
    for recovery in recoveries:
        if not isinstance(recovery, dict):
            continue
        lines.append(
            "- Recovered "
            f"`{recovery.get('reason')}` previous_pid=`{recovery.get('previous_pid') or 'unknown'}` "
            f"heartbeat_age=`{recovery.get('heartbeat_age_seconds')}` "
            f"threshold=`{recovery.get('threshold_seconds')}` recovered_at={recovery.get('recovered_at')}"
        )
    for block in blocks:
        if not isinstance(block, dict):
            continue
        lines.append(
            "- Blocked "
            f"`{block.get('reason')}` previous_pid=`{block.get('previous_pid') or 'unknown'}` "
            f"heartbeat_age=`{block.get('heartbeat_age_seconds')}` "
            f"threshold=`{block.get('threshold_seconds')}`"
        )
    if recoveries or blocks:
        lines.extend(
            [
                "",
                "Machine-readable project stale running recovery:",
                "",
                "```json",
                json.dumps({"recoveries": recoveries, "blocks": blocks}, indent=2, sort_keys=True),
                "```",
            ]
        )
    lines.append("")
    lines.extend(
        [
        "## Dispatch Queue",
        "",
        ]
    )
    if not payload.get("queue"):
        lines.append("No projects were discovered.")
    for item in payload.get("queue", []):
        lines.append(
            f"- rank=`{item.get('scheduler_rank')}` path=`{item.get('index')}` `{item.get('project')}` "
            f"`{item.get('dispatch_status')}` eligible=`{str(bool(item.get('eligible'))).lower()}` "
            f"score=`{item.get('score')}` root=`{item.get('root')}`"
        )
        selected_reason = item.get("selected_reason") if isinstance(item.get("selected_reason"), dict) else {}
        if selected_reason:
            lines.append(f"  - Selected reason: `{selected_reason.get('code')}` {selected_reason.get('message')}")
        components = item.get("score_components") if isinstance(item.get("score_components"), dict) else {}
        if components:
            lines.extend(
                [
                    "  - Score components:",
                    "",
                    "    ```json",
                    "\n".join(
                        f"    {line}" for line in json.dumps(components, indent=2, sort_keys=True).splitlines()
                    ),
                    "    ```",
                ]
            )
        priority = item.get("priority") if isinstance(item.get("priority"), dict) else {}
        if priority:
            starvation = (
                priority.get("starvation_prevention")
                if isinstance(priority.get("starvation_prevention"), dict)
                else {}
            )
            lines.append(
                "  - Priority: "
                f"rank=`{priority.get('scheduler_rank')}` score=`{priority.get('score')}` "
                f"cooldown=`{str(bool(starvation.get('cooldown_active'))).lower()}` "
                f"backoff=`{str(bool(starvation.get('nonproductive_backoff_active'))).lower()}`"
            )
        resource_budget = item.get("resource_budget") if isinstance(item.get("resource_budget"), dict) else {}
        if resource_budget:
            budget = (
                resource_budget.get("per_invocation")
                if isinstance(resource_budget.get("per_invocation"), dict)
                else {}
            )
            demand = (
                resource_budget.get("project_demand")
                if isinstance(resource_budget.get("project_demand"), dict)
                else {}
            )
            lines.append(
                "  - Resource budget: "
                f"max_tasks=`{budget.get('max_tasks')}` time_budget_seconds=`{budget.get('time_budget_seconds')}` "
                f"pending_tasks=`{demand.get('pending_tasks')}` "
                f"pending_continuations=`{demand.get('pending_continuations')}`"
            )
        project_lease = item.get("project_lease") if isinstance(item.get("project_lease"), dict) else {}
        if project_lease:
            lines.append(
                "  - Project lease: "
                f"`{project_lease.get('status')}` active=`{str(bool(project_lease.get('active'))).lower()}` "
                f"protected=`{str(bool(project_lease.get('protected'))).lower()}` "
                f"owner_pid=`{project_lease.get('owner_pid') or 'none'}`"
            )
        retry_backoff = (
            item.get("retry_backoff_summary")
            if isinstance(item.get("retry_backoff_summary"), dict)
            else {}
        )
        if retry_backoff:
            lines.append(
                "  - Retry/backoff summary: "
                f"attempts_remaining=`{retry_backoff.get('attempts_remaining')}` "
                f"retry_exhausted=`{str(bool(retry_backoff.get('retry_exhausted'))).lower()}` "
                f"backoff=`{retry_backoff.get('backoff_decision') or 'none'}`"
            )
        backoff = item.get("backoff") if isinstance(item.get("backoff"), dict) else {}
        if backoff:
            lines.append(
                "  - Nonproductive backoff: "
                f"`{backoff.get('decision')}` active=`{str(bool(backoff.get('active'))).lower()}` "
                f"reason=`{backoff.get('reason') or 'none'}` "
                f"age=`{backoff.get('age_seconds')}` threshold=`{backoff.get('threshold_seconds')}` "
                f"expires=`{backoff.get('expires_at') or 'none'}` source=`{backoff.get('source_report') or 'none'}`"
            )
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
        if next_task:
            lines.append(f"  - Next task: `{next_task.get('id')}` {next_task.get('title')}")
        checkpoint_readiness = item.get("checkpoint_readiness")
        if isinstance(checkpoint_readiness, dict):
            lines.append(
                "  - Checkpoint readiness: "
                f"`{checkpoint_readiness.get('reason')}` "
                f"ready=`{str(bool(checkpoint_readiness.get('ready'))).lower()}` "
                f"blocking=`{str(bool(checkpoint_readiness.get('blocking'))).lower()}` "
                f"blocking_paths=`{len(checkpoint_readiness.get('blocking_paths', []))}`"
            )
        stale_recovery = item.get("stale_running_recovery")
        if isinstance(stale_recovery, dict):
            lines.append(
                "  - Stale running recovery: "
                f"`{stale_recovery.get('reason')}` "
                f"previous_pid=`{stale_recovery.get('previous_pid') or 'unknown'}` "
                f"recovered_at=`{stale_recovery.get('recovered_at')}`"
            )
        stale_block = item.get("stale_running_block")
        if isinstance(stale_block, dict):
            lines.append(
                "  - Stale running block: "
                f"`{stale_block.get('reason')}` "
                f"previous_pid=`{stale_block.get('previous_pid') or 'unknown'}`"
            )
        for reason in item.get("skip_reasons", []):
            lines.append(f"  - Skip `{reason.get('code')}`: {reason.get('message')}")

    drive = payload.get("drive") if isinstance(payload.get("drive"), dict) else None
    lines.extend(["", "## Selected Drive", ""])
    if not drive:
        lines.append("No project drive was started.")
    else:
        lines.extend(
            [
                f"- Status: `{drive.get('status')}`",
                f"- Tasks run: `{len(drive.get('results', []))}`",
                f"- Continuations: `{len(drive.get('continuations', []))}`",
                f"- Self-iterations: `{len(drive.get('self_iterations', []))}`",
                f"- Drive report: `{drive.get('drive_report')}`",
            ]
        )

    machine_payload = {key: value for key, value in payload.items() if key != "drive"}
    if drive:
        machine_payload["drive"] = {
            "project": drive.get("project"),
            "root": drive.get("root"),
            "status": drive.get("status"),
            "message": drive.get("message"),
            "drive_report": drive.get("drive_report"),
            "drive_report_json": drive.get("drive_report_json"),
            "result_count": len(drive.get("results", [])),
            "continuation_count": len(drive.get("continuations", [])),
            "self_iteration_count": len(drive.get("self_iterations", [])),
        }
    lines.extend(
        [
            "",
            "## Machine-Readable Dispatch",
            "",
            "```json",
            json.dumps(machine_payload, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(json_path, payload)
    return payload["dispatch_report"]


def workspace_drive_dispatch(args: argparse.Namespace) -> tuple[int, dict]:
    workspace = Path(args.workspace).resolve()
    started_at = utc_now()
    acquisition = acquire_workspace_dispatch_lease(workspace, args)
    if not acquisition.get("acquired"):
        status = "lease_held" if acquisition.get("status") == "held" else "lease_unavailable"
        assessment = acquisition.get("assessment") if isinstance(acquisition.get("assessment"), dict) else {}
        message = assessment.get("message") or "workspace dispatch lease is not available"
        payload = {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-drive-dispatch",
            "workspace": str(workspace),
            "scheduler_policy": _workspace_scheduler_policy(args),
            "status": status,
            "message": message,
            "started_at": started_at,
            "finished_at": utc_now(),
            "limits": _workspace_dispatch_limits(args),
            "queue": [],
            "queue_summary": _workspace_dispatch_queue_summary([], args),
            "eligible_count": 0,
            "skipped_count": 0,
            "selected": None,
            "drive": None,
            "lease": workspace_dispatch_lease_payload(workspace, acquisition),
            "stale_running_recoveries": [],
            "stale_running_blocks": [],
        }
        write_workspace_dispatch_report(workspace, payload)
        return 1, payload

    heartbeat = start_workspace_dispatch_lease_heartbeat(acquisition)
    payload: dict | None = None
    drive_exit_code = 0
    try:
        heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-scanning")
        queue = build_workspace_dispatch_queue(workspace, args)
        heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-queue-built")
        selected = next((item for item in queue if item.get("eligible")), None)
        drive_payload = None
        if selected is None:
            status = "no_eligible_project"
            message = "No eligible project drive was found."
        else:
            selected["selected"] = True
            selected["dispatch_status"] = "dispatched"
            selected["selected_reason"] = _workspace_selected_reason(selected, args)
            selected["priority"] = _workspace_priority_evidence(selected, args)
            selected["retry_backoff_summary"] = _workspace_retry_backoff_summary(selected)
            selected_project = {
                "project": selected.get("project"),
                "root": selected.get("root"),
                "queue_index": selected.get("index"),
                "scheduler_rank": selected.get("scheduler_rank"),
                "scheduler_policy": selected.get("scheduler_policy"),
                "score": selected.get("score"),
                "priority": selected.get("priority"),
                "resource_budget": selected.get("resource_budget"),
                "project_lease": selected.get("project_lease"),
                "retry_backoff_summary": selected.get("retry_backoff_summary"),
                "selected_reason": selected.get("selected_reason"),
                "backoff": selected.get("backoff"),
                "checkpoint_readiness": selected.get("checkpoint_readiness"),
                "stale_running_recovery": selected.get("stale_running_recovery"),
                "stale_running_preflight": selected.get("stale_running_preflight"),
            }
            heartbeat_workspace_dispatch_lease(
                acquisition,
                activity="workspace-dispatch-selected",
                selected_project=selected_project,
            )
            for item in queue:
                if item is selected or not item.get("eligible"):
                    continue
                item["dispatch_status"] = "skipped"
                _add_dispatch_skip(
                    item,
                    "one_project_per_invocation",
                    "another eligible project was selected by the workspace scheduler",
                    selected_project=selected.get("project"),
                    selected_root=selected.get("root"),
                    scheduler_policy=_workspace_scheduler_policy(args),
                    selected_score=selected.get("score"),
                    project_score=item.get("score"),
                )
                item["retry_backoff_summary"] = _workspace_retry_backoff_summary(item)
            drive_args = _workspace_drive_args(args, Path(str(selected["root"])))
            heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-driving")
            drive_exit_code, drive_payload = run_project_drive(Path(str(selected["root"])), drive_args)
            heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-drive-finished")
            selected["drive_exit_code"] = drive_exit_code
            selected["drive_status"] = drive_payload.get("status")
            selected["drive_report"] = drive_payload.get("drive_report")
            selected["drive_report_json"] = drive_payload.get("drive_report_json")
            status = "dispatched" if drive_exit_code == 0 else "drive_failed"
            message = f"Dispatched project {selected['project']}: {drive_payload.get('status')}"

        eligible_count = sum(1 for item in queue if item.get("eligible"))
        skipped_count = sum(1 for item in queue if item.get("dispatch_status") == "skipped")
        payload = {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-drive-dispatch",
            "workspace": str(workspace),
            "scheduler_policy": _workspace_scheduler_policy(args),
            "status": status,
            "message": message,
            "started_at": started_at,
            "finished_at": utc_now(),
            "limits": _workspace_dispatch_limits(args),
            "queue": queue,
            "queue_summary": _workspace_dispatch_queue_summary(queue, args),
            "eligible_count": eligible_count,
            "skipped_count": skipped_count,
            "selected": (
                {
                    "project": selected.get("project"),
                    "root": selected.get("root"),
                    "queue_index": selected.get("index"),
                    "scheduler_rank": selected.get("scheduler_rank"),
                    "scheduler_policy": selected.get("scheduler_policy"),
                    "score": selected.get("score"),
                    "score_components": selected.get("score_components"),
                    "priority": selected.get("priority"),
                    "resource_budget": selected.get("resource_budget"),
                    "project_lease": selected.get("project_lease"),
                    "retry_backoff_summary": selected.get("retry_backoff_summary"),
                    "selected_reason": selected.get("selected_reason"),
                    "backoff": selected.get("backoff"),
                    "checkpoint_readiness": selected.get("checkpoint_readiness"),
                    "stale_running_recovery": selected.get("stale_running_recovery"),
                    "stale_running_preflight": selected.get("stale_running_preflight"),
                    "drive_status": selected.get("drive_status"),
                    "drive_exit_code": selected.get("drive_exit_code"),
                    "drive_report": selected.get("drive_report"),
                    "drive_report_json": selected.get("drive_report_json"),
                }
                if selected
                else None
            ),
            "drive": drive_payload,
            "stale_running_recoveries": _workspace_stale_running_recoveries(queue),
            "stale_running_blocks": _workspace_stale_running_blocks(queue),
        }
        return drive_exit_code, payload
    finally:
        if heartbeat is not None:
            stop_event, thread = heartbeat
            stop_event.set()
            thread.join(timeout=1)
        release = release_workspace_dispatch_lease(acquisition)
        if payload is not None:
            payload["finished_at"] = utc_now()
            payload["lease"] = workspace_dispatch_lease_payload(workspace, acquisition, release=release)
            write_workspace_dispatch_report(workspace, payload)


def _daemon_supervisor_runtime_state_path(workspace: Path) -> Path:
    return workspace / ".engineering" / "state" / DAEMON_SUPERVISOR_RUNTIME_STATE_FILENAME


def _daemon_supervisor_runtime_report_dir(workspace: Path) -> Path:
    return workspace / ".engineering" / "reports" / DAEMON_SUPERVISOR_RUNTIME_REPORT_DIRNAME


def _daemon_supervisor_runtime_report_path(workspace: Path) -> Path:
    report_dir = _daemon_supervisor_runtime_report_dir(workspace)
    report_dir.mkdir(parents=True, exist_ok=True)
    base = report_dir / f"{slug_now()}-daemon-supervisor-runtime.md"
    candidate = base
    counter = 2
    while candidate.exists() or candidate.with_suffix(".json").exists():
        candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
    return candidate


def _read_daemon_supervisor_runtime_state(workspace: Path) -> dict | None:
    return _read_json_mapping(_daemon_supervisor_runtime_state_path(workspace))


def _write_daemon_supervisor_runtime_state(workspace: Path, state: dict) -> None:
    write_json(_daemon_supervisor_runtime_state_path(workspace), state)


def _daemon_supervisor_runtime_stale_after_seconds(args: argparse.Namespace) -> int:
    cli_value = getattr(args, "runtime_stale_after_seconds", None)
    if cli_value is not None:
        return _coerce_positive_int(cli_value, DEFAULT_DAEMON_SUPERVISOR_RUNTIME_STALE_SECONDS)
    return _coerce_positive_int(
        os.environ.get(DAEMON_SUPERVISOR_RUNTIME_STALE_SECONDS_ENV),
        DEFAULT_DAEMON_SUPERVISOR_RUNTIME_STALE_SECONDS,
    )


def _daemon_supervisor_deadline(started_at: str, run_window_seconds: int) -> str | None:
    if run_window_seconds <= 0:
        return None
    started = parse_utc_timestamp(started_at) or datetime.now(timezone.utc)
    return _workspace_format_utc(started + timedelta(seconds=run_window_seconds))


def _daemon_supervisor_runtime_options(args: argparse.Namespace, workspace: Path) -> dict:
    return {
        "workspace": str(workspace),
        "max_depth": args.max_depth,
        "max_ticks": args.max_ticks,
        "run_window_seconds": args.run_window_seconds,
        "sleep_seconds": args.sleep_seconds,
        "idle_sleep_seconds": args.idle_sleep_seconds,
        "idle_stop_count": args.idle_stop_count,
        "nonproductive_backoff_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
        "runtime_stale_after_seconds": _daemon_supervisor_runtime_stale_after_seconds(args),
        "workspace_drive": _workspace_dispatch_limits(args),
    }


def _daemon_supervisor_previous_snapshot(state: dict | None) -> dict | None:
    if not isinstance(state, dict):
        return None
    return {
        "schema_version": state.get("schema_version"),
        "kind": state.get("kind"),
        "loop_id": state.get("loop_id"),
        "generation": state.get("generation"),
        "status": state.get("status"),
        "active": bool(state.get("active", False)),
        "owner_pid": state.get("owner_pid"),
        "started_at": state.get("started_at"),
        "last_heartbeat_at": state.get("last_heartbeat_at"),
        "heartbeat_count": state.get("heartbeat_count"),
        "run_window": deepcopy(state.get("run_window")) if isinstance(state.get("run_window"), dict) else None,
        "last_tick": deepcopy(state.get("last_tick")) if isinstance(state.get("last_tick"), dict) else None,
        "last_decision": deepcopy(state.get("last_decision")) if isinstance(state.get("last_decision"), dict) else None,
        "stop_reason": deepcopy(state.get("stop_reason")) if isinstance(state.get("stop_reason"), dict) else None,
        "latest_report": state.get("latest_report"),
        "latest_report_json": state.get("latest_report_json"),
    }


def _daemon_supervisor_completed_dispatch_reports(previous: dict | None) -> list[dict]:
    if not isinstance(previous, dict):
        return []
    restartable = previous.get("restartable_loop") if isinstance(previous.get("restartable_loop"), dict) else {}
    completed = restartable.get("completed_dispatch_reports") if isinstance(restartable.get("completed_dispatch_reports"), list) else []
    reports = [deepcopy(item) for item in completed if isinstance(item, dict)]
    last_tick = previous.get("last_tick") if isinstance(previous.get("last_tick"), dict) else {}
    report_json = last_tick.get("dispatch_report_json")
    if report_json and not any(item.get("dispatch_report_json") == report_json for item in reports):
        reports.append(
            {
                "tick_index": last_tick.get("tick_index"),
                "dispatch_status": last_tick.get("dispatch_status"),
                "drive_status": last_tick.get("drive_status"),
                "dispatch_report": last_tick.get("dispatch_report"),
                "dispatch_report_json": report_json,
                "recorded_at": last_tick.get("finished_at") or last_tick.get("started_at"),
            }
        )
    return reports[-DAEMON_SUPERVISOR_RUNTIME_HISTORY_LIMIT:]


def _daemon_supervisor_runtime_assessment(
    state: dict | None,
    *,
    stale_after_seconds: int,
) -> dict:
    checked_at = utc_now()
    if not isinstance(state, dict) or not state:
        return {
            "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "status": "not_found",
            "active": False,
            "recoverable": False,
            "blocking": False,
            "checked_at": checked_at,
        }
    status = str(state.get("status", "idle"))
    active = bool(state.get("active", False)) or status == "running"
    pid = _workspace_dispatch_owner_pid(state.get("owner_pid"))
    pid_alive = _workspace_dispatch_process_is_running(pid)
    heartbeat_at = state.get("last_heartbeat_at")
    heartbeat_age_seconds = _workspace_dispatch_timestamp_age_seconds(heartbeat_at)
    heartbeat_stale = heartbeat_age_seconds is None or heartbeat_age_seconds > stale_after_seconds
    if not active:
        return {
            "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "status": "not_needed",
            "active": False,
            "recoverable": False,
            "blocking": False,
            "checked_at": checked_at,
            "previous_status": status,
            "previous_pid": pid,
            "pid_alive": pid_alive,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "threshold_seconds": stale_after_seconds,
        }

    if pid is None:
        reason = "missing_pid"
    elif pid_alive is False:
        reason = "pid_gone"
    elif heartbeat_stale:
        reason = "heartbeat_stale"
    else:
        reason = "runtime_in_progress"

    recoverable = reason in {"missing_pid", "pid_gone", "heartbeat_stale"}
    return {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "status": "recoverable" if recoverable else "in_progress",
        "active": True,
        "recoverable": recoverable,
        "blocking": not recoverable,
        "reason": reason,
        "message": (
            "previous daemon supervisor runtime can be resumed from durable state"
            if recoverable
            else "daemon supervisor runtime is already active"
        ),
        "checked_at": checked_at,
        "previous_status": status,
        "previous_pid": pid,
        "pid_alive": pid_alive,
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "threshold_seconds": stale_after_seconds,
    }


def _daemon_supervisor_recovery_payload(previous: dict, assessment: dict) -> dict:
    recovered_at = utc_now()
    return {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "kind": "engineering-harness.daemon-supervisor-runtime-recovery",
        "status": "recovered",
        "reason": assessment.get("reason"),
        "message": "stale daemon supervisor runtime recovered before starting a new loop window",
        "recovered_at": recovered_at,
        "previous_loop": _daemon_supervisor_previous_snapshot(previous),
        "assessment": deepcopy(assessment),
    }


def _daemon_supervisor_new_state(
    workspace: Path,
    args: argparse.Namespace,
    *,
    previous: dict | None,
    recovery: dict | None,
) -> dict:
    started_at = utc_now()
    previous_restartable = previous.get("restartable_loop") if isinstance(previous, dict) and isinstance(previous.get("restartable_loop"), dict) else {}
    generation = int((previous or {}).get("generation", 0) or previous_restartable.get("generation", 0) or 0) + 1
    resume_count = int(previous_restartable.get("resume_count", 0) or 0) + (1 if previous else 0)
    completed_reports = _daemon_supervisor_completed_dispatch_reports(previous)
    loop_id = f"{slug_now()}-{os.getpid()}-{int(time.time() * 1_000_000)}"
    return {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "kind": DAEMON_SUPERVISOR_RUNTIME_KIND,
        "workspace": str(workspace),
        "state_path": _workspace_relative(workspace, _daemon_supervisor_runtime_state_path(workspace)),
        "status": "running",
        "active": True,
        "owner_pid": os.getpid(),
        "loop_id": loop_id,
        "generation": generation,
        "started_at": started_at,
        "finished_at": None,
        "last_heartbeat_at": started_at,
        "heartbeat_count": 1,
        "current_activity": "daemon-supervisor-starting",
        "run_window": {
            "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "started_at": started_at,
            "deadline_at": _daemon_supervisor_deadline(started_at, args.run_window_seconds),
            "window_seconds": args.run_window_seconds,
            "max_ticks": args.max_ticks,
            "tick_count": 0,
            "remaining_ticks": args.max_ticks,
        },
        "restartable_loop": {
            "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "generation": generation,
            "resume_count": resume_count,
            "resumed_from": _daemon_supervisor_previous_snapshot(previous),
            "recovered_previous": recovery,
            "completed_dispatch_reports": completed_reports,
        },
        "command_options": _daemon_supervisor_runtime_options(args, workspace),
        "ticks": [],
        "last_tick": None,
        "last_decision": None,
        "stop_reason": None,
        "history": [
            {
                "at": started_at,
                "event": "start",
                "loop_id": loop_id,
                "generation": generation,
                "recovered_previous": bool(recovery),
            }
        ],
        "latest_report": None,
        "latest_report_json": None,
    }


def _daemon_supervisor_heartbeat(
    workspace: Path,
    state: dict,
    *,
    activity: str,
    message: str | None = None,
) -> None:
    now = utc_now()
    state["owner_pid"] = os.getpid()
    state["last_heartbeat_at"] = now
    state["heartbeat_count"] = int(state.get("heartbeat_count", 0) or 0) + 1
    state["current_activity"] = activity
    if message is not None:
        state["last_progress_message"] = message
    _write_daemon_supervisor_runtime_state(workspace, state)


def _daemon_supervisor_window_expired(state: dict) -> bool:
    run_window = state.get("run_window") if isinstance(state.get("run_window"), dict) else {}
    deadline_at = run_window.get("deadline_at")
    if not deadline_at:
        return False
    deadline = parse_utc_timestamp(deadline_at)
    return deadline is not None and datetime.now(timezone.utc) >= deadline


def _daemon_supervisor_stop_reason(code: str, message: str, **evidence) -> dict:
    payload = {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "code": code,
        "message": message,
        "stopped_at": utc_now(),
    }
    payload.update({key: value for key, value in evidence.items() if value is not None})
    return payload


def _daemon_supervisor_progress(dispatch_payload: dict | None) -> dict:
    dispatch_payload = dispatch_payload if isinstance(dispatch_payload, dict) else {}
    drive = dispatch_payload.get("drive") if isinstance(dispatch_payload.get("drive"), dict) else None
    return _workspace_drive_progress_evidence(drive)


def _daemon_supervisor_tick_decision(
    dispatch_exit_code: int,
    dispatch_payload: dict,
    *,
    idle_count: int,
    args: argparse.Namespace,
) -> dict:
    status = str(dispatch_payload.get("status") or "unknown")
    progress = _daemon_supervisor_progress(dispatch_payload)
    selected = dispatch_payload.get("selected") if isinstance(dispatch_payload.get("selected"), dict) else {}
    drive_status = selected.get("drive_status") or (
        dispatch_payload.get("drive", {}).get("status") if isinstance(dispatch_payload.get("drive"), dict) else None
    )
    decision = {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "decided_at": utc_now(),
        "dispatch_status": status,
        "dispatch_exit_code": dispatch_exit_code,
        "drive_status": drive_status,
        "idle_count": idle_count,
        "sleep_seconds": 0,
        "backoff_seconds": 0,
        "progress": progress,
        "action": "continue",
        "reason": "productive_dispatch" if progress.get("useful_progress") else "dispatch_completed",
        "message": "continue supervisor loop",
    }
    if status == "no_eligible_project":
        decision.update(
            {
                "action": "stop" if idle_count >= args.idle_stop_count else "sleep",
                "reason": "no_eligible_project",
                "message": "no eligible project was available in the workspace queue",
                "sleep_seconds": args.idle_sleep_seconds,
            }
        )
        return decision
    if status == "lease_held":
        decision.update(
            {
                "action": "sleep",
                "reason": "workspace_dispatch_lease_held",
                "message": "workspace dispatch lease is held by another local process",
                "sleep_seconds": args.idle_sleep_seconds,
            }
        )
        return decision
    if dispatch_exit_code != 0 or status == "drive_failed":
        decision.update(
            {
                "action": "stop",
                "reason": "dispatch_failed",
                "message": str(dispatch_payload.get("message") or "workspace dispatch failed"),
                "backoff_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
            }
        )
        return decision
    if not progress.get("useful_progress") and status == "dispatched":
        decision.update(
            {
                "action": "sleep",
                "reason": "nonproductive_dispatch",
                "message": "selected dispatch did not complete a task or materialize useful work",
                "sleep_seconds": args.idle_sleep_seconds,
                "backoff_seconds": _workspace_dispatch_nonproductive_backoff_seconds(args),
            }
        )
        return decision
    decision["sleep_seconds"] = args.sleep_seconds
    return decision


def _daemon_supervisor_tick_payload(
    tick_index: int,
    dispatch_exit_code: int,
    dispatch_payload: dict,
    decision: dict,
) -> dict:
    selected = dispatch_payload.get("selected") if isinstance(dispatch_payload.get("selected"), dict) else {}
    return {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "tick_index": tick_index,
        "started_at": dispatch_payload.get("started_at"),
        "finished_at": dispatch_payload.get("finished_at"),
        "dispatch_status": dispatch_payload.get("status"),
        "dispatch_message": dispatch_payload.get("message"),
        "dispatch_exit_code": dispatch_exit_code,
        "dispatch_report": dispatch_payload.get("dispatch_report"),
        "dispatch_report_json": dispatch_payload.get("dispatch_report_json"),
        "selected": deepcopy(selected) if selected else None,
        "drive_status": selected.get("drive_status"),
        "drive_report": selected.get("drive_report"),
        "drive_report_json": selected.get("drive_report_json"),
        "eligible_count": dispatch_payload.get("eligible_count"),
        "skipped_count": dispatch_payload.get("skipped_count"),
        "decision": deepcopy(decision),
    }


def _daemon_supervisor_record_tick(workspace: Path, state: dict, tick: dict) -> None:
    ticks = state.setdefault("ticks", [])
    if not isinstance(ticks, list):
        ticks = []
        state["ticks"] = ticks
    ticks.append(deepcopy(tick))
    state["ticks"] = ticks[-DAEMON_SUPERVISOR_RUNTIME_HISTORY_LIMIT:]
    state["last_tick"] = deepcopy(tick)
    state["last_decision"] = deepcopy(tick.get("decision"))
    run_window = state.get("run_window") if isinstance(state.get("run_window"), dict) else {}
    run_window["tick_count"] = int(run_window.get("tick_count", 0) or 0) + 1
    max_ticks = int(run_window.get("max_ticks", 0) or 0)
    run_window["remaining_ticks"] = max(0, max_ticks - int(run_window.get("tick_count", 0) or 0))
    state["run_window"] = run_window
    completed = state.setdefault("restartable_loop", {}).setdefault("completed_dispatch_reports", [])
    if isinstance(completed, list) and tick.get("dispatch_report_json"):
        if not any(item.get("dispatch_report_json") == tick.get("dispatch_report_json") for item in completed):
            completed.append(
                {
                    "tick_index": tick.get("tick_index"),
                    "dispatch_status": tick.get("dispatch_status"),
                    "drive_status": tick.get("drive_status"),
                    "dispatch_report": tick.get("dispatch_report"),
                    "dispatch_report_json": tick.get("dispatch_report_json"),
                    "recorded_at": tick.get("finished_at") or utc_now(),
                }
            )
            state["restartable_loop"]["completed_dispatch_reports"] = completed[
                -DAEMON_SUPERVISOR_RUNTIME_HISTORY_LIMIT:
            ]
    _daemon_supervisor_heartbeat(
        workspace,
        state,
        activity="daemon-supervisor-tick-recorded",
        message=f"recorded supervisor tick {tick.get('tick_index')}: {tick.get('dispatch_status')}",
    )


def _daemon_supervisor_finalize_state(
    workspace: Path,
    state: dict,
    *,
    status: str,
    stop_reason: dict,
) -> dict:
    now = utc_now()
    state.update(
        {
            "status": status,
            "active": False,
            "owner_pid": None,
            "finished_at": now,
            "last_heartbeat_at": now,
            "current_activity": "daemon-supervisor-stopped",
            "stop_reason": stop_reason,
        }
    )
    history = state.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "at": now,
                "event": "stop",
                "status": status,
                "stop_reason": stop_reason.get("code"),
                "message": stop_reason.get("message"),
            }
        )
        state["history"] = history[-DAEMON_SUPERVISOR_RUNTIME_HISTORY_LIMIT:]
    _write_daemon_supervisor_runtime_state(workspace, state)
    return state


def _daemon_supervisor_report_tick_lines(ticks: list[dict]) -> list[str]:
    lines: list[str] = []
    if not ticks:
        return ["No supervisor ticks ran."]
    for tick in ticks:
        decision = tick.get("decision") if isinstance(tick.get("decision"), dict) else {}
        selected = tick.get("selected") if isinstance(tick.get("selected"), dict) else {}
        lines.append(
            f"- Tick `{tick.get('tick_index')}`: dispatch=`{tick.get('dispatch_status')}` "
            f"drive=`{tick.get('drive_status') or 'none'}` action=`{decision.get('action')}` "
            f"reason=`{decision.get('reason')}`"
        )
        if selected:
            lines.append(f"  - Selected: `{selected.get('project')}`")
        if tick.get("dispatch_report_json"):
            lines.append(f"  - Dispatch sidecar: `{tick.get('dispatch_report_json')}`")
        if tick.get("drive_report_json"):
            lines.append(f"  - Drive sidecar: `{tick.get('drive_report_json')}`")
        if decision.get("sleep_seconds"):
            lines.append(f"  - Sleep seconds: `{decision.get('sleep_seconds')}`")
        if decision.get("backoff_seconds"):
            lines.append(f"  - Backoff seconds: `{decision.get('backoff_seconds')}`")
    return lines


def write_daemon_supervisor_runtime_report(workspace: Path, payload: dict) -> str:
    report_path = _daemon_supervisor_runtime_report_path(workspace)
    json_path = report_path.with_suffix(".json")
    payload["runtime_report"] = _workspace_relative(workspace, report_path)
    payload["runtime_report_json"] = _workspace_relative(workspace, json_path)
    stop_reason = payload.get("stop_reason") if isinstance(payload.get("stop_reason"), dict) else {}
    run_window = payload.get("run_window") if isinstance(payload.get("run_window"), dict) else {}
    restartable = payload.get("restartable_loop") if isinstance(payload.get("restartable_loop"), dict) else {}
    lines = [
        "# Daemon Supervisor Runtime Report",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Started: {payload.get('started_at')}",
        f"- Finished: {payload.get('finished_at')}",
        f"- Stop reason: `{stop_reason.get('code')}` - {stop_reason.get('message')}",
        f"- Runtime state: `{payload.get('state_path')}`",
        "",
        "## Run Window",
        "",
        f"- Started: {run_window.get('started_at')}",
        f"- Deadline: {run_window.get('deadline_at') or 'none'}",
        f"- Window seconds: `{run_window.get('window_seconds')}`",
        f"- Tick count: `{run_window.get('tick_count')}`",
        f"- Max ticks: `{run_window.get('max_ticks')}`",
        "",
        "## Restartable Loop",
        "",
        f"- Generation: `{restartable.get('generation')}`",
        f"- Resume count: `{restartable.get('resume_count')}`",
        f"- Completed dispatch reports: `{len(restartable.get('completed_dispatch_reports', []))}`",
    ]
    if isinstance(restartable.get("recovered_previous"), dict):
        recovery = restartable["recovered_previous"]
        lines.append(f"- Recovered previous loop: `{recovery.get('reason')}` at {recovery.get('recovered_at')}")
    if isinstance(restartable.get("resumed_from"), dict):
        resumed = restartable["resumed_from"]
        lines.append(f"- Resumed from loop: `{resumed.get('loop_id')}` status=`{resumed.get('status')}`")
    lines.extend(
        [
            "",
            "## Tick Decisions",
            "",
            *_daemon_supervisor_report_tick_lines(payload.get("ticks", [])),
            "",
            "## Machine-Readable Runtime",
            "",
            "```json",
            json.dumps(payload, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(json_path, payload)
    return payload["runtime_report"]


def daemon_supervisor_runtime(args: argparse.Namespace) -> tuple[int, dict]:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    stale_after_seconds = _daemon_supervisor_runtime_stale_after_seconds(args)
    previous = _read_daemon_supervisor_runtime_state(workspace)
    assessment = _daemon_supervisor_runtime_assessment(previous, stale_after_seconds=stale_after_seconds)
    if assessment.get("blocking"):
        payload = {
            "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "kind": DAEMON_SUPERVISOR_RUNTIME_KIND,
            "workspace": str(workspace),
            "state_path": _workspace_relative(workspace, _daemon_supervisor_runtime_state_path(workspace)),
            "status": "already_running",
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "run_window": {},
            "restartable_loop": {
                "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
                "resumed_from": _daemon_supervisor_previous_snapshot(previous),
                "recovered_previous": None,
                "completed_dispatch_reports": _daemon_supervisor_completed_dispatch_reports(previous),
            },
            "ticks": [],
            "last_decision": None,
            "stop_reason": _daemon_supervisor_stop_reason(
                "runtime_already_running",
                "daemon supervisor runtime is already active",
                assessment=assessment,
            ),
            "preflight": assessment,
        }
        write_daemon_supervisor_runtime_report(workspace, payload)
        return 1, payload

    recovery = (
        _daemon_supervisor_recovery_payload(previous, assessment)
        if isinstance(previous, dict) and assessment.get("recoverable")
        else None
    )
    state = _daemon_supervisor_new_state(workspace, args, previous=previous, recovery=recovery)
    _write_daemon_supervisor_runtime_state(workspace, state)
    ticks: list[dict] = []
    idle_count = 0
    stop_reason: dict | None = None
    exit_code = 0

    while True:
        tick_count = int(state.get("run_window", {}).get("tick_count", 0) or 0)
        if _daemon_supervisor_window_expired(state):
            stop_reason = _daemon_supervisor_stop_reason(
                "run_window_expired",
                "daemon supervisor run window expired before the next tick",
                tick_count=tick_count,
            )
            break
        if tick_count >= args.max_ticks:
            stop_reason = _daemon_supervisor_stop_reason(
                "max_ticks",
                f"daemon supervisor stopped after {args.max_ticks} tick(s)",
                tick_count=tick_count,
            )
            break

        _daemon_supervisor_heartbeat(
            workspace,
            state,
            activity="daemon-supervisor-dispatching",
            message=f"starting supervisor tick {tick_count + 1}",
        )
        dispatch_exit_code, dispatch_payload = workspace_drive_dispatch(args)
        if dispatch_payload.get("status") == "no_eligible_project":
            idle_count += 1
        else:
            idle_count = 0
        decision = _daemon_supervisor_tick_decision(
            dispatch_exit_code,
            dispatch_payload,
            idle_count=idle_count,
            args=args,
        )
        tick = _daemon_supervisor_tick_payload(tick_count + 1, dispatch_exit_code, dispatch_payload, decision)
        ticks.append(tick)
        _daemon_supervisor_record_tick(workspace, state, tick)

        if decision.get("action") == "stop":
            reason = str(decision.get("reason") or "operator_visible_stop")
            code = "idle_limit" if reason == "no_eligible_project" else reason
            stop_reason = _daemon_supervisor_stop_reason(
                code,
                str(decision.get("message") or "daemon supervisor stopped"),
                tick_count=int(state.get("run_window", {}).get("tick_count", 0) or 0),
                dispatch_status=dispatch_payload.get("status"),
                dispatch_exit_code=dispatch_exit_code,
            )
            if code == "dispatch_failed":
                exit_code = 1
            break

        if _daemon_supervisor_window_expired(state):
            stop_reason = _daemon_supervisor_stop_reason(
                "run_window_expired",
                "daemon supervisor run window expired after dispatch tick",
                tick_count=int(state.get("run_window", {}).get("tick_count", 0) or 0),
            )
            break
        if int(state.get("run_window", {}).get("tick_count", 0) or 0) >= args.max_ticks:
            stop_reason = _daemon_supervisor_stop_reason(
                "max_ticks",
                f"daemon supervisor stopped after {args.max_ticks} tick(s)",
                tick_count=int(state.get("run_window", {}).get("tick_count", 0) or 0),
            )
            break

        sleep_seconds = int(decision.get("sleep_seconds", 0) or 0)
        if sleep_seconds > 0:
            _daemon_supervisor_heartbeat(
                workspace,
                state,
                activity="daemon-supervisor-sleeping",
                message=f"sleeping {sleep_seconds}s before the next supervisor tick",
            )
            time.sleep(sleep_seconds)

    if stop_reason is None:
        stop_reason = _daemon_supervisor_stop_reason("completed", "daemon supervisor runtime completed")
    state = _daemon_supervisor_finalize_state(workspace, state, status="stopped", stop_reason=stop_reason)
    payload = {
        "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
        "kind": DAEMON_SUPERVISOR_RUNTIME_KIND,
        "workspace": str(workspace),
        "state_path": state.get("state_path"),
        "status": state.get("status"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "run_window": deepcopy(state.get("run_window")),
        "restartable_loop": deepcopy(state.get("restartable_loop")),
        "ticks": ticks,
        "last_tick": deepcopy(state.get("last_tick")),
        "last_decision": deepcopy(state.get("last_decision")),
        "stop_reason": stop_reason,
        "preflight": assessment,
        "command_options": deepcopy(state.get("command_options")),
        "final_state": deepcopy(state),
    }
    write_daemon_supervisor_runtime_report(workspace, payload)
    state["latest_report"] = payload.get("runtime_report")
    state["latest_report_json"] = payload.get("runtime_report_json")
    payload["final_state"] = deepcopy(state)
    _write_daemon_supervisor_runtime_state(workspace, state)
    write_json(workspace / str(payload["runtime_report_json"]), payload)
    return exit_code, payload


def cmd_daemon_supervisor(args: argparse.Namespace) -> int:
    if args.max_ticks < 1:
        raise ValueError("--max-ticks must be at least 1")
    if args.max_tasks < 1:
        raise ValueError("--max-tasks must be at least 1")
    if args.run_window_seconds < 0:
        raise ValueError("--run-window-seconds must be non-negative")
    if args.sleep_seconds < 0 or args.idle_sleep_seconds < 0:
        raise ValueError("--sleep-seconds and --idle-sleep-seconds must be non-negative")
    if args.idle_stop_count < 1:
        raise ValueError("--idle-stop-count must be at least 1")
    if args.runtime_stale_after_seconds is not None and args.runtime_stale_after_seconds < 1:
        raise ValueError("--runtime-stale-after-seconds must be at least 1")
    if args.lease_stale_after_seconds is not None and args.lease_stale_after_seconds < 1:
        raise ValueError("--lease-stale-after-seconds must be at least 1")
    if args.nonproductive_backoff_seconds is not None and args.nonproductive_backoff_seconds < 0:
        raise ValueError("--nonproductive-backoff-seconds must be non-negative")
    exit_code, payload = daemon_supervisor_runtime(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        stop_reason = payload.get("stop_reason") if isinstance(payload.get("stop_reason"), dict) else {}
        print(f"Daemon supervisor: {payload['status']} - {stop_reason.get('message')}")
        print(f"Ticks: {len(payload.get('ticks', []))}")
        print(f"Stop reason: {stop_reason.get('code')}")
        print(f"Runtime report: {payload.get('runtime_report')}")
    return exit_code


def cmd_workspace_drive(args: argparse.Namespace) -> int:
    if args.max_tasks < 1:
        raise ValueError("--max-tasks must be at least 1")
    if args.max_continuations < 0 or args.max_self_iterations < 0:
        raise ValueError("--max-continuations and --max-self-iterations must be non-negative")
    if args.lease_stale_after_seconds is not None and args.lease_stale_after_seconds < 1:
        raise ValueError("--lease-stale-after-seconds must be at least 1")
    if args.nonproductive_backoff_seconds is not None and args.nonproductive_backoff_seconds < 0:
        raise ValueError("--nonproductive-backoff-seconds must be non-negative")
    exit_code, payload = workspace_drive_dispatch(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Workspace dispatch: {payload['status']} - {payload['message']}")
        print(f"Projects scanned: {len(payload.get('queue', []))}")
        print(f"Eligible projects: {payload.get('eligible_count', 0)}")
        selected = payload.get("selected") or {}
        print(f"Selected project: {selected.get('project') or 'none'}")
        print(f"Dispatch report: {payload['dispatch_report']}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Engineering Orchestrator: roadmap-driven engineering control plane")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List built-in project profiles")
    profiles.add_argument("--json", action="store_true")
    profiles.set_defaults(func=cmd_profiles)

    scan = subparsers.add_parser("scan", help="Discover projects in a workspace")
    scan.add_argument("--workspace", type=Path, default=Path.cwd())
    scan.add_argument("--max-depth", type=int, default=3)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    init = subparsers.add_parser("init", help="Initialize .engineering in a project")
    init.add_argument("--project-root", type=Path, required=True)
    init.add_argument("--profile", required=True)
    init.add_argument("--name", default=None)
    init.add_argument("--force", action="store_true")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_init)

    plan_goal = subparsers.add_parser("plan-goal", help="Propose or materialize a starter roadmap from a high-level goal")
    plan_goal.add_argument("--project-root", type=Path, required=True)
    plan_goal.add_argument("--name", default=None)
    plan_goal.add_argument("--profile", required=True)
    goal_source = plan_goal.add_mutually_exclusive_group(required=True)
    goal_source.add_argument("--goal", default=None)
    goal_source.add_argument("--goal-file", type=Path, default=None)
    plan_goal.add_argument("--blueprint", default=None)
    plan_goal.add_argument("--constraint", action="append", default=None)
    plan_goal.add_argument("--experience-kind", default=None)
    plan_goal.add_argument("--stage-count", type=int, default=DEFAULT_GOAL_STAGE_COUNT)
    plan_goal.add_argument("--materialize", action="store_true")
    plan_goal.add_argument("--force", action="store_true")
    plan_goal.add_argument("--json", action="store_true")
    plan_goal.set_defaults(func=cmd_plan_goal)

    workspace_drive = subparsers.add_parser(
        "workspace-drive",
        help="Dispatch at most one eligible project drive from a workspace queue",
    )
    workspace_drive.add_argument("--workspace", type=Path, default=Path.cwd())
    workspace_drive.add_argument("--max-depth", type=int, default=3)
    workspace_drive.add_argument("--max-tasks", type=int, default=1)
    workspace_drive.add_argument("--time-budget-seconds", type=int, default=0)
    workspace_drive.add_argument("--rolling", action="store_true")
    workspace_drive.add_argument("--self-iterate", action="store_true")
    workspace_drive.add_argument("--max-continuations", type=int, default=1)
    workspace_drive.add_argument("--max-self-iterations", type=int, default=1)
    workspace_drive.add_argument("--continuation-batch-size", type=int, default=1)
    workspace_drive.add_argument("--no-progress-limit", type=int, default=2)
    workspace_drive.add_argument("--lease-stale-after-seconds", type=int, default=None)
    workspace_drive.add_argument("--nonproductive-backoff-seconds", type=int, default=None)
    workspace_drive.add_argument(
        "--scheduler-policy",
        choices=WORKSPACE_DISPATCH_SCHEDULER_POLICIES,
        default=WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY,
        help="Workspace project ordering policy: fair priority scoring or legacy path-order",
    )
    workspace_drive.add_argument("--allow-live", action="store_true")
    workspace_drive.add_argument("--allow-manual", action="store_true")
    workspace_drive.add_argument("--allow-agent", action="store_true")
    workspace_drive.add_argument("--json", action="store_true")
    workspace_drive.set_defaults(func=cmd_workspace_drive)

    daemon_supervisor = subparsers.add_parser(
        "daemon-supervisor",
        help="Run a durable local supervisor loop over workspace-drive ticks",
    )
    daemon_supervisor.add_argument("--workspace", type=Path, default=Path.cwd())
    daemon_supervisor.add_argument("--max-depth", type=int, default=3)
    daemon_supervisor.add_argument("--max-ticks", type=int, default=1)
    daemon_supervisor.add_argument("--run-window-seconds", type=int, default=0)
    daemon_supervisor.add_argument("--sleep-seconds", type=int, default=0)
    daemon_supervisor.add_argument("--idle-sleep-seconds", type=int, default=300)
    daemon_supervisor.add_argument("--idle-stop-count", type=int, default=1)
    daemon_supervisor.add_argument("--runtime-stale-after-seconds", type=int, default=None)
    daemon_supervisor.add_argument("--max-tasks", type=int, default=1)
    daemon_supervisor.add_argument("--time-budget-seconds", type=int, default=0)
    daemon_supervisor.add_argument("--rolling", action="store_true")
    daemon_supervisor.add_argument("--self-iterate", action="store_true")
    daemon_supervisor.add_argument("--max-continuations", type=int, default=1)
    daemon_supervisor.add_argument("--max-self-iterations", type=int, default=1)
    daemon_supervisor.add_argument("--continuation-batch-size", type=int, default=1)
    daemon_supervisor.add_argument("--no-progress-limit", type=int, default=2)
    daemon_supervisor.add_argument("--lease-stale-after-seconds", type=int, default=None)
    daemon_supervisor.add_argument("--nonproductive-backoff-seconds", type=int, default=None)
    daemon_supervisor.add_argument(
        "--scheduler-policy",
        choices=WORKSPACE_DISPATCH_SCHEDULER_POLICIES,
        default=WORKSPACE_DISPATCH_DEFAULT_SCHEDULER_POLICY,
        help="Workspace project ordering policy used by each supervisor dispatch tick",
    )
    daemon_supervisor.add_argument("--allow-live", action="store_true")
    daemon_supervisor.add_argument("--allow-manual", action="store_true")
    daemon_supervisor.add_argument("--allow-agent", action="store_true")
    daemon_supervisor.add_argument("--json", action="store_true")
    daemon_supervisor.set_defaults(func=cmd_daemon_supervisor)

    spec_sync = subparsers.add_parser("spec-sync", help="Audit or update a project's dynamic spec task ledger")
    spec_sync_subparsers = spec_sync.add_subparsers(dest="spec_sync_action", required=True)

    spec_sync_audit = spec_sync_subparsers.add_parser("audit", help="Audit the dynamic spec maintenance files")
    spec_sync_audit.add_argument("--project-root", type=Path, default=None)
    spec_sync_audit.add_argument("--workspace", type=Path, default=Path.cwd())
    spec_sync_audit.add_argument("--project", default=None)
    spec_sync_audit.add_argument("--max-depth", type=int, default=3)
    spec_sync_audit.add_argument("--json", action="store_true")
    spec_sync_audit.set_defaults(func=cmd_spec_sync)

    spec_sync_record = spec_sync_subparsers.add_parser("record", help="Record a task or stage status update")
    spec_sync_record.add_argument("--project-root", type=Path, default=None)
    spec_sync_record.add_argument("--workspace", type=Path, default=Path.cwd())
    spec_sync_record.add_argument("--project", default=None)
    spec_sync_record.add_argument("--max-depth", type=int, default=3)
    spec_sync_record.add_argument("--task-id", required=True)
    spec_sync_record.add_argument("--status", required=True)
    spec_sync_record.add_argument("--evidence", action="append", default=[])
    spec_sync_record.add_argument("--requirement-id", action="append", default=[])
    spec_sync_record.add_argument("--note", default="")
    spec_sync_record.add_argument("--actor", default="engineering-harness")
    spec_sync_record.add_argument("--phase", default="")
    spec_sync_record.add_argument("--stage-id", default="")
    spec_sync_record.add_argument("--apply", action="store_true")
    spec_sync_record.add_argument("--json", action="store_true")
    spec_sync_record.set_defaults(func=cmd_spec_sync)

    docs_sync = subparsers.add_parser("docs-sync", help="Audit, propose, or record target documentation synchronization")
    docs_sync_subparsers = docs_sync.add_subparsers(dest="docs_sync_action", required=True)

    docs_sync_audit = docs_sync_subparsers.add_parser("audit", help="Audit target documentation role coverage")
    docs_sync_audit.add_argument("--project-root", type=Path, default=None)
    docs_sync_audit.add_argument("--workspace", type=Path, default=Path.cwd())
    docs_sync_audit.add_argument("--project", default=None)
    docs_sync_audit.add_argument("--max-depth", type=int, default=3)
    docs_sync_audit.add_argument("--json", action="store_true")
    docs_sync_audit.set_defaults(func=cmd_docs_sync)

    docs_sync_propose = docs_sync_subparsers.add_parser("propose", help="Propose bounded documentation updates")
    docs_sync_propose.add_argument("--project-root", type=Path, default=None)
    docs_sync_propose.add_argument("--workspace", type=Path, default=Path.cwd())
    docs_sync_propose.add_argument("--project", default=None)
    docs_sync_propose.add_argument("--max-depth", type=int, default=3)
    docs_sync_propose.add_argument("--task-id", required=True)
    docs_sync_propose.add_argument("--status", default="completed")
    docs_sync_propose.add_argument("--evidence", action="append", default=[])
    docs_sync_propose.add_argument("--requirement-id", action="append", default=[])
    docs_sync_propose.add_argument("--note", default="")
    docs_sync_propose.add_argument("--actor", default="engineering-harness")
    docs_sync_propose.add_argument("--phase", default="")
    docs_sync_propose.add_argument("--stage-id", default="")
    docs_sync_propose.add_argument("--architecture-change", action="store_true")
    docs_sync_propose.add_argument("--json", action="store_true")
    docs_sync_propose.set_defaults(func=cmd_docs_sync)

    docs_sync_record = docs_sync_subparsers.add_parser("record", help="Record safe documentation updates")
    docs_sync_record.add_argument("--project-root", type=Path, default=None)
    docs_sync_record.add_argument("--workspace", type=Path, default=Path.cwd())
    docs_sync_record.add_argument("--project", default=None)
    docs_sync_record.add_argument("--max-depth", type=int, default=3)
    docs_sync_record.add_argument("--task-id", required=True)
    docs_sync_record.add_argument("--status", default="completed")
    docs_sync_record.add_argument("--evidence", action="append", default=[])
    docs_sync_record.add_argument("--requirement-id", action="append", default=[])
    docs_sync_record.add_argument("--note", default="")
    docs_sync_record.add_argument("--actor", default="engineering-harness")
    docs_sync_record.add_argument("--phase", default="")
    docs_sync_record.add_argument("--stage-id", default="")
    docs_sync_record.add_argument("--architecture-change", action="store_true")
    docs_sync_record.add_argument("--apply", action="store_true")
    docs_sync_record.add_argument("--json", action="store_true")
    docs_sync_record.set_defaults(func=cmd_docs_sync)

    merge_plan = subparsers.add_parser(
        "merge-plan",
        help="Plan a non-destructive merge order for parallel worktrees or task branches",
    )
    merge_plan.add_argument("--project-root", type=Path, default=None)
    merge_plan.add_argument("--workspace", type=Path, default=Path.cwd())
    merge_plan.add_argument("--project", default=None)
    merge_plan.add_argument("--max-depth", type=int, default=3)
    merge_plan.add_argument("--base", default=None, help="Base branch or ref to plan merges into; defaults to current branch")
    merge_plan.add_argument("--branch", action="append", default=None, help="Task branch to include in the merge planner")
    merge_plan.add_argument("--worktree", type=Path, action="append", default=None, help="Git worktree path to include")
    merge_plan.add_argument("--task", action="append", default=None, help="Roadmap task id whose acceptance commands should run after merge")
    merge_plan.add_argument(
        "--post-merge-acceptance",
        action="append",
        default=None,
        help="Additional post-merge acceptance command to include in the report",
    )
    merge_plan.add_argument("--write", action="store_true", help="Write Markdown and JSON merge planner reports")
    merge_plan.add_argument("--json", action="store_true")
    merge_plan.set_defaults(func=cmd_merge_plan)

    parallel_drive = subparsers.add_parser(
        "parallel-drive",
        help="Run a bounded native parallel development drive with isolated worktrees",
    )
    parallel_drive.add_argument("--project-root", type=Path, default=None)
    parallel_drive.add_argument("--workspace", type=Path, default=Path.cwd())
    parallel_drive.add_argument("--project", default=None)
    parallel_drive.add_argument("--max-depth", type=int, default=3)
    parallel_drive.add_argument("--base", default=None, help="Base branch or ref to merge successful task branches into")
    parallel_drive.add_argument("--max-workers", type=int, default=2)
    parallel_drive.add_argument("--max-tasks", type=int, default=1)
    parallel_drive.add_argument("--time-budget-seconds", type=int, default=0)
    parallel_drive.add_argument("--poll-interval-seconds", type=float, default=0.2)
    parallel_drive.add_argument("--allow-dirty-worktree", action="store_true")
    parallel_drive.add_argument("--allow-live", action="store_true")
    parallel_drive.add_argument("--allow-manual", action="store_true")
    parallel_drive.add_argument("--allow-agent", action="store_true")
    parallel_drive.add_argument("--resume", action="store_true")
    parallel_drive.add_argument("--plan-only", action="store_true")
    parallel_drive.add_argument("--json", action="store_true")
    parallel_drive.set_defaults(func=cmd_parallel_drive)

    for name, help_text, func in [
        ("status", "Show project or workspace status", cmd_status),
        ("operator-console", "Generate a bounded local operator console summary", cmd_operator_console),
        ("validate", "Validate the engineering roadmap schema and task commands", cmd_validate),
        ("next", "Show the next selected task", cmd_next),
        ("run", "Run the next or selected task acceptance checks", cmd_run),
        ("advance", "Materialize the next continuation milestone into the roadmap", cmd_advance),
        ("frontend-tasks", "Propose or materialize frontend roadmap tasks from the experience plan", cmd_frontend_tasks),
        ("plan-spec", "Propose or materialize continuation stages from a specification document", cmd_spec_backlog),
        ("spec-backlog", "Propose or materialize continuation stages from specification task lists", cmd_spec_backlog),
        ("self-iterate", "Assess current state and append the next continuation stage", cmd_self_iterate),
        ("pause", "Pause future drive scheduling for this project", cmd_pause),
        ("resume", "Clear pause or cancel state so a drive can continue", cmd_resume),
        ("cancel", "Cancel future drive scheduling for this project until resumed", cmd_cancel),
        ("approvals", "List pending or historical approval gates", cmd_approvals),
        ("approve", "Approve one or all pending approval gates", cmd_approve),
        ("drive", "Continuously run pending roadmap tasks until complete, blocked, failed, or out of budget", cmd_drive),
    ]:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--project", default=None)
        command.add_argument("--max-depth", type=int, default=3)
        command.add_argument("--json", action="store_true")
        if name == "run":
            command.add_argument("--task", default=None)
            command.add_argument("--max-tasks", type=int, default=1)
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--allow-manual", action="store_true")
            command.add_argument("--allow-agent", action="store_true")
            command.add_argument("--commit-after-task", action="store_true")
            command.add_argument("--push-after-task", action="store_true")
            command.add_argument("--git-remote", default="origin")
            command.add_argument("--git-branch", default=None)
            command.add_argument("--git-message-template", default="chore(engineering): complete {task_id}")
        if name == "operator-console":
            command.add_argument("--write", action="store_true")
        if name == "advance":
            command.add_argument("--max-new-milestones", type=int, default=1)
            command.add_argument("--reason", default="manual_advance")
        if name == "frontend-tasks":
            command.add_argument("--materialize", action="store_true")
            command.add_argument("--milestone-id", default="frontend-visualization")
            command.add_argument("--reason", default="manual_frontend_task_generation")
        if name in {"plan-spec", "spec-backlog"}:
            command.add_argument("--spec", type=Path, action="append", default=None)
            command.add_argument("--source", type=Path, action="append", default=None)
            command.add_argument("--include-blueprint", action="store_true")
            command.add_argument("--from-stage", type=int, default=1)
            command.add_argument("--materialize", action="store_true")
            command.add_argument("--reason", default="manual_spec_backlog_materialization")
        if name == "self-iterate":
            command.add_argument("--reason", default="manual_self_iteration")
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--allow-agent", action="store_true")
        if name in {"pause", "resume", "cancel"}:
            command.add_argument("--reason", default=f"manual_{name}")
        if name == "approvals":
            command.add_argument("--status", default="pending")
            command.add_argument("--all", action="store_true")
        if name == "approve":
            command.add_argument("approval_id", nargs="?")
            command.add_argument("--all", action="store_true")
            command.add_argument("--approved-by", default="local")
            command.add_argument("--reason", default="manual approval")
        if name == "drive":
            command.add_argument("--max-tasks", type=int, default=100)
            command.add_argument("--time-budget-seconds", type=int, default=0)
            command.add_argument("--rolling", action="store_true")
            command.add_argument("--self-iterate", action="store_true")
            command.add_argument("--max-continuations", type=int, default=20)
            command.add_argument("--max-self-iterations", type=int, default=20)
            command.add_argument("--continuation-batch-size", type=int, default=1)
            command.add_argument("--no-progress-limit", type=int, default=2)
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--allow-manual", action="store_true")
            command.add_argument("--allow-agent", action="store_true")
            command.add_argument("--stop-after-each", action="store_true")
            command.add_argument("--commit-after-task", action="store_true")
            command.add_argument("--push-after-task", action="store_true")
            command.add_argument("--git-remote", default="origin")
            command.add_argument("--git-branch", default=None)
            command.add_argument("--git-message-template", default="chore(engineering): complete {task_id}")
        command.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

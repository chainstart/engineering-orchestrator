from __future__ import annotations

import json
import importlib
import os
import subprocess
import time
import tomllib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from engineering_orchestrator.browser_e2e import browser_user_experience_command
from engineering_orchestrator.goal_intake import GoalIntakeValidationError, normalize_goal_intake, validate_goal_intake
from engineering_orchestrator.goal_planner import plan_goal_roadmap
from engineering_orchestrator.core import Harness, discover_projects, init_project, redact_evidence, utc_now
from engineering_orchestrator.parallel_drive import (
    _checkpoint_worker_branch,
    load_parallel_drive_state,
    plan_parallel_drive,
)
from engineering_orchestrator.domain_frontend import (
    DOMAIN_FRONTEND_DECISION_KIND,
    DOMAIN_FRONTEND_GENERATOR_ID,
    build_domain_frontend_plan,
)
from engineering_orchestrator.executors import (
    CodexExecutorAdapter,
    DAGGER_ENABLE_ENV,
    OPENHANDS_BINARY_ENV,
    OPENHANDS_ENABLE_ENV,
    TASK_AGENT_DEVELOPER_BINARY_ENV,
    DaggerExecutorAdapter,
    ExecutorInvocation,
    ExecutorMetadata,
    ExecutorRegistry,
    ExecutorResult,
    OpenHandsExecutorAdapter,
    ShellExecutorAdapter,
    default_executor_registry,
)
from engineering_orchestrator.cli import build_parser, main as cli_main
from engineering_orchestrator.policy_compat import (
    evaluate_opa_policy_input,
    export_policy_input_for_opa,
    serialize_policy_input_for_opa,
)
from engineering_orchestrator.profiles import list_profiles
from engineering_orchestrator.supervisor_decision import SUPERVISOR_DECISION_KIND


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROADMAP_FIXTURES = Path(__file__).parent / "fixtures" / "roadmaps"


def validate_roadmap_fixture(tmp_path: Path, fixture_name: str) -> dict:
    project = tmp_path / Path(fixture_name).stem
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    fixture_path = ROADMAP_FIXTURES / fixture_name
    roadmap_path.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")

    return Harness(project).validate_roadmap()


def validate_roadmap_payload(tmp_path: Path, roadmap: dict) -> dict:
    project = tmp_path / "payload-project"
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    return Harness(project).validate_roadmap()


def status_summary_for_roadmap(tmp_path: Path, project_name: str, roadmap: dict) -> dict:
    project = tmp_path / project_name
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    return Harness(project).status_summary()


def test_next_task_treats_completed_roadmap_status_as_terminal(tmp_path: Path):
    project = tmp_path / "completed-roadmap-status"
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap = {
        "version": 1,
        "project": "completed-roadmap-status",
        "profile": "python-agent",
        "milestones": [
            {
                "id": "baseline",
                "status": "active",
                "tasks": [
                    {
                        "id": "already-complete",
                        "title": "Already complete",
                        "status": "completed",
                        "file_scope": ["**"],
                        "acceptance": [{"name": "complete", "command": "python3 -c \"print('complete')\""}],
                    },
                    {
                        "id": "next-work",
                        "title": "Next work",
                        "status": "pending",
                        "file_scope": ["**"],
                        "acceptance": [{"name": "pending", "command": "python3 -c \"print('pending')\""}],
                    },
                ],
            }
        ],
    }
    (engineering_dir / "roadmap.yaml").write_text(json.dumps(roadmap), encoding="utf-8")

    task = Harness(project).next_task()

    assert task is not None
    assert task.id == "next-work"


def roadmap_fixture_payload(fixture_name: str) -> dict:
    fixture_path = ROADMAP_FIXTURES / fixture_name
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def continuation_tasks(roadmap: dict) -> list[dict]:
    return [
        task
        for stage in roadmap.get("continuation", {}).get("stages", [])
        for task in stage.get("tasks", [])
    ]


def task_manifest(project: Path, result: dict) -> dict:
    return json.loads((project / result["manifest"]).read_text(encoding="utf-8"))


def harness_state(project: Path) -> dict:
    return json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))


def write_harness_state(project: Path, state: dict) -> None:
    (project / ".engineering/state/harness-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def unused_pid() -> int:
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pid += 1
            continue
        pid += 1


def init_git_repo(project: Path, message: str = "initial") -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project, check=True, capture_output=True, text=True)


def test_parallel_worker_checkpoint_rejects_noop_branch(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "README.md").write_text("base\n", encoding="utf-8")
    init_git_repo(project)
    subprocess.run(["git", "branch", "worker/noop"], cwd=project, check=True)
    worktree = tmp_path / "noop-worker"
    subprocess.run(["git", "worktree", "add", str(worktree), "worker/noop"], cwd=project, check=True)

    checkpoint = _checkpoint_worker_branch(
        project,
        {"worktree_path": str(worktree), "branch": "worker/noop", "task_id": "noop-task"},
    )

    assert checkpoint["status"] == "failed"
    assert checkpoint["ahead_count"] == 0
    assert "no commits ahead" in checkpoint["message"]


def test_parallel_worker_checkpoint_commits_dirty_worktree(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "README.md").write_text("base\n", encoding="utf-8")
    init_git_repo(project)
    subprocess.run(["git", "branch", "worker/dirty"], cwd=project, check=True)
    worktree = tmp_path / "dirty-worker"
    subprocess.run(["git", "worktree", "add", str(worktree), "worker/dirty"], cwd=project, check=True)
    (worktree / "feature.txt").write_text("worker output\n", encoding="utf-8")

    checkpoint = _checkpoint_worker_branch(
        project,
        {"worktree_path": str(worktree), "branch": "worker/dirty", "task_id": "dirty-task"},
    )

    assert checkpoint["status"] == "checkpointed"
    assert checkpoint["ahead_count"] == 1
    assert subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def init_workspace_project(workspace: Path, dirname: str, *, name: str | None = None, marker: str | None = None) -> Path:
    project = workspace / dirname
    project.mkdir()
    init_project(project, "python-agent", name=name or dirname)
    if marker:
        roadmap_path = project / ".engineering/roadmap.yaml"
        roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
        roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
            f"python3 -c \"from pathlib import Path; Path('{marker}').write_text('ok')\""
        )
        roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    return project


def workspace_backoff_stage(stage_id: str, task_id: str) -> dict:
    return {
        "id": stage_id,
        "title": f"{stage_id} Stage",
        "objective": "Create a local workspace backoff marker task.",
        "tasks": [
            {
                "id": task_id,
                "title": f"{task_id} Task",
                "file_scope": ["**"],
                "acceptance": [
                    {
                        "name": "local marker",
                        "command": f"python3 -c \"from pathlib import Path; Path('{task_id}.txt').write_text('ok')\"",
                    }
                ],
            }
        ],
    }


def workspace_backoff_planner_source(stage: dict) -> str:
    return (
        "import json\n"
        "from pathlib import Path\n\n"
        "roadmap_path = Path('.engineering/roadmap.yaml')\n"
        "roadmap = json.loads(roadmap_path.read_text(encoding='utf-8'))\n"
        "continuation = roadmap.setdefault('continuation', {'enabled': True, 'stages': []})\n"
        "continuation['enabled'] = True\n"
        f"continuation.setdefault('stages', []).append({json.dumps(stage)})\n"
        "roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding='utf-8')\n"
    )


def configure_workspace_self_iteration_project(project: Path, planner_source: str) -> None:
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next local workspace backoff stage.",
        "max_stages_per_iteration": 1,
        "planner": {"name": "workspace backoff planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "planner.py").write_text(planner_source, encoding="utf-8")


def configure_workspace_continuation_project(project: Path, stages: list[dict]) -> None:
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Materialize local workspace backoff stages.",
        "stages": stages,
    }
    roadmap.pop("self_iteration", None)
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")


def seed_local_full_lifecycle_smoke_task(project: Path) -> None:
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task_id = "local-full-lifecycle-artifact"
    artifact_path = "artifacts/local-full-lifecycle/implementation.txt"
    e2e_path = "artifacts/local-full-lifecycle/e2e-evidence.json"
    roadmap["milestones"] = [
        {
            "id": "local-full-lifecycle-smoke",
            "title": "Local Full Lifecycle Smoke",
            "status": "active",
            "objective": "Complete one safe unattended local task and capture deterministic evidence.",
            "tasks": [
                {
                    "id": task_id,
                    "title": "Write local lifecycle artifact",
                    "status": "pending",
                    "max_attempts": 1,
                    "agent_approval_required": False,
                    "file_scope": ["artifacts/**"],
                    "implementation": [
                        {
                            "name": "write local artifact",
                            "command": (
                                "python3 -c \"from pathlib import Path; "
                                f"p=Path('{artifact_path}'); "
                                "p.parent.mkdir(parents=True, exist_ok=True); "
                                "p.write_text('implemented\\n', encoding='utf-8'); "
                                "print('implemented')\""
                            ),
                            "timeout_seconds": 30,
                        }
                    ],
                    "acceptance": [
                        {
                            "name": "artifact is readable",
                            "command": (
                                "python3 -c \"from pathlib import Path; "
                                f"assert Path('{artifact_path}').read_text(encoding='utf-8') == 'implemented\\n'; "
                                "print('accepted')\""
                            ),
                            "timeout_seconds": 30,
                        }
                    ],
                    "e2e": [
                        {
                            "name": "local lifecycle e2e evidence",
                            "command": (
                                "python3 -c \"from pathlib import Path; import json; "
                                f"artifact=Path('{artifact_path}'); "
                                "assert artifact.read_text(encoding='utf-8') == 'implemented\\n'; "
                                f"evidence=Path('{e2e_path}'); "
                                "evidence.write_text("
                                "json.dumps({'status':'passed','artifact':str(artifact)}, sort_keys=True) + '\\n', "
                                "encoding='utf-8'); "
                                "print('e2e passed')\""
                            ),
                            "timeout_seconds": 30,
                        }
                    ],
                }
            ],
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap, indent=2, sort_keys=True), encoding="utf-8")


def workspace_dispatch_lease_dir(workspace: Path) -> Path:
    return workspace / ".engineering/state/workspace-dispatch-lease"


def workspace_dispatch_lease_path(workspace: Path) -> Path:
    return workspace_dispatch_lease_dir(workspace) / "lease.json"


def write_workspace_dispatch_lease(
    workspace: Path,
    *,
    owner_pid: int,
    heartbeat_at: str | None = None,
    stale_after_seconds: int = 3600,
) -> dict:
    now = utc_now()
    payload = {
        "schema_version": 1,
        "kind": "engineering-harness.workspace-dispatch-lease",
        "status": "running",
        "workspace": str(workspace.resolve()),
        "owner_pid": owner_pid,
        "started_at": heartbeat_at or now,
        "last_heartbeat_at": heartbeat_at or now,
        "heartbeat_count": 1,
        "selected_project": None,
        "command_options": {
            "workspace": str(workspace.resolve()),
            "max_depth": 3,
            "max_tasks": 1,
            "allow_live": False,
            "allow_manual": False,
            "allow_agent": False,
            "push_after_task": False,
            "commit_after_task": False,
            "lease_stale_after_seconds": stale_after_seconds,
        },
        "stale_after_seconds": stale_after_seconds,
        "current_activity": "test",
    }
    lease_dir = workspace_dispatch_lease_dir(workspace)
    lease_dir.mkdir(parents=True, exist_ok=True)
    workspace_dispatch_lease_path(workspace).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def run_workspace_drive_json(capsys, workspace: Path, *extra_args: str) -> tuple[int, dict]:
    capsys.readouterr()
    exit_code = cli_main(["workspace-drive", "--workspace", str(workspace), *extra_args, "--json"])
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


def run_parallel_drive_json(capsys, project: Path, *extra_args: str) -> tuple[int, dict]:
    capsys.readouterr()
    exit_code = cli_main(["parallel-drive", "--project-root", str(project), *extra_args, "--json"])
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


def daemon_supervisor_state_path(workspace: Path) -> Path:
    return workspace / ".engineering/state/daemon-supervisor-runtime.json"


def daemon_supervisor_state(workspace: Path) -> dict:
    return json.loads(daemon_supervisor_state_path(workspace).read_text(encoding="utf-8"))


def write_daemon_supervisor_state(workspace: Path, state: dict) -> None:
    daemon_supervisor_state_path(workspace).write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_daemon_supervisor_json(capsys, workspace: Path, *extra_args: str) -> tuple[int, dict]:
    capsys.readouterr()
    exit_code = cli_main(["daemon-supervisor", "--workspace", str(workspace), *extra_args, "--json"])
    payload = json.loads(capsys.readouterr().out)
    return exit_code, payload


def project_text_snapshot(project: Path) -> dict[str, str]:
    return {
        str(path.relative_to(project)): path.read_text(encoding="utf-8")
        for path in sorted(project.rglob("*"))
        if path.is_file()
    }


def report_policy_evidence(project: Path, result: dict) -> dict:
    report = (project / result["report"]).read_text(encoding="utf-8")
    section = report.split("## Policy Decisions", 1)[1]
    block = section.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def drive_report_goal_gap_retrospective(project: Path, report_path: str) -> dict:
    report = (project / report_path).read_text(encoding="utf-8")
    section = report.split("## Goal-Gap Retrospective", 1)[1]
    block = section.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def scorecard_category_map(scorecard: dict) -> dict[str, dict]:
    return {category["id"]: category for category in scorecard["categories"]}


def policy_decision(manifest: dict, kind: str, **matches) -> dict:
    for decision in manifest["policy_decisions"]:
        if decision.get("kind") != kind:
            continue
        if all(decision.get(key) == value for key, value in matches.items()):
            return decision
    raise AssertionError(f"missing policy decision {kind} matching {matches}")


def roadmap_without_experience(project_name: str, *, profile: str = "python-agent", task_title: str) -> dict:
    return {
        "version": 1,
        "project": project_name,
        "profile": profile,
        "milestones": [
            {
                "id": "baseline",
                "title": "Baseline",
                "objective": task_title,
                "tasks": [
                    {
                        "id": "baseline-task",
                        "title": task_title,
                        "status": "pending",
                        "acceptance": [
                            {
                                "name": "baseline validates",
                                "command": "python3 -c \"print('ok')\"",
                                "timeout_seconds": 30,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def write_supervisor_context_project(project: Path, *, task_count: int = 15) -> None:
    init_project(project, "python-agent", name=project.name)
    docs_dir = project / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "spec.md").write_text(
        "# Local Spec\n\n## EH-SPEC-018: Supervisor Codex Role\n\nBuild supervisor context locally.\n",
        encoding="utf-8",
    )
    (project / "README.md").write_text("initial\n", encoding="utf-8")
    tasks = []
    for index in range(task_count):
        tasks.append(
            {
                "id": f"task-{index:02d}",
                "title": f"Supervisor context task {index:02d}",
                "status": "pending",
                "file_scope": ["src/**", "tests/**"],
                "spec_refs": ["EH-SPEC-018"],
                "acceptance": [
                    {
                        "name": "supervisor context verifies",
                        "command": "python3 -c \"print('supervisor context')\"",
                        "spec_refs": ["EH-SPEC-018"],
                    }
                ],
            }
        )
    roadmap = {
        "version": 1,
        "project": project.name,
        "profile": "python-agent",
        "report_dir": ".engineering/reports/tasks",
        "manifest_index_path": ".engineering/reports/tasks/manifest-index.json",
        "goal": {"text": "Drain local supervisor gate work."},
        "spec": {"path": "docs/spec.md", "kind": "markdown"},
        "milestones": [
            {
                "id": "supervisor-context",
                "title": "Supervisor Context",
                "objective": "Build bounded supervisor context packs.",
                "tasks": tasks,
            }
        ],
    }
    (project / ".engineering/roadmap.yaml").write_text(json.dumps(roadmap, indent=2), encoding="utf-8")


def write_supervisor_context_manifest(
    project: Path,
    *,
    name: str,
    task_id: str,
    status: str = "failed",
) -> None:
    report_dir = project / ".engineering/reports/tasks"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{name}.md"
    manifest_path = report_dir / f"{name}.json"
    report_path.write_text(
        "# Task Report\n\nGate failed with OPENAI_API_KEY=sk-supersecret123456 and docs_sync evidence.\n",
        encoding="utf-8",
    )
    manifest = {
        "kind": "engineering-harness.task-run-manifest",
        "project": project.name,
        "task_id": task_id,
        "manifest_path": str(manifest_path.relative_to(project)),
        "report_path": str(report_path.relative_to(project)),
        "status": status,
        "message": "failed gate with TOKEN=secret-token-value",
        "started_at": "2026-06-02T00:00:00Z",
        "finished_at": "2026-06-02T00:01:00Z",
        "runs": [
            {
                "phase": "acceptance",
                "name": "supervisor context verifies",
                "executor": "shell",
                "status": status,
                "returncode": 1 if status == "failed" else 0,
                "spec_refs": ["EH-SPEC-018"],
                "requested_capabilities": [],
            }
        ],
        "spec_sync": {"status": "applied", "reason": "spec_sync evidence recorded"},
        "docs_sync": {
            "status": "applied",
            "reason": "docs_sync evidence recorded",
            "docs_update_log": "docs/docs_update_log.jsonl",
        },
        "policy_decision_summary": {"total": 0},
        "safety_audit": {"unsafe_decision_count": 0},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def test_supervisor_context_pack_writes_bounded_local_context_artifact(tmp_path: Path):
    project = tmp_path / "supervisor-context-project"
    project.mkdir()
    write_supervisor_context_project(project)
    write_supervisor_context_manifest(project, name="20260602T000000Z-task-00", task_id="task-00")
    write_supervisor_context_manifest(project, name="20260602T000100Z-task-01", task_id="task-01", status="completed")
    (project / "docs/docs_update_log.jsonl").write_text(
        json.dumps({"kind": "docs_sync", "note": "docs_sync completed with API_KEY=docs-secret"}) + "\n",
        encoding="utf-8",
    )
    (project / "docs/spec_update_log.jsonl").write_text(
        json.dumps({"kind": "spec_sync", "note": "spec_sync completed with TOKEN=spec-secret"}) + "\n",
        encoding="utf-8",
    )
    init_git_repo(project)
    (project / "README.md").write_text("initial\nsupervisor context dirty diff\n", encoding="utf-8")

    summary = Harness(project).write_supervisor_context_pack(
        gate_reason="task_failure: OPENAI_API_KEY=sk-gatesecret123456",
        objective="Review the current supervisor context gate.",
        risk_metadata={"api_key": "risk-secret-value", "severity": "high"},
    )
    pack_path = project / summary["path"]
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    serialized = json.dumps(pack, sort_keys=True)

    assert pack["kind"] == "engineering-harness.supervisor-context-pack"
    assert pack["local_only"] is True
    assert pack["source_scope"] == "local-only"
    assert pack["artifact"]["path"] == summary["path"]
    assert len(pack["pending_tasks"]) == pack["limits"]["pending_task_count"]
    assert pack["roadmap"]["pending_tasks_truncated"] is True
    assert pack["docs_sync"]["manifest_evidence_count"] == 2
    assert pack["spec_sync"]["manifest_evidence_count"] == 2
    assert pack["docs_sync"]["log"]["included_count"] == 1
    assert pack["spec_sync"]["log"]["included_count"] == 1
    assert pack["git"]["is_repository"] is True
    assert pack["git"]["diff_summary"]["file_count"] >= 1
    assert pack["risk_metadata"]["provided"]["api_key"] == "[REDACTED]"
    assert "[REDACTED]" in serialized
    assert "sk-gatesecret123456" not in serialized
    assert "sk-supersecret123456" not in serialized
    assert "risk-secret-value" not in serialized


def test_supervisor_context_cli_generates_context_pack(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-context-cli"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    write_supervisor_context_manifest(project, name="20260602T000000Z-task-00", task_id="task-00")

    exit_code = cli_main(
        [
            "supervisor-context",
            "--project-root",
            str(project),
            "--gate-reason",
            "quality_gate",
            "--objective",
            "Inspect supervisor context from local evidence.",
            "--risk-metadata",
            "severity=medium",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    pack = json.loads((project / payload["path"]).read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["kind"] == "engineering-harness.supervisor-context-pack"
    assert len(payload["sha256"]) == 64
    assert pack["gate_reason"] == "quality_gate"
    assert pack["summary"]["local_only"] is True
    assert pack["sync_evidence"]["docs_sync"]["manifest_evidence_count"] == 1


def test_supervisor_decision_validator_accepts_continue_and_persists_local_evidence(tmp_path: Path):
    project = tmp_path / "supervisor-decision-continue"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    harness = Harness(project)
    context = harness.write_supervisor_context_pack(
        gate_reason="quality_gate_complete",
        objective="Decide whether the next supervisor task can continue.",
    )
    decision = {
        "kind": SUPERVISOR_DECISION_KIND,
        "decision": "continue",
        "approved_next_tasks": ["task-00"],
        "blocked_tasks": [],
        "tasks_to_rewrite": [],
        "requires_human": False,
        "reason": "Local supervisor context evidence supports continuing to the next queued task.",
        "evidence": [context["path"]],
    }

    validation = harness.validate_supervisor_decision(decision, context_pack_path=context["path"])
    result = harness.write_supervisor_decision_evidence(
        decision,
        context_pack_path=context["path"],
        gate_reason="quality_gate_complete",
    )
    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    report = (project / result["report"]).read_text(encoding="utf-8")

    assert validation["accepted"] is True
    assert validation["normalized_decision"]["safety_classification"]["auto_apply_allowed"] is True
    assert result["accepted"] is True
    assert manifest["kind"] == "engineering-orchestrator.supervisor-decision-validation.v1"
    assert manifest["decision_kind"] == SUPERVISOR_DECISION_KIND
    assert manifest["status"] == "accepted"
    assert manifest["supervisor_decision"]["approved_next_tasks"] == ["task-00"]
    assert any(item["kind"] == "supervisor_context_pack" for item in manifest["artifacts"])
    assert "supervisor-decision" in result["manifest"]
    assert "Supervisor Decision Report" in report
    assert "continue" in report


def test_supervisor_decision_validator_rejects_high_risk_drop_task_without_human(tmp_path: Path):
    project = tmp_path / "supervisor-decision-reject-drop"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    harness = Harness(project)
    context = harness.write_supervisor_context_pack(
        gate_reason="blocked_task",
        objective="Evaluate blocked task handling.",
    )
    decision = {
        "kind": SUPERVISOR_DECISION_KIND,
        "decision": "drop_task",
        "approved_next_tasks": [],
        "blocked_tasks": ["task-00"],
        "tasks_to_rewrite": ["task-00"],
        "requires_human": False,
        "reason": "Drop a blocked task from the roadmap.",
        "evidence": [context["path"]],
    }

    result = harness.write_supervisor_decision_evidence(
        decision,
        context_pack_path=context["path"],
        gate_reason="blocked_task",
    )
    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    error_codes = {item["code"] for item in result["validation"]["errors"]}

    assert result["accepted"] is False
    assert result["status"] == "rejected"
    assert "human_required" in error_codes
    assert result["safety_classification"]["high_risk_action"] is True
    assert manifest["validation"]["status"] == "rejected"
    assert manifest["safety_classification"]["decision_effect"] == "reject"


def test_supervisor_decision_cli_persists_request_human_review_decision(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-decision-cli"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    harness = Harness(project)
    context = harness.write_supervisor_context_pack(
        gate_reason="operator_requested_review",
        objective="Ask the operator to review a blocked local gate.",
    )
    decision_path = project / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "kind": SUPERVISOR_DECISION_KIND,
                "decision": "request_human_review",
                "approved_next_tasks": [],
                "blocked_tasks": ["task-00"],
                "tasks_to_rewrite": [],
                "requires_human": True,
                "reason": "The local evidence needs operator review before the queue changes.",
                "evidence": [context["path"]],
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "supervisor-decision",
            "--project-root",
            str(project),
            "--decision",
            str(decision_path),
            "--context-pack",
            context["path"],
            "--gate-reason",
            "operator_requested_review",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    manifest = json.loads((project / payload["manifest"]).read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["accepted"] is True
    assert payload["decision"] == "request_human_review"
    assert payload["requires_human"] is True
    assert manifest["supervisor_decision"]["decision"] == "request_human_review"
    assert manifest["safety_classification"]["risk_flags"]["manual"] is True


def test_supervisor_drive_records_failed_task_gate_in_drive_report(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drive-failed-gate"
    project.mkdir()
    init_project(project, "python-agent", name="supervisor-drive-failed-gate")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"raise SystemExit(7)\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        ["drive", "--project-root", str(project), "--supervisor-gate", "failed-task", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]
    report_text = (project / payload["drive_report"]).read_text(encoding="utf-8")

    assert exit_code == 0
    assert payload["status"] == "paused"
    assert payload["results"][0]["status"] == "failed"
    assert gate["kind"] == "engineering-harness.supervisor-gate.v1"
    assert gate["gate_type"] == "failed_task"
    assert gate["context_path"]
    assert gate["decision_path"]
    assert gate["application_status"] == "applied"
    assert gate["decision"] == "pause"
    assert "## Supervisor Gates" in report_text
    assert "Machine-readable supervisor gate records" in report_text


def test_supervisor_drive_deployment_gate_pauses_before_worker_execution(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drive-deployment-gate"
    project.mkdir()
    init_project(project, "python-agent", name="supervisor-drive-deployment-gate")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["deployment_sensitive"] = True
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('deployment-marker.txt').write_text('ran')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--supervisor-gate",
            "deployment-sensitive-task",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]

    assert exit_code == 0
    assert payload["status"] == "paused"
    assert payload["results"] == []
    assert not (project / "deployment-marker.txt").exists()
    assert gate["gate_type"] == "deployment_sensitive_task"
    assert gate["application_status"] == "applied"
    assert gate["context_path"]
    assert gate["decision_path"]


def test_supervisor_drive_success_quality_gate_continues_with_artifacts(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drive-success-quality-gate"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["quality_gate"] = True
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--max-tasks",
            "1",
            "--supervisor-gate",
            "quality-gate-completion",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]
    mutation_manifest = json.loads((project / gate["mutation_manifest"]).read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["results"][0]["status"] == "passed"
    assert gate["gate_type"] == "quality_gate_completion"
    assert gate["decision"] == "continue"
    assert gate["application_status"] == "applied"
    assert gate["action_result"]["status"] == "queue_order_applied"
    assert gate["action_result"]["approved_next_tasks"] == ["task-01"]
    assert gate["context_path"]
    assert gate["decision_path"]
    assert mutation_manifest["mutation"]["applied"] is True


def test_supervisor_drive_records_blocked_task_gate_for_policy_block(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drive-blocked-gate"
    project.mkdir()
    init_project(project, "python-agent", name="supervisor-drive-blocked-gate")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('blocked')\" --live"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        ["drive", "--project-root", str(project), "--supervisor-gate", "blocked-task", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]

    assert exit_code == 0
    assert payload["status"] == "paused"
    assert payload["results"][0]["status"] == "blocked"
    assert gate["gate_type"] == "blocked_task"
    assert gate["result_status"] == "blocked"
    assert gate["decision"] == "pause"
    assert gate["application_status"] == "applied"
    assert gate["context_path"]
    assert gate["decision_path"]


def test_supervisor_drive_records_milestone_completion_gate(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drive-milestone-gate"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    first_task, second_task = roadmap["milestones"][0]["tasks"]
    roadmap["milestones"] = [
        {
            "id": "first-milestone",
            "title": "First milestone",
            "status": "active",
            "objective": "Complete the first supervisor milestone.",
            "tasks": [first_task],
        },
        {
            "id": "second-milestone",
            "title": "Second milestone",
            "status": "active",
            "objective": "Keep a follow-on task queued after milestone completion.",
            "tasks": [second_task],
        },
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--max-tasks",
            "1",
            "--supervisor-gate",
            "milestone-completion",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]
    context_pack = json.loads((project / gate["context_path"]).read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["results"][0]["task"]["id"] == "task-00"
    assert payload["results"][0]["status"] == "passed"
    assert gate["gate_type"] == "milestone_completion"
    assert gate["decision"] == "continue"
    assert gate["application_status"] == "applied"
    assert gate["action_result"]["approved_next_tasks"] == ["task-01"]
    assert context_pack["risk_metadata"]["provided"]["milestone_id"] == "first-milestone"


def test_supervisor_drive_records_budget_risk_gate_after_task_budget(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drive-budget-gate"
    project.mkdir()
    init_project(project, "python-agent", name="supervisor-drive-budget-gate")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--max-tasks",
            "1",
            "--supervisor-gate",
            "budget-risk-threshold",
            "--supervisor-gate-risk-threshold",
            "100",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]

    assert exit_code == 0
    assert payload["status"] == "paused"
    assert payload["results"][0]["status"] == "passed"
    assert gate["gate_type"] == "budget_risk_threshold"
    assert gate["context_path"]
    assert gate["decision_path"]
    assert gate["application_status"] == "applied"


def test_supervisor_parallel_drive_records_blocked_gate_report(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-parallel-blocked-gate"
    project.mkdir()
    init_project(project, "python-agent", name="supervisor-parallel-blocked-gate")

    exit_code, payload = run_parallel_drive_json(
        capsys,
        project,
        "--supervisor-gate",
        "blocked-task",
    )
    gate = payload["supervisor_gates"][0]
    report_text = (project / payload["parallel_drive_report"]).read_text(encoding="utf-8")

    assert exit_code == 1
    assert payload["status"] == "paused"
    assert gate["gate_type"] == "blocked_task"
    assert gate["context_path"]
    assert gate["decision_path"]
    assert gate["application_status"] == "applied"
    assert "parallel-drive" in report_text
    assert "## Supervisor Gates" in report_text


def test_supervisor_continue_mutation_applies_approved_next_task_queue_order(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-continue-queue-mutation"
    project.mkdir()
    write_supervisor_context_project(project, task_count=3)
    decision_path = project / "supervisor-continue.json"
    decision_path.write_text(
        json.dumps(
            {
                "kind": SUPERVISOR_DECISION_KIND,
                "decision": "continue",
                "approved_next_tasks": ["task-02", "task-01"],
                "blocked_tasks": [],
                "tasks_to_rewrite": [],
                "requires_human": False,
                "reason": "Local supervisor mutation should prioritize the approved next task queue order.",
                "evidence": [".engineering/roadmap.yaml"],
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--supervisor-gate",
            "operator-request",
            "--supervisor-decision",
            str(decision_path),
            "--max-tasks",
            "1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]
    state = harness_state(project)
    mutation_manifest = json.loads((project / gate["mutation_manifest"]).read_text(encoding="utf-8"))
    mutation_report = (project / gate["mutation_report"]).read_text(encoding="utf-8")

    assert exit_code == 0
    assert payload["results"][0]["task"]["id"] == "task-02"
    assert gate["application_status"] == "applied"
    assert gate["action_result"]["status"] == "queue_order_applied"
    assert gate["action_result"]["approved_next_tasks"] == ["task-02", "task-01"]
    assert state["supervisor_queue"]["approved_next_tasks"] == ["task-02", "task-01"]
    assert mutation_manifest["kind"] == "engineering-orchestrator.supervisor-mutation.v1"
    assert mutation_manifest["mutation"]["applied"] is True
    assert mutation_manifest["mutation"]["action_result"]["approved_next_tasks"] == ["task-02", "task-01"]
    assert "Supervisor Mutation Report" in mutation_report
    assert "approved_next_tasks" in mutation_report


def test_supervisor_drop_task_approval_mutation_is_recorded_but_not_auto_applied(tmp_path: Path, capsys):
    project = tmp_path / "supervisor-drop-task-approval-mutation"
    project.mkdir()
    write_supervisor_context_project(project, task_count=2)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap_before = roadmap_path.read_text(encoding="utf-8")
    decision_path = project / "supervisor-drop-task.json"
    decision_path.write_text(
        json.dumps(
            {
                "kind": SUPERVISOR_DECISION_KIND,
                "decision": "drop_task",
                "approved_next_tasks": [],
                "blocked_tasks": ["task-00"],
                "tasks_to_rewrite": ["task-00"],
                "requires_human": True,
                "reason": "Drop task-00 from the roadmap after explicit human review.",
                "evidence": [".engineering/roadmap.yaml"],
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--supervisor-gate",
            "operator-request",
            "--supervisor-decision",
            str(decision_path),
            "--max-tasks",
            "1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    gate = payload["supervisor_gates"][0]
    mutation_manifest = json.loads((project / gate["mutation_manifest"]).read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["status"] == "paused"
    assert payload["results"] == []
    assert roadmap_path.read_text(encoding="utf-8") == roadmap_before
    assert gate["decision_status"] == "accepted"
    assert gate["decision"] == "drop_task"
    assert gate["requires_human"] is True
    assert gate["application_status"] == "skipped"
    assert gate["mutation_status"] == "requires_human"
    assert mutation_manifest["mutation"]["applied"] is False
    assert mutation_manifest["mutation"]["effect"] == "requires_human"
    assert mutation_manifest["supervisor_decision"]["tasks_to_rewrite"] == ["task-00"]


def test_supervisor_unsafe_live_mutation_requires_human_approval(tmp_path: Path):
    project = tmp_path / "supervisor-unsafe-live-approval"
    project.mkdir()
    write_supervisor_context_project(project, task_count=1)
    harness = Harness(project)
    context = harness.write_supervisor_context_pack(
        gate_reason="operator_request",
        objective="Evaluate unsafe supervisor production request.",
    )
    decision = {
        "kind": SUPERVISOR_DECISION_KIND,
        "decision": "continue",
        "approved_next_tasks": ["task-00"],
        "blocked_tasks": [],
        "tasks_to_rewrite": [],
        "requires_human": False,
        "reason": "Deploy to production and rotate a secret token before continuing.",
        "evidence": [context["path"]],
    }

    validation = harness.validate_supervisor_decision(decision, context_pack_path=context["path"])
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["accepted"] is False
    assert "human_required" in error_codes
    assert validation["safety_classification"]["requires_human_by_policy"] is True
    assert validation["safety_classification"]["risk_level"] == "high"
    assert set(validation["safety_classification"]["risk_categories"]) >= {"deployment", "live", "secret"}


def test_supervisor_decision_rejects_missing_local_evidence(tmp_path: Path):
    project = tmp_path / "supervisor-missing-evidence"
    project.mkdir()
    write_supervisor_context_project(project, task_count=1)
    harness = Harness(project)
    context = harness.write_supervisor_context_pack(
        gate_reason="operator_request",
        objective="Evaluate supervisor decision with missing evidence.",
    )
    decision = {
        "kind": SUPERVISOR_DECISION_KIND,
        "decision": "continue",
        "approved_next_tasks": ["task-00"],
        "blocked_tasks": [],
        "tasks_to_rewrite": [],
        "requires_human": False,
        "reason": "Continue even though the supervisor decision cites missing local evidence.",
        "evidence": [".engineering/reports/tasks/missing-supervisor-evidence.json"],
    }

    validation = harness.validate_supervisor_decision(decision, context_pack_path=context["path"])
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["accepted"] is False
    assert "missing_evidence_path" in error_codes
    assert "missing_evidence" in error_codes


def test_supervisor_context_pack_bounds_deep_risk_metadata_recursion(tmp_path: Path):
    project = tmp_path / "supervisor-bounded-recursion"
    project.mkdir()
    write_supervisor_context_project(project, task_count=1)
    nested: dict[str, object] = {"leaf": "supervisor bounded recursion leaf"}
    for index in range(80):
        nested = {
            "level": index,
            "child": nested,
            "wide": list(range(20)),
        }

    summary = Harness(project).write_supervisor_context_pack(
        gate_reason="bounded recursion operator request",
        objective="Prove supervisor context pack serialization remains bounded.",
        risk_metadata={"deep": nested, "wide": list(range(20))},
    )
    pack = json.loads((project / summary["path"]).read_text(encoding="utf-8"))
    provided = pack["risk_metadata"]["provided"]
    serialized = json.dumps(provided)

    assert len(provided["wide"]) == 8
    assert '"reason": "max_depth"' in serialized
    assert "supervisor bounded recursion leaf" not in serialized


def test_profiles_are_available():
    profile_ids = {item["id"] for item in list_profiles()}

    assert "evm-protocol" in profile_ids
    assert "python-agent" in profile_ids
    assert "trading-research" in profile_ids


def test_cli_help_uses_engineering_orchestrator_name():
    help_text = build_parser().format_help()

    assert "Engineering Orchestrator" in help_text
    assert "Goal-driven engineering harness" not in help_text


def test_canonical_and_legacy_cli_entry_points_are_packaged():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert scripts["engo"] == "engineering_orchestrator.cli:main"
    assert scripts["engh"] == "engineering_orchestrator.cli:main"
    assert (PROJECT_ROOT / "bin/engo").read_text(encoding="utf-8") == (
        PROJECT_ROOT / "bin/engh"
    ).read_text(encoding="utf-8")


def test_legacy_engineering_harness_imports_remain_compatible():
    legacy_cli = importlib.import_module("engineering_harness.cli")
    canonical_cli = importlib.import_module("engineering_orchestrator.cli")
    legacy_core = importlib.import_module("engineering_harness.core")
    canonical_core = importlib.import_module("engineering_orchestrator.core")

    assert legacy_cli.main is canonical_cli.main
    assert legacy_core.Harness is canonical_core.Harness


def test_readme_declares_orchestrator_canonical_name_and_legacy_compatibility():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.startswith("# Engineering Orchestrator")
    assert "Engineering Harness is a legacy compatibility name" in readme
    assert "Agent harness is reserved" in readme


def test_goal_intake_normalizes_local_contract_shape():
    contract = normalize_goal_intake(
        project_name="  Autonomous Report Worker  ",
        profile=" Python-Agent ",
        goal_text="Build a local autonomous report generator.\nInclude deterministic tests.",
        blueprint_path=Path("docs/blueprint.md"),
        constraints=[" local only ", "no private keys", "local only"],
        desired_experience_kind="dashboard",
    )

    assert contract["schema_version"] == 1
    assert contract["kind"] == "engineering-harness.goal-intake.v1"
    assert contract["project"] == {
        "name": "Autonomous Report Worker",
        "slug": "autonomous-report-worker",
        "profile": "python-agent",
    }
    assert contract["goal"] == {
        "text": "Build a local autonomous report generator. Include deterministic tests.",
    }
    assert contract["blueprint"] == {"path": "docs/blueprint.md", "provided": True}
    assert contract["constraints"] == ["local only", "no private keys"]
    assert contract["experience"] == {"kind": "dashboard", "provided": True}
    assert contract["safety"]["mode"] == "local-only"
    assert contract["safety"]["allow_live_services"] is False
    assert contract["safety"]["blocked_requirements"] == []
    assert contract["roadmap_seed"] == {
        "project": "Autonomous Report Worker",
        "profile": "python-agent",
        "goal": "Build a local autonomous report generator. Include deterministic tests.",
        "blueprint_path": "docs/blueprint.md",
        "constraints": ["local only", "no private keys"],
        "experience_kind": "dashboard",
    }


def test_goal_intake_validation_rejects_empty_goal():
    result = validate_goal_intake(
        project_name="agent-project",
        profile="python-agent",
        goal_text=" \n\t ",
        constraints=[],
        desired_experience_kind="api only",
    )

    assert result["status"] == "failed"
    assert result["goal_intake"] is None
    assert result["errors"] == ["`goal_text` is required"]

    with pytest.raises(GoalIntakeValidationError) as excinfo:
        normalize_goal_intake(project_name="agent-project", profile="python-agent", goal_text="")
    assert excinfo.value.errors == ["`goal_text` is required"]


@pytest.mark.parametrize(
    ("goal_text", "constraints", "expected_match"),
    [
        ("Deploy to production after generating the roadmap.", [], "Deploy to production"),
        ("Build a trading agent that can place live orders.", [], "place live orders"),
        ("Build a local dashboard.", ["Must execute real trades during acceptance."], "execute real trades"),
    ],
)
def test_goal_intake_rejects_unsafe_live_service_requirements(goal_text, constraints, expected_match):
    result = validate_goal_intake(
        project_name="agent-project",
        profile="python-agent",
        goal_text=goal_text,
        constraints=constraints,
        desired_experience_kind="dashboard",
    )

    assert result["status"] == "failed"
    assert result["goal_intake"] is None
    assert result["blocked_requirements"]
    assert any(expected_match in item["match"] for item in result["blocked_requirements"])
    assert any("unsafe live-service requirement" in error for error in result["errors"])


def test_goal_intake_validation_rejects_nonlocal_blueprint_and_unknown_kind():
    result = validate_goal_intake(
        project_name="agent-project",
        profile="python-agent",
        goal_text="Build a local API planner.",
        blueprint_path="https://example.com/blueprint.md",
        desired_experience_kind="desktop-app",
    )

    assert result["status"] == "failed"
    assert "`blueprint_path` must be a local path, not a URL" in result["errors"]
    assert any("desired_experience_kind `desktop-app` is not supported" in error for error in result["errors"])


def test_plan_goal_cli_proposes_starter_roadmap_without_writing(tmp_path, capsys):
    project = tmp_path / "planner-project"

    exit_code = cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Local Report CLI",
            "--profile",
            "python-agent",
            "--goal",
            "Build a CLI tool that generates local reports and documented command output.",
            "--experience-kind",
            "cli",
            "--constraint",
            "Keep examples deterministic.",
            "--stage-count",
            "3",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    roadmap = payload["roadmap"]
    stages = roadmap["continuation"]["stages"]

    assert exit_code == 0
    assert payload["status"] == "proposed"
    assert payload["materialized"] is False
    assert payload["stage_count"] == 3
    assert not (project / ".engineering/roadmap.yaml").exists()
    assert roadmap["project"] == "Local Report CLI"
    assert roadmap["experience"]["kind"] == "cli-only"
    assert roadmap["experience"]["derived"] is False
    assert roadmap["goal"]["constraints"] == ["Keep examples deterministic."]
    assert [stage["id"] for stage in stages] == [
        "stage-1-local-slice",
        "stage-2-experience-validation",
        "stage-3-policy-evidence-observability",
    ]
    assert roadmap["milestones"][0]["id"] == "baseline"
    assert roadmap["milestones"][0]["tasks"][0]["acceptance"]
    for stage in stages:
        task = stage["tasks"][0]
        assert task["file_scope"]
        assert task["implementation"][0]["executor"] == "codex"
        assert task["repair"][0]["executor"] == "codex"
        assert task["acceptance"][0]["command"].startswith("python3 ")
        assert task["e2e"][0]["command"].startswith("python3 ")
    assert payload["goal_intake"]["safety"]["allow_live_services"] is False


def test_plan_goal_cli_materializes_goal_file_roadmap_and_validates(tmp_path, capsys):
    project = tmp_path / "autonomous-worker"
    project.mkdir()
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("Build an autonomous dashboard worker for local research artifacts.", encoding="utf-8")

    exit_code = cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Autonomous Dashboard Worker",
            "--profile",
            "python-agent",
            "--goal-file",
            str(goal_file),
            "--blueprint",
            "docs/blueprint.md",
            "--materialize",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["status"] == "materialized"
    assert payload["materialized"] is True
    assert roadmap["generated_by"] == "engineering-harness-goal-roadmap-planner"
    assert roadmap["goal"]["blueprint"] == "docs/blueprint.md"
    assert roadmap["experience"]["kind"] == "dashboard"
    assert roadmap["continuation"]["enabled"] is True
    assert [stage["id"] for stage in roadmap["continuation"]["stages"]] == [
        "stage-1-local-slice",
        "stage-2-experience-validation",
        "stage-3-policy-evidence-observability",
        "stage-4-unattended-drive-readiness",
    ]
    assert roadmap["continuation"]["stages"][0]["tasks"][0]["implementation"][0]["executor"] == "codex"
    assert roadmap["self_iteration"]["enabled"] is True
    assert Harness(project).validate_roadmap()["status"] == "passed"

    assert cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Autonomous Dashboard Worker",
            "--profile",
            "python-agent",
            "--goal-file",
            str(goal_file),
            "--materialize",
            "--json",
        ]
    ) == 2


def test_spec_backlog_cli_proposes_and_materializes_remaining_spec_stages(tmp_path, capsys):
    project = tmp_path / "spec-backlog-project"
    project.mkdir()
    init_project(project, "python-agent", name="spec-backlog-project")
    docs_dir = project / "docs"
    docs_dir.mkdir()
    spec_plan = docs_dir / "spec-plan.md"
    spec_plan.write_text(
        """# Spec Plan

## Stage 1: Completed Foundation

Requirement refs:

- `EH-SPEC-001`

Goal:

Already complete.

Tasks:

1. Keep the completed foundation.

Acceptance:

- Existing behavior remains stable.

## Stage 2: Canonical Index

Requirement refs:

- `EH-SPEC-002`
- `EH-SPEC-014`

Goal:

Add a canonical spec index.

Tasks:

1. Add a top-level roadmap spec block.
2. Summarize spec coverage in status JSON.

Acceptance:

- Invalid refs are reported.
- Status includes compact spec coverage.
""",
        encoding="utf-8",
    )
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["spec"] = {"schema_version": 1, "development_plan": "docs/spec-plan.md"}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        [
            "spec-backlog",
            "--project-root",
            str(project),
            "--from-stage",
            "2",
            "--json",
        ]
    )

    proposal = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert proposal["status"] == "proposed"
    assert proposal["materialized"] is False
    assert proposal["source_count"] == 1
    assert proposal["stage_count"] == 1
    assert proposal["task_count"] == 2
    stage = proposal["stages"][0]
    assert stage["id"] == "docs-spec-plan-stage-2-canonical-index"
    assert stage["spec_refs"] == ["EH-SPEC-002", "EH-SPEC-014"]
    assert [task["spec_refs"] for task in stage["tasks"]] == [["EH-SPEC-002", "EH-SPEC-014"]] * 2
    assert stage["tasks"][0]["implementation"][0]["executor"] == "codex"

    assert cli_main(
        [
            "spec-backlog",
            "--project-root",
            str(project),
            "--from-stage",
            "2",
            "--materialize",
            "--json",
        ]
    ) == 0
    materialized = json.loads(capsys.readouterr().out)
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    continuation = updated["continuation"]["stages"]

    assert materialized["status"] == "materialized"
    assert continuation[-1]["id"] == "docs-spec-plan-stage-2-canonical-index"
    assert Harness(project).validate_roadmap()["status"] == "passed"

    assert cli_main(
        [
            "spec-backlog",
            "--project-root",
            str(project),
            "--from-stage",
            "2",
            "--materialize",
            "--json",
        ]
    ) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["status"] == "up_to_date"
    assert repeated["skipped_stage_count"] == 1


def test_plan_spec_cli_reads_explicit_spec_document_and_skips_semantic_coverage(tmp_path, capsys):
    project = tmp_path / "plan-spec-project"
    project.mkdir()
    init_project(project, "python-agent", name="plan-spec-project")
    docs_dir = project / "docs"
    docs_dir.mkdir()
    spec_plan = docs_dir / "spec-plan.md"
    spec_plan.write_text(
        """# Spec Plan

## Stage 3: Spec-To-Roadmap Planner

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-013`

Goal:

Generate or update roadmap stages from a specification while preserving traceability.

Tasks:

1. Extend `plan-goal` or add a dedicated planning command that reads a spec document.
2. Generate milestones, tasks, acceptance gates, E2E gates, and `spec_refs`.

Acceptance:

- Generated tasks cite spec refs.
- The planner does not duplicate existing roadmap coverage.
""",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "plan-spec",
            "--project-root",
            str(project),
            "--spec",
            str(spec_plan),
            "--from-stage",
            "3",
            "--json",
        ]
    )

    proposal = json.loads(capsys.readouterr().out)
    refs = ["EH-SPEC-001", "EH-SPEC-002", "EH-SPEC-013"]
    assert exit_code == 0
    assert proposal["status"] == "proposed"
    assert proposal["source_count"] == 1
    assert proposal["stage_count"] == 1
    assert proposal["task_count"] == 2
    stage = proposal["stages"][0]
    assert stage["source"]["path"] == "docs/spec-plan.md"
    assert stage["spec_refs"] == refs
    assert [task["spec_refs"] for task in stage["tasks"]] == [refs, refs]
    assert stage["tasks"][0]["e2e"][0]["spec_refs"] == refs

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap.setdefault("continuation", {"enabled": True, "stages": []}).setdefault("stages", []).append(
        {
            "id": "already-covered-spec-planner",
            "title": "Already Covered Spec Planner",
            "status": "planned",
            "spec_refs": refs,
            "tasks": [
                {
                    "id": "already-covered-generated-gates",
                    "title": "Generate milestones, tasks, acceptance gates, E2E gates, and `spec_refs`",
                    "status": "pending",
                    "file_scope": ["src/engineering_orchestrator/**", "tests/**", "docs/**"],
                    "acceptance": [{"name": "local smoke", "command": "python3 -c \"print('ok')\""}],
                },
                {
                    "id": "already-covered-plan-spec",
                    "title": "Extend `plan-goal` or add a dedicated planning command that reads a spec document",
                    "status": "pending",
                    "file_scope": ["src/engineering_orchestrator/**", "tests/**", "docs/**"],
                    "acceptance": [{"name": "local smoke", "command": "python3 -c \"print('ok')\""}],
                },
            ],
        }
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(
        [
            "plan-spec",
            "--project-root",
            str(project),
            "--spec",
            str(spec_plan),
            "--from-stage",
            "3",
            "--json",
        ]
    ) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["status"] == "proposed"
    assert repeated["stage_count"] == 0
    assert repeated["skipped_stage_count"] == 1
    assert repeated["skipped_stages"][0]["reasons"] == ["existing_stage_semantics"]


def test_plan_spec_cli_filters_partially_covered_tasks_by_spec_refs_and_semantics(tmp_path, capsys):
    project = tmp_path / "partial-plan-spec-project"
    project.mkdir()
    init_project(project, "python-agent", name="partial-plan-spec-project")
    docs_dir = project / "docs"
    docs_dir.mkdir()
    spec_plan = docs_dir / "spec-plan.md"
    spec_plan.write_text(
        """# Spec Plan

## Stage 3: Spec-To-Roadmap Planner

Requirement refs:

- `EH-SPEC-001`
- `EH-SPEC-002`
- `EH-SPEC-013`

Goal:

Generate or update roadmap stages from a specification while preserving traceability.

Tasks:

1. Add duplicate-plan detection based on spec refs and task semantics.
2. Make self-iteration append continuation stages that cite spec refs.

Acceptance:

- The planner does not duplicate existing roadmap coverage.
""",
        encoding="utf-8",
    )
    refs = ["EH-SPEC-001", "EH-SPEC-002", "EH-SPEC-013"]
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap.setdefault("continuation", {"enabled": True, "stages": []}).setdefault("stages", []).append(
        {
            "id": "already-covered-duplicate-plan-task",
            "title": "Already Covered Duplicate Plan Task",
            "status": "planned",
            "tasks": [
                {
                    "id": "already-covered-task-semantics",
                    "title": "Add duplicate-plan detection based on spec refs and task semantics.",
                    "status": "pending",
                    "spec_refs": refs,
                    "file_scope": ["src/engineering_orchestrator/**", "tests/**", "docs/**"],
                    "acceptance": [{"name": "local smoke", "command": "python3 -c \"print('ok')\""}],
                }
            ],
        }
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(
        [
            "plan-spec",
            "--project-root",
            str(project),
            "--spec",
            str(spec_plan),
            "--from-stage",
            "3",
            "--json",
        ]
    )

    proposal = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert proposal["status"] == "proposed"
    assert proposal["stage_count"] == 1
    assert proposal["task_count"] == 1
    assert proposal["skipped_task_count"] == 1
    assert proposal["skipped_tasks"][0]["reasons"] == ["existing_task_semantics"]
    assert proposal["skipped_tasks"][0]["matched_task_ids"] == ["already-covered-task-semantics"]
    stage = proposal["stages"][0]
    assert stage["source"]["task_count"] == 1
    assert stage["source"]["source_task_count"] == 2
    assert stage["skipped_task_count"] == 1
    assert [task["title"] for task in stage["tasks"]] == [
        "Make self-iteration append continuation stages that cite spec refs"
    ]


def test_goal_planner_generates_python_behavioral_and_journey_gates(tmp_path):
    proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-python-agent",
        project_name="Gated Dashboard Agent",
        profile="python-agent",
        goal_text="Build a local dashboard agent that reads reports and lets an operator inspect E2E evidence.",
        desired_experience_kind="dashboard",
    )
    task = continuation_tasks(proposal["roadmap"])[0]

    acceptance_names = [item["name"] for item in task["acceptance"]]
    acceptance_commands = [item["command"] for item in task["acceptance"]]
    e2e_names = [item["name"] for item in task["e2e"]]
    e2e_commands = [item["command"] for item in task["e2e"]]
    implementation_prompt = task["implementation"][0]["prompt"]

    assert acceptance_names[0] == "python behavioral tests pass"
    assert acceptance_commands[0] == "python3 -m pytest tests -q"
    assert acceptance_names[-1] == "roadmap task contract smoke"
    assert "generated task contract is locally checkable" not in acceptance_names
    assert any(command == "python3 -m pytest tests/e2e -q" for command in e2e_commands)
    assert any("journey evidence" in name for name in e2e_names)
    assert "tests/e2e/" in implementation_prompt
    assert "docs/e2e/" in implementation_prompt
    assert "python3 -m pytest tests -q" in implementation_prompt
    assert validate_roadmap_payload(tmp_path, proposal["roadmap"])["status"] == "passed"


def test_goal_planner_generates_node_frontend_npm_gates(tmp_path):
    proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-node-frontend",
        project_name="Gated Frontend",
        profile="node-frontend",
        goal_text="Build an operator dashboard frontend with local journey evidence.",
        desired_experience_kind="dashboard",
    )
    task = continuation_tasks(proposal["roadmap"])[0]

    assert task["acceptance"][0]["name"] == "frontend unit and integration tests pass"
    assert task["acceptance"][0]["command"] == "npm test"
    assert task["e2e"][0]["command"] == "python3 -m engineering_orchestrator.browser_e2e --project-root . --journey-id operator-observes-run"
    assert task["e2e"][0]["user_experience_gate"]["runner"]["fallback"]["kind"] == "static-html-smoke"
    assert any(item["command"] == "npm run e2e" for item in task["e2e"])
    assert "engineering_orchestrator.browser_e2e" in task["implementation"][0]["prompt"]
    assert "roadmap task contract smoke" == task["acceptance"][-1]["name"]
    assert validate_roadmap_payload(tmp_path, proposal["roadmap"])["status"] == "passed"


def test_goal_planner_generates_cli_and_api_documented_example_gates(tmp_path):
    cli_proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-cli",
        project_name="Gated CLI",
        profile="python-agent",
        goal_text="Build a CLI tool that writes a deterministic local report.",
        desired_experience_kind="cli",
    )
    api_proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-api",
        project_name="Gated API",
        profile="python-agent",
        goal_text="Build a local API with a documented client example.",
        desired_experience_kind="api",
    )

    cli_task = continuation_tasks(cli_proposal["roadmap"])[0]
    api_task = continuation_tasks(api_proposal["roadmap"])[0]

    assert any("CLI example" in item["name"] for item in cli_task["acceptance"])
    assert any("examples/" in item.get("guidance", "") for item in cli_task["acceptance"])
    assert "tests/cli/" in cli_task["implementation"][0]["prompt"]
    assert any("API example" in item["name"] for item in api_task["acceptance"])
    assert any("tests/api/" in item.get("guidance", "") for item in api_task["acceptance"])
    assert "request" in api_task["implementation"][0]["prompt"]


def test_init_project_creates_config_and_policy(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()

    result = init_project(project, "python-agent", name="agent-project")

    assert Path(result["roadmap"]).exists()
    assert (project / ".engineering/policies/command-allowlist.yaml").exists()
    harness = Harness(project)
    assert harness.status_summary()["project"] == "agent-project"


def test_harness_runs_task_and_writes_state(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["status"] == "passed"
    assert (project / state["tasks"]["tests"]["last_report"]).exists()


def test_harness_runs_task_and_writes_matching_manifest(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('done.txt').write_text('done'); print('manifest ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    report_path = project / result["report"]
    manifest_path = project / result["manifest"]
    assert manifest_path == report_path.with_suffix(".json")
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project"] == "agent-project"
    assert manifest["task_id"] == "tests"
    assert manifest["task"]["id"] == "tests"
    assert manifest["milestone"]["id"] == "baseline"
    assert manifest["status"] == "passed"
    assert manifest["message"] == result["message"]
    assert manifest["started_at"]
    assert manifest["finished_at"]
    assert manifest["attempt"] == 1
    assert manifest["report_path"] == result["report"]
    assert manifest["manifest_path"] == result["manifest"]
    assert manifest["artifacts"] == [
        {"kind": "markdown_report", "path": result["report"]},
        {"kind": "json_manifest", "path": result["manifest"]},
    ]
    assert manifest["runs"][0]["phase"] == "acceptance-1"
    assert manifest["runs"][0]["executor"] == "shell"
    assert manifest["runs"][0]["status"] == "passed"
    assert manifest["runs"][0]["returncode"] == 0
    assert manifest["runs"][0]["stdout"]["bytes"] > 0
    assert manifest["runs"][0]["stdout"]["sha256"]
    assert manifest["runs"][0]["executor_metadata"]["schema_version"] == 1
    assert manifest["runs"][0]["executor_metadata"]["id"] == "shell"
    assert manifest["runs"][0]["executor_metadata"]["kind"] == "process"
    assert manifest["runs"][0]["executor_metadata"]["input_mode"] == "command"
    assert manifest["runs"][0]["executor_metadata"]["uses_command_policy"] is True
    assert manifest["runs"][0]["executor_result"]["schema_version"] == 1
    assert manifest["runs"][0]["executor_result"]["status"] == "passed"
    assert manifest["runs"][0]["executor_result"]["returncode"] == 0
    assert manifest["runs"][0]["executor_result"]["stdout"] == manifest["runs"][0]["stdout"]
    assert manifest["runs"][0]["executor_result"]["stderr"] == manifest["runs"][0]["stderr"]
    assert result["runs"][0]["executor_metadata"]["id"] == "shell"
    assert result["runs"][0]["executor_result"]["status"] == "passed"
    assert manifest["safety"]["git_preflight"]["status"] == "clean"
    assert manifest["safety"]["file_scope_guard"]["status"] == "passed"
    assert manifest["git"]["is_repository"] is True
    assert manifest["git"]["head"] == head
    assert manifest["git"]["dirty_before_paths"] == []
    assert manifest["git"]["dirty_after_paths"] == ["done.txt"]
    assert manifest["policy_input"]["schema_version"] == 1
    assert manifest["policy_input"]["project"]["name"] == "agent-project"
    assert manifest["policy_input"]["task"]["id"] == "tests"
    assert manifest["policy_input"]["file_scope"]["patterns"] == ["**"]
    assert manifest["policy_input"]["approvals"] == {
        "allow_manual": False,
        "allow_agent": False,
        "manual_required": False,
        "agent_required": False,
        "executor_agent_required": False,
    }
    assert manifest["policy_input"]["live"]["allow_live"] is False

    command_policy = policy_decision(manifest, "command_policy", outcome="allowed")
    assert command_policy["schema_version"] == 1
    assert command_policy["effect"] == "allow"
    assert command_policy["severity"] == "info"
    assert command_policy["input"]["phase"] == "acceptance"
    assert command_policy["input"]["command"]["executor"] == "shell"
    assert command_policy["input"]["executor"]["uses_command_policy"] is True
    assert policy_decision(manifest, "executor_policy", outcome="allowed")["executor"] == "shell"
    assert policy_decision(manifest, "executor_approval", outcome="allowed")["reason"] == "executor approval not required"
    assert policy_decision(manifest, "manual_approval", outcome="allowed")["reason"] == "manual approval not required"
    assert policy_decision(manifest, "live_approval", outcome="allowed")["reason"] == "live approval not required"
    assert policy_decision(manifest, "git_preflight", outcome="allowed")["status"] == "clean"
    assert policy_decision(manifest, "file_scope_guard", outcome="allowed")["status"] == "passed"
    summary = manifest["policy_decision_summary"]
    assert summary["total"] == len(manifest["policy_decisions"]) == 8
    assert summary["by_kind"] == {
        "agent_approval": 1,
        "command_policy": 1,
        "executor_approval": 1,
        "executor_policy": 1,
        "file_scope_guard": 1,
        "git_preflight": 1,
        "live_approval": 1,
        "manual_approval": 1,
    }
    assert summary["by_outcome"] == {"allowed": 8}
    assert summary["blocking"] == []
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == summary
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    index = Harness(project).manifest_index()
    assert index["policy_decision_summary"]["total"] == summary["total"]
    assert index["manifests"][0]["policy_decision_summary"] == summary
    assert Harness(project).status_summary()["manifest_index"]["policy_decision_summary"]["total"] == summary["total"]

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["last_manifest"] == result["manifest"]


def test_roadmap_validation_checks_spec_refs(tmp_path):
    roadmap = {
        "version": 1,
        "project": "spec-project",
        "profile": "python-agent",
        "milestones": [
            {
                "id": "baseline",
                "title": "Baseline",
                "tasks": [
                    {
                        "id": "spec-task",
                        "title": "Spec task",
                        "spec_refs": ["EH-SPEC-002", "EH-SPEC-008"],
                        "file_scope": ["**"],
                        "acceptance": [
                            {
                                "name": "spec acceptance",
                                "command": "python3 -c \"print('ok')\"",
                                "spec_refs": ["EH-SPEC-007"],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    valid_root = tmp_path / "valid"
    invalid_root = tmp_path / "invalid"
    valid_root.mkdir()
    invalid_root.mkdir()

    assert validate_roadmap_payload(valid_root, roadmap)["status"] == "passed"

    invalid = deepcopy(roadmap)
    invalid["milestones"][0]["tasks"][0]["spec_refs"] = ["EH-SPEC-002", "EH-SPEC-002"]
    invalid["milestones"][0]["tasks"][0]["acceptance"][0]["spec_refs"] = [""]
    result = validate_roadmap_payload(invalid_root, invalid)

    assert result["status"] == "failed"
    assert "task `spec-task` spec_refs contains duplicate spec ref `EH-SPEC-002`" in result["errors"]
    assert "task `spec-task` acceptance[0].spec_refs[0] must be a non-empty string" in result["errors"]


def test_roadmap_spec_block_indexes_requirement_coverage(tmp_path, capsys):
    project = tmp_path / "indexed-spec-project"
    project.mkdir()
    (project / "docs").mkdir()
    (project / "docs/spec.md").write_text(
        "# Project Spec\n\n### EH-SPEC-001: Intake\n\n### EH-SPEC-002: Planning\n",
        encoding="utf-8",
    )
    (project / "docs/spec-index.json").write_text(
        json.dumps(
            {
                "kind": "engineering-harness.spec-index.v1",
                "requirements": [
                    {"id": "EH-SPEC-001", "title": "Intake"},
                    {"id": "EH-SPEC-002", "title": "Planning"},
                    {"id": "EH-SPEC-014", "title": "Distribution"},
                ],
            }
        ),
        encoding="utf-8",
    )
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text(
        json.dumps(
            {
                "version": 1,
                "project": "indexed-spec-project",
                "profile": "python-agent",
                "spec": {
                    "path": "docs/spec.md",
                    "kind": "markdown",
                    "requirements_index": "docs/spec-index.json",
                },
                "milestones": [
                    {
                        "id": "baseline",
                        "title": "Baseline",
                        "tasks": [
                            {
                                "id": "indexed-spec-task",
                                "title": "Indexed spec task",
                                "spec_refs": ["EH-SPEC-001"],
                                "file_scope": ["**"],
                                "acceptance": [
                                    {
                                        "name": "spec acceptance",
                                        "command": "python3 -c \"print('ok')\"",
                                        "spec_refs": ["EH-SPEC-002"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    validation = Harness(project).validate_roadmap()
    status = Harness(project).status_summary()
    coverage = status["spec"]

    assert validation["status"] == "passed"
    assert validation["spec"]["known_requirement_count"] == 3
    assert coverage["status"] == "ok"
    assert coverage["path"] == "docs/spec.md"
    assert coverage["kind"] == "markdown"
    assert coverage["requirements_index"] == "docs/spec-index.json"
    assert coverage["known_requirement_count"] == 3
    assert coverage["referenced_requirement_count"] == 2
    assert coverage["covered_requirement_count"] == 2
    assert coverage["unknown_requirement_count"] == 0
    assert coverage["unreferenced_requirements"] == ["EH-SPEC-014"]
    assert coverage["task_with_spec_refs_count"] == 1
    assert coverage["command_with_spec_refs_count"] == 1
    assert status["runtime_dashboard"]["spec"]["covered_requirement_count"] == 2

    assert cli_main(["status", "--project-root", str(project)]) == 0
    text = capsys.readouterr().out
    assert "Spec coverage: ok 2/3 covered refs=2 unknown=0 path=docs/spec.md" in text


def test_roadmap_spec_block_indexes_nested_structured_requirements(tmp_path):
    project = tmp_path / "nested-indexed-spec-project"
    project.mkdir()
    (project / "docs").mkdir()
    (project / "docs/spec.md").write_text("# Project Spec\n", encoding="utf-8")
    (project / "docs/spec-index.json").write_text(
        json.dumps(
            {
                "kind": "engineering-harness.spec-index.v1",
                "groups": [
                    {
                        "title": "Core",
                        "requirements": [
                            {"id": "EH-SPEC-001", "title": "Intake"},
                            {"requirement_id": "EH-SPEC-002", "title": "Planning"},
                        ],
                    },
                    {
                        "title": "Distribution",
                        "items": {
                            "EH-SPEC-014": {"title": "Public Distribution"},
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    (engineering_dir / "roadmap.yaml").write_text(
        json.dumps(
            {
                "version": 1,
                "project": "nested-indexed-spec-project",
                "profile": "python-agent",
                "spec": {
                    "path": "docs/spec.md",
                    "kind": "markdown",
                    "requirements_index": "docs/spec-index.json",
                },
                "milestones": [
                    {
                        "id": "baseline",
                        "tasks": [
                            {
                                "id": "nested-index-task",
                                "title": "Nested index task",
                                "spec_refs": ["EH-SPEC-014"],
                                "acceptance": [
                                    {
                                        "name": "nested index acceptance",
                                        "command": "python3 -c \"print('ok')\"",
                                        "spec_refs": ["EH-SPEC-002"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    validation = Harness(project).validate_roadmap()
    coverage = Harness(project).status_summary()["spec"]

    assert validation["status"] == "passed"
    assert validation["spec"]["known_requirement_count"] == 3
    assert coverage["requirements_source"] == "requirements_index"
    assert coverage["known_requirement_count"] == 3
    assert coverage["referenced_requirements"] == ["EH-SPEC-002", "EH-SPEC-014"]
    assert coverage["unreferenced_requirements"] == ["EH-SPEC-001"]


def test_roadmap_spec_block_reports_unknown_requirement_refs(tmp_path):
    project = tmp_path / "unknown-spec-ref-project"
    project.mkdir()
    (project / "docs").mkdir()
    (project / "docs/spec.md").write_text("# Project Spec\n", encoding="utf-8")
    (project / "docs/spec-index.json").write_text(
        json.dumps({"requirements": [{"id": "EH-SPEC-001"}]}),
        encoding="utf-8",
    )
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    (engineering_dir / "roadmap.yaml").write_text(
        json.dumps(
            {
                "version": 1,
                "project": "unknown-spec-ref-project",
                "profile": "python-agent",
                "spec": {
                    "path": "docs/spec.md",
                    "kind": "markdown",
                    "requirements_index": "docs/spec-index.json",
                },
                "milestones": [
                    {
                        "id": "baseline",
                        "tasks": [
                            {
                                "id": "unknown-ref-task",
                                "spec_refs": ["EH-SPEC-999"],
                                "acceptance": [
                                    {
                                        "name": "unknown command ref",
                                        "command": "python3 -c \"print('ok')\"",
                                        "spec_refs": ["EH-SPEC-998"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    validation = Harness(project).validate_roadmap()
    coverage = Harness(project).status_summary()["spec"]

    assert validation["status"] == "failed"
    assert "task `unknown-ref-task` spec_refs[0] references unknown requirement id `EH-SPEC-999`" in validation["errors"]
    assert (
        "task `unknown-ref-task` acceptance[0].spec_refs[0] references unknown requirement id `EH-SPEC-998`"
        in validation["errors"]
    )
    assert coverage["status"] == "unknown_refs"
    assert coverage["unknown_requirements"] == ["EH-SPEC-998", "EH-SPEC-999"]


def test_spec_refs_are_preserved_in_manifest_policy_input_and_report(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["spec_refs"] = ["EH-SPEC-002", "EH-SPEC-008"]
    task["acceptance"][0]["name"] = "spec trace acceptance"
    task["acceptance"][0]["command"] = "python3 -c \"print('spec trace ok')\""
    task["acceptance"][0]["spec_refs"] = ["EH-SPEC-007"]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task())
    manifest = task_manifest(project, result)
    command_policy = policy_decision(manifest, "command_policy", outcome="allowed")
    report_text = (project / result["report"]).read_text(encoding="utf-8")

    assert result["status"] == "passed"
    assert result["task"]["spec_refs"] == ["EH-SPEC-002", "EH-SPEC-008"]
    assert result["runs"][0]["spec_refs"] == ["EH-SPEC-007"]
    assert manifest["task"]["spec_refs"] == ["EH-SPEC-002", "EH-SPEC-008"]
    assert manifest["task"]["acceptance"][0]["spec_refs"] == ["EH-SPEC-007"]
    assert manifest["runs"][0]["spec_refs"] == ["EH-SPEC-007"]
    assert manifest["policy_input"]["task"]["spec_refs"] == ["EH-SPEC-002", "EH-SPEC-008"]
    assert command_policy["input"]["task"]["spec_refs"] == ["EH-SPEC-002", "EH-SPEC-008"]
    assert command_policy["input"]["command"]["spec_refs"] == ["EH-SPEC-007"]
    assert "## Spec Traceability" in report_text
    assert 'Task spec refs: `["EH-SPEC-002", "EH-SPEC-008"]`' in report_text
    assert 'acceptance `spec trace acceptance` spec refs: `["EH-SPEC-007"]`' in report_text


def test_agent_prompts_include_bounded_requirement_context_and_manifest_reference(tmp_path):
    captured: dict[str, str] = {}

    class RecordingCodexExecutor:
        metadata = ExecutorMetadata(
            id="codex",
            name="Recording Codex",
            kind="agent",
            adapter="test.recording-codex",
            input_mode="prompt",
            capabilities=("agent", "workspace_write", "stdout", "stderr"),
            requires_agent_approval=True,
        )

        def prepare_invocation(self, invocation, task_context):
            return CodexExecutorAdapter().prepare_invocation(invocation, task_context)

        def display_command(self, invocation):
            return f"recording-codex <task:{invocation.task_id}>"

        def execute(self, invocation):
            captured["prompt"] = invocation.prompt or ""
            captured["context_pack_path"] = invocation.context_pack["path"]
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="agent ok",
                stderr="",
                metadata={"selected": "codex"},
            )

    project = tmp_path / "agent-context-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-context-project")
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs/spec.md").write_text(
        "\n".join(
            [
                "# Project Spec",
                "",
                "### EH-SPEC-004: Executor Abstraction",
                "",
                "Executors must declare capabilities and normalize results.",
                "",
                "### EH-SPEC-005: Model And Memory Layer",
                "",
                "Agent prompts receive bounded task, spec, file-scope, and verification context.",
                "OPENAI_API_KEY=super-secret-value",
                "",
                "### EH-SPEC-010: Policy And Governance",
                "",
                "Policy decisions expose agent, secret, network, and filesystem risk.",
                "",
                "### EH-SPEC-999: Unrelated Requirement",
                "",
                "This unrelated section must not be copied into the agent prompt.",
            ]
        ),
        encoding="utf-8",
    )
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["spec"] = {
        "path": "docs/spec.md",
        "kind": "markdown",
    }
    task = roadmap["milestones"][0]["tasks"][0]
    task["spec_refs"] = ["EH-SPEC-005"]
    task["implementation"] = [
        {
            "name": "agent implementation",
            "executor": "codex",
            "prompt": "Use the requirement excerpts and do not call external services.",
            "spec_refs": ["EH-SPEC-004"],
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('agent context acceptance ok')\""
    task["acceptance"][0]["spec_refs"] = ["EH-SPEC-010"]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project, executor_registry=ExecutorRegistry((RecordingCodexExecutor(), ShellExecutorAdapter())))
    result = harness.run_task(harness.next_task(), allow_agent=True)
    manifest = task_manifest(project, result)
    context_pack_path = project / captured["context_pack_path"]
    context_pack = json.loads(context_pack_path.read_text(encoding="utf-8"))
    prompt = captured["prompt"]

    assert result["status"] == "passed"
    assert "Spec refs:" in prompt
    assert "- EH-SPEC-004" in prompt
    assert "- EH-SPEC-005" in prompt
    assert "- EH-SPEC-010" in prompt
    assert "Requirement excerpts:" in prompt
    assert "EH-SPEC-005: Model And Memory Layer" in prompt
    assert "Agent context pack:" in prompt
    assert captured["context_pack_path"] in prompt
    assert "EH-SPEC-999" not in prompt
    assert "super-secret-value" not in prompt
    assert "OPENAI_API_KEY=[REDACTED]" in prompt

    assert context_pack_path.exists()
    assert context_pack["kind"] == "engineering-harness.agent-context-pack"
    assert context_pack["spec_refs"] == ["EH-SPEC-005", "EH-SPEC-004", "EH-SPEC-010"]
    assert [item["id"] for item in context_pack["requirements"]] == [
        "EH-SPEC-005",
        "EH-SPEC-004",
        "EH-SPEC-010",
    ]
    assert "super-secret-value" not in context_pack_path.read_text(encoding="utf-8")
    assert manifest["runs"][0]["context_pack"]["path"] == captured["context_pack_path"]
    assert manifest["runs"][0]["executor_result"]["metadata"] == {"selected": "codex"}
    assert any(
        artifact["kind"] == "agent_context_pack" and artifact["path"] == captured["context_pack_path"]
        for artifact in manifest["artifacts"]
    )


def test_agent_context_pack_includes_agent_development_evidence_and_reference_notes(tmp_path):
    captured: dict[str, str] = {}

    class RecordingCodexExecutor:
        metadata = ExecutorMetadata(
            id="codex",
            name="Recording Codex",
            kind="agent",
            adapter="test.recording-codex",
            input_mode="prompt",
            capabilities=("agent", "workspace_write", "stdout", "stderr"),
            requires_agent_approval=True,
        )

        def prepare_invocation(self, invocation, task_context):
            return CodexExecutorAdapter().prepare_invocation(invocation, task_context)

        def display_command(self, invocation):
            return f"recording-codex <task:{invocation.task_id}>"

        def execute(self, invocation):
            captured["context_pack_path"] = invocation.context_pack["path"]
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="agent ok",
                stderr="",
                metadata={},
            )

    project = tmp_path / "agent-development-context-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-development-context-project")
    (project / "docs").mkdir(exist_ok=True)
    (project / "src/engineering_orchestrator").mkdir(parents=True, exist_ok=True)
    (project / "src/engineering_orchestrator/executors.py").write_text(
        "# Executor registry catalog\n",
        encoding="utf-8",
    )
    (project / "docs/spec.md").write_text(
        "\n".join(
            [
                "# Agent Development Spec",
                "",
                "## context-pack-task - Task-specific requirement excerpt",
                "",
                "This bounded excerpt is keyed by the roadmap task id and should reach the agent.",
            ]
        ),
        encoding="utf-8",
    )

    report_dir = project / ".engineering/reports/tasks"
    report_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = project / ".engineering/reports/task-agent-developer/candidate.json"
    audit_path = project / "artifacts/task-agent-developer/audit.json"
    for path in (candidate_path, audit_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps({"candidate": "agent package"}), encoding="utf-8")
    audit_path.write_text(json.dumps({"audit": "agent package checked"}), encoding="utf-8")
    previous_report = report_dir / "20240101T000000Z-prior-agent-task.md"
    previous_manifest = previous_report.with_suffix(".json")
    previous_report.write_text(
        "\n".join(
            [
                "# Task Report: prior-agent-task",
                "",
                "### acceptance-1: failing context check",
                "",
                "- Status: `failed`",
                "- Return code: `1`",
                "",
                "Stderr:",
                "",
                "```text",
                "missing context pack evidence",
                "```",
            ]
        ),
        encoding="utf-8",
    )
    previous_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "engineering-harness.task-run-manifest",
                "project": "agent-development-context-project",
                "project_root": str(project),
                "profile": "python-agent",
                "roadmap_path": ".engineering/roadmap.yaml",
                "task_id": "prior-agent-task",
                "milestone_id": "agent-development-toolchain",
                "task": {"id": "prior-agent-task", "title": "Prior agent task"},
                "milestone": {"id": "agent-development-toolchain", "title": "Agent Development"},
                "status": "failed",
                "message": "acceptance failed",
                "started_at": "2024-01-01T00:00:00Z",
                "finished_at": "2024-01-01T00:00:01Z",
                "dry_run": False,
                "attempt": 1,
                "report_path": str(previous_report.relative_to(project)),
                "manifest_path": str(previous_manifest.relative_to(project)),
                "artifacts": [
                    {
                        "kind": "task_agent_developer_candidate",
                        "path": str(candidate_path.relative_to(project)),
                        "exists": True,
                        "bytes": candidate_path.stat().st_size,
                    },
                    {
                        "kind": "task_agent_developer_audit",
                        "path": str(audit_path.relative_to(project)),
                        "exists": True,
                        "bytes": audit_path.stat().st_size,
                    },
                ],
                "runs": [
                    {
                        "phase": "acceptance-1",
                        "name": "failing context check",
                        "executor": "shell",
                        "command": "python3 -c 'raise SystemExit(1)'",
                        "status": "failed",
                        "returncode": 1,
                        "stdout": {"bytes": 0, "sha256": None},
                        "stderr": {"bytes": 29, "sha256": "stderr-sha"},
                        "executor_result": {"metadata": {}},
                        "artifacts": [
                            {
                                "kind": "task_agent_developer_candidate",
                                "path": str(candidate_path.relative_to(project)),
                                "exists": True,
                                "bytes": candidate_path.stat().st_size,
                            },
                            {
                                "kind": "task_agent_developer_audit",
                                "path": str(audit_path.relative_to(project)),
                                "exists": True,
                                "bytes": audit_path.stat().st_size,
                            },
                        ],
                    }
                ],
                "policy_decision_summary": {"total": 0, "by_kind": {}, "by_outcome": {}, "by_effect": {}, "by_severity": {}, "blocking": [], "requires_approval": []},
                "safety_audit": {"unsafe_decision_count": 0, "unsafe_classes": [], "unsafe_capabilities": []},
                "git": {"is_repository": False},
            }
        ),
        encoding="utf-8",
    )

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["spec"] = {"path": "docs/spec.md", "kind": "markdown"}
    task = roadmap["milestones"][0]["tasks"][0]
    task["id"] = "context-pack-task"
    task["title"] = "Upgrade agent development context"
    task["spec_refs"] = ["context-pack-task"]
    task["reference_repositories"] = [
        {
            "name": "agent package reference repo",
            "url": "https://example.invalid/agent-package.git",
            "notes": "Use this reference repo note only as local design guidance.",
        }
    ]
    task["implementation"] = [
        {
            "name": "agent implementation",
            "executor": "codex",
            "prompt": "Use the agent development context pack.",
        }
    ]
    task["acceptance"] = [{"name": "local smoke", "command": "python3 -c \"print('ok')\""}]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(
        project,
        executor_registry=ExecutorRegistry((RecordingCodexExecutor(), ShellExecutorAdapter())),
    ).run_task(Harness(project).next_task(), allow_agent=True)
    context_pack = json.loads((project / captured["context_pack_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "passed"
    assert context_pack["requirements"][0]["id"] == "context-pack-task"
    assert "roadmap task id" in context_pack["requirements"][0]["excerpt"]
    assert context_pack["task_reference_excerpts"]["included_count"] == 1
    assert context_pack["agent_artifacts"]["by_kind"]["task_agent_developer_candidate"] == 1
    assert context_pack["agent_artifacts"]["by_kind"]["task_agent_developer_audit"] == 1
    assert "agent package" in json.dumps(context_pack["agent_artifacts"])
    assert context_pack["acceptance_failures"]["included_count"] == 1
    assert "missing context pack evidence" in context_pack["acceptance_failures"]["failures"][0]["report_excerpt"]["excerpt"]
    assert any(item["path"] == "src/engineering_orchestrator/executors.py" for item in context_pack["registry_catalog"]["files"])
    assert context_pack["reference_repositories"]["repositories"][0]["notes"] == (
        "Use this reference repo note only as local design guidance."
    )


def test_opa_policy_input_export_shape(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())
    manifest = task_manifest(project, result)

    exported = export_policy_input_for_opa(manifest["policy_input"])

    assert exported["schema_version"] == 1
    assert exported["kind"] == "opa_rego_policy_input_export"
    assert exported["target"] == "opa-rego"
    assert exported["policy_input_schema_version"] == 1
    assert exported["policy_decision_schema_version"] == 1
    assert exported["authoritative_engine"] == "python"
    assert exported["external_evaluation"] == {
        "enabled": False,
        "decision_mode": "advisory",
        "runtime_dependency": None,
    }
    assert exported["rego"] == {
        "package": "engineering_orchestrator.policy.v1",
        "entrypoint": "data.engineering_orchestrator.policy.v1.decisions",
        "input_path": "input.policy_input",
    }
    assert exported["policy_input"] == manifest["policy_input"]

    exported["policy_input"]["task"]["id"] = "mutated"
    assert manifest["policy_input"]["task"]["id"] == "tests"
    serialized = json.loads(serialize_policy_input_for_opa(manifest["policy_input"]))
    assert serialized["policy_input"]["task"]["id"] == "tests"


def test_opa_policy_input_evaluation_stub_is_disabled_by_default():
    policy_input = {
        "schema_version": 1,
        "project": {"name": "agent-project"},
        "task": {"id": "tests"},
    }
    called = False

    def evaluator(_exported: dict) -> dict:
        nonlocal called
        called = True
        raise AssertionError("disabled OPA/Rego stub must not call evaluator")

    result = evaluate_opa_policy_input(policy_input, evaluator=evaluator)

    assert called is False
    assert result["enabled"] is False
    assert result["status"] == "disabled"
    assert result["authoritative"] is False
    assert result["authoritative_engine"] == "python"
    assert result["decision_mode"] == "disabled"
    assert result["decisions"] == []
    assert result["export"]["external_evaluation"]["enabled"] is False

    enabled_without_runtime = evaluate_opa_policy_input(policy_input, enabled=True)
    assert enabled_without_runtime["status"] == "not_configured"
    assert enabled_without_runtime["decisions"] == []


def test_manifest_index_lists_multiple_task_run_manifests_deterministically(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    first_task = roadmap["milestones"][0]["tasks"][0]
    first_task["acceptance"][0]["command"] = "python3 -c \"print('first')\""
    roadmap["milestones"][0]["tasks"].append(
        {
            "id": "zz-second",
            "title": "Second indexed task",
            "file_scope": ["tests/**"],
            "acceptance": [{"name": "second", "command": "python3 -c \"print('second')\""}],
        }
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    first_result = harness.run_task(harness.next_task())
    harness = Harness(project)
    second_result = harness.run_task(harness.next_task())

    index_path = project / ".engineering/reports/tasks/manifest-index.json"
    index = Harness(project).manifest_index()
    reread_index = Harness(project).manifest_index()
    on_disk_index = json.loads(index_path.read_text(encoding="utf-8"))

    assert first_result["status"] == "passed"
    assert second_result["status"] == "passed"
    assert index_path.exists()
    assert index == reread_index == on_disk_index
    assert index["kind"] == "engineering-harness.task-run-manifest-index"
    assert index["manifest_index_path"] == ".engineering/reports/tasks/manifest-index.json"
    assert index["manifest_count"] == 2
    assert index["status_counts"] == {"passed": 2}
    assert [item["task_id"] for item in index["manifests"]] == ["tests", "zz-second"]
    assert [item["manifest_path"] for item in index["manifests"]] == [
        first_result["manifest"],
        second_result["manifest"],
    ]
    assert index["latest_manifest"] == second_result["manifest"]
    assert index["latest_by_task"] == {
        "tests": first_result["manifest"],
        "zz-second": second_result["manifest"],
    }
    assert index["policy_decision_summary"]["by_kind"]["command_policy"] == 2
    assert index["policy_decision_summary"]["by_kind"]["file_scope_guard"] == 2
    assert Harness(project).status_summary()["manifest_index"]["manifest_count"] == 2


def test_custom_executor_registry_preserves_task_semantics_and_normalizes_result(tmp_path):
    class NoopExecutor:
        metadata = ExecutorMetadata(
            id="noop",
            name="Noop",
            kind="test",
            adapter="test.noop",
            input_mode="prompt",
            capabilities=("stdout",),
        )

        def display_command(self, invocation):
            return f"noop <task:{invocation.task_id}>"

        def execute(self, invocation):
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout=f"prompt={invocation.prompt}",
                stderr="",
                metadata={"adapter": "noop"},
            )

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "future adapter",
        "executor": "noop",
        "prompt": "preserve the roadmap command shape",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    registry = ExecutorRegistry((NoopExecutor(),))
    harness = Harness(project, executor_registry=registry)

    assert harness.validate_roadmap()["status"] == "passed"
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert result["runs"][0]["executor"] == "noop"
    assert result["runs"][0]["command"] == "noop <task:tests>"

    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    run = manifest["runs"][0]
    assert run["executor"] == "noop"
    assert run["executor_metadata"]["adapter"] == "test.noop"
    assert run["executor_metadata"]["input_mode"] == "prompt"
    assert run["executor_result"]["status"] == "passed"
    assert run["executor_result"]["metadata"] == {"adapter": "noop"}
    assert any(
        decision["kind"] == "executor_policy" and decision["executor"] == "noop" and decision["outcome"] == "allowed"
        for decision in manifest["policy_decisions"]
    )


def test_shell_executor_selection_uses_registered_adapter(tmp_path):
    executed = []

    class RecordingShellExecutor:
        metadata = ExecutorMetadata(
            id="shell",
            name="Recording Shell",
            kind="process",
            adapter="test.recording-shell",
            input_mode="command",
            capabilities=("stdout",),
            uses_command_policy=True,
        )

        def display_command(self, invocation):
            return f"recording-shell:{invocation.command}"

        def execute(self, invocation):
            executed.append(invocation)
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="shell adapter selected",
                stderr="",
                metadata={"selected": "shell"},
            )

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('not run directly')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project, executor_registry=ExecutorRegistry((RecordingShellExecutor(),)))
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert len(executed) == 1
    assert executed[0].command == "python3 -c \"print('not run directly')\""
    assert result["runs"][0]["executor"] == "shell"
    assert result["runs"][0]["command"] == "recording-shell:python3 -c \"print('not run directly')\""
    assert result["runs"][0]["executor_metadata"]["adapter"] == "test.recording-shell"
    assert result["runs"][0]["executor_result"]["metadata"] == {"selected": "shell"}


def test_codex_executor_selection_uses_registered_adapter_and_preparation(tmp_path):
    prepared = []
    executed = []

    class RecordingCodexExecutor:
        metadata = ExecutorMetadata(
            id="codex",
            name="Recording Codex",
            kind="agent",
            adapter="test.recording-codex",
            input_mode="prompt",
            capabilities=("stdout",),
            requires_agent_approval=True,
        )

        def prepare_invocation(self, invocation, task_context):
            prepared.append((invocation.prompt, task_context.task_id, task_context.acceptance[0].name))
            return replace(invocation, prompt=f"prepared:{task_context.task_id}:{invocation.prompt}")

        def display_command(self, invocation):
            return f"recording-codex <task:{invocation.task_id}>"

        def execute(self, invocation):
            executed.append(invocation)
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout=invocation.prompt or "",
                stderr="",
                metadata={"selected": "codex"},
            )

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "agent work",
        "executor": "codex",
        "prompt": "Do not change files.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project, executor_registry=ExecutorRegistry((RecordingCodexExecutor(),)))
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    assert prepared[0] == ("Do not change files.", "tests", "agent work")
    assert len(executed) == 1
    assert executed[0].prompt == "prepared:tests:Do not change files."
    assert result["runs"][0]["executor"] == "codex"
    assert result["runs"][0]["command"] == "recording-codex <task:tests>"
    assert result["runs"][0]["executor_metadata"]["adapter"] == "test.recording-codex"
    assert result["runs"][0]["executor_result"]["metadata"] == {"selected": "codex"}


def test_task_agent_developer_executor_is_discoverable_and_validates_command_payload(tmp_path):
    registry = default_executor_registry()
    assert "task-agent-developer" in registry.ids()
    metadata = registry.metadata_for("task-agent-developer")
    assert metadata["adapter"] == "builtin.task-agent-developer"
    assert metadata["kind"] == "agent"
    assert metadata["input_mode"] == "command"
    assert metadata["requires_agent_approval"] is True
    assert "local_task_agent_developer_cli" in metadata["capabilities"]
    assert "artifact_collection" in metadata["capabilities"]

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "task agent developer smoke",
        "executor": "task-agent-developer",
        "command": "develop --task tests",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert Harness(project).validate_roadmap()["status"] == "passed"

    roadmap["milestones"][0]["tasks"][0]["acceptance"][0].pop("command")
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    invalid = Harness(project).validate_roadmap()

    assert invalid["status"] == "failed"
    assert any("task-agent-developer command is required" in error for error in invalid["errors"])


def test_task_agent_developer_executor_collects_artifacts_and_redacts_manifest_evidence(tmp_path, monkeypatch):
    args_path = tmp_path / "task-agent-developer-args.json"
    task_agent_developer_bin = tmp_path / "task-agent-developer"
    task_agent_developer_bin.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "import pathlib",
                "import sys",
                "candidate = pathlib.Path('.engineering/reports/task-agent-developer/candidate.json')",
                "audit = pathlib.Path('artifacts/task-agent-developer/audit.json')",
                "report = pathlib.Path('.engineering/reports/task-agent-developer/report.md')",
                "for path in (candidate, audit, report):",
                "    path.parent.mkdir(parents=True, exist_ok=True)",
                "candidate.write_text(json.dumps({'candidate': 'ok'}), encoding='utf-8')",
                "audit.write_text(json.dumps({'audit': 'ok'}), encoding='utf-8')",
                "report.write_text('# Task Agent Developer Report\\n', encoding='utf-8')",
                "payload = {",
                "    'args': sys.argv[1:],",
                "    'cwd': os.getcwd(),",
                "    'engineering_harness': os.environ.get('ENGINEERING_HARNESS'),",
                "}",
                "pathlib.Path(os.environ['TASK_AGENT_DEVELOPER_ARGS_PATH']).write_text(json.dumps(payload))",
                "print(json.dumps({'artifacts': [",
                "    {'kind': 'candidate', 'path': str(candidate)},",
                "    {'kind': 'audit', 'path': str(audit)},",
                "    {'kind': 'report', 'path': str(report)},",
                "]}))",
                "print('OPENAI_API_KEY=sk-taskagentsecret12345')",
            ]
        ),
        encoding="utf-8",
    )
    task_agent_developer_bin.chmod(0o755)
    monkeypatch.setenv(TASK_AGENT_DEVELOPER_BINARY_ENV, str(task_agent_developer_bin))
    monkeypatch.setenv("TASK_AGENT_DEVELOPER_ARGS_PATH", str(args_path))

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "task agent developer smoke",
        "executor": "task-agent-developer",
        "command": "task-agent-developer develop --task tests",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task(), allow_agent=True)
    manifest = task_manifest(project, result)
    run = manifest["runs"][0]
    report_text = (project / result["report"]).read_text(encoding="utf-8")
    task_agent_metadata = run["executor_result"]["metadata"]["task_agent_developer"]
    artifacts = task_agent_metadata["artifacts"]
    artifact_kinds = {artifact["kind"] for artifact in artifacts}
    manifest_artifact_kinds = {artifact["kind"] for artifact in manifest["artifacts"]}
    payload = json.loads(args_path.read_text(encoding="utf-8"))

    assert result["status"] == "passed"
    assert payload["args"] == ["develop", "--task", "tests"]
    assert payload["cwd"] == str(project)
    assert payload["engineering_harness"] == "1"
    assert run["executor"] == "task-agent-developer"
    assert run["command"] == "task-agent-developer develop --task tests <task:tests>"
    assert run["executor_metadata"]["adapter"] == "builtin.task-agent-developer"
    assert run["executor_metadata"]["capabilities"] == [
        "agent",
        "local_task_agent_developer_cli",
        "workspace_write",
        "artifact_collection",
        "exit_code",
        "stdout",
        "stderr",
    ]
    assert run["executor_result"]["status"] == "passed"
    assert task_agent_metadata["artifact_count"] == 3
    assert artifact_kinds == {
        "task_agent_developer_candidate",
        "task_agent_developer_audit",
        "task_agent_developer_report",
    }
    assert all(artifact["exists"] is True and artifact["sha256"] for artifact in artifacts)
    assert run["artifacts"] == artifacts
    assert {
        "task_agent_developer_candidate",
        "task_agent_developer_audit",
        "task_agent_developer_report",
    }.issubset(manifest_artifact_kinds)
    assert "sk-taskagentsecret12345" not in json.dumps(manifest)
    assert "sk-taskagentsecret12345" not in report_text
    assert "OPENAI_API_KEY=[REDACTED]" in report_text


def test_dagger_executor_is_discoverable_and_validates_command_payload(tmp_path):
    registry = default_executor_registry()
    assert "dagger" in registry.ids()
    assert registry.metadata_for("dagger")["adapter"] == "builtin.dagger"

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "dagger smoke",
        "executor": "dagger",
        "command": "call test --source=.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert Harness(project).validate_roadmap()["status"] == "passed"

    roadmap["milestones"][0]["tasks"][0]["acceptance"][0].pop("command")
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    invalid = Harness(project).validate_roadmap()

    assert invalid["status"] == "failed"
    assert any("dagger command is required" in error for error in invalid["errors"])


def test_dagger_executor_selection_blocks_until_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv(DAGGER_ENABLE_ENV, raising=False)
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "dagger smoke",
        "executor": "dagger",
        "command": "call test --source=.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert DAGGER_ENABLE_ENV in result["message"]
    run = result["runs"][0]
    assert run["executor"] == "dagger"
    assert run["command"] == "dagger call test --source=. <task:tests>"
    assert run["executor_metadata"]["kind"] == "container"
    assert run["executor_metadata"]["capabilities"][-1] == "requires_explicit_configuration"
    assert run["executor_result"]["status"] == "blocked"
    assert run["executor_result"]["metadata"] == {
        "configured": False,
        "required_environment": DAGGER_ENABLE_ENV,
    }


def test_dagger_executor_invokes_local_cli_when_enabled(tmp_path, monkeypatch):
    args_path = tmp_path / "dagger-args.json"
    dagger_bin = tmp_path / "dagger"
    dagger_bin.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "import pathlib",
                "import sys",
                "pathlib.Path(os.environ['DAGGER_ARGS_PATH']).write_text(json.dumps(sys.argv[1:]))",
                "print('dagger ok')",
            ]
        ),
        encoding="utf-8",
    )
    dagger_bin.chmod(0o755)
    monkeypatch.setenv(DAGGER_ENABLE_ENV, "1")
    monkeypatch.setenv("DAGGER_ARGS_PATH", str(args_path))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    invocation = ExecutorInvocation(
        project_root=tmp_path,
        task_id="dagger-task",
        name="dagger smoke",
        command="dagger call smoke --source=.",
        prompt=None,
        timeout_seconds=15,
    )

    result = DaggerExecutorAdapter().execute(invocation)

    assert result.status == "passed"
    assert result.stdout == "dagger ok\n"
    assert result.metadata["configured"] is True
    assert result.metadata["watchdog"]["executor_id"] == "dagger"
    assert result.metadata["watchdog"]["status"] == "passed"
    assert json.loads(args_path.read_text(encoding="utf-8")) == ["call", "smoke", "--source=."]


def test_openhands_executor_is_discoverable_and_validates_prompt_payload(tmp_path):
    registry = default_executor_registry()
    assert "openhands" in registry.ids()
    metadata = registry.metadata_for("openhands")
    assert metadata["adapter"] == "builtin.openhands"
    assert metadata["requires_agent_approval"] is True
    assert "local_openhands_cli" in metadata["capabilities"]

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "openhands smoke",
        "executor": "openhands",
        "prompt": "Inspect the project and report blockers.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert Harness(project).validate_roadmap()["status"] == "passed"

    roadmap["milestones"][0]["tasks"][0]["acceptance"][0].pop("prompt")
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    invalid = Harness(project).validate_roadmap()

    assert invalid["status"] == "failed"
    assert any("openhands prompt is required" in error for error in invalid["errors"])


def test_openhands_executor_selection_blocks_until_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv(OPENHANDS_ENABLE_ENV, raising=False)
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "openhands smoke",
        "executor": "openhands",
        "prompt": "Do not change files.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "blocked"
    assert OPENHANDS_ENABLE_ENV in result["message"]
    run = result["runs"][0]
    assert run["executor"] == "openhands"
    assert run["command"] == "openhands --headless --json --override-with-envs -t <task:tests>"
    assert run["executor_metadata"]["kind"] == "agent"
    assert "browser_automation" in run["executor_metadata"]["capabilities"]
    assert run["executor_result"]["status"] == "blocked"
    metadata = run["executor_result"]["metadata"]
    assert metadata["configured"] is False
    assert metadata["required_environment"] == OPENHANDS_ENABLE_ENV
    assert metadata["health"]["binary"] == "openhands"
    assert "binary_found" in metadata["health"]


def test_openhands_unsafe_executor_capabilities_are_audited_without_blocking_config_gate(tmp_path, monkeypatch):
    monkeypatch.delenv(OPENHANDS_ENABLE_ENV, raising=False)
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "openhands smoke",
        "executor": "openhands",
        "prompt": "Do not change files.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task(), allow_agent=True)

    assert result["status"] == "blocked"
    assert OPENHANDS_ENABLE_ENV in result["message"]
    manifest = task_manifest(project, result)
    warning = policy_decision(manifest, "capability_policy", outcome="warning")
    assert warning["effect"] == "warn"
    assert warning["severity"] == "warning"
    assert warning["metadata"]["executor_unsafe_classes"] == ["network"]
    assert warning["metadata"]["executor_unsafe_capabilities"] == ["network_access", "browser_automation"]
    assert manifest["safety_audit"]["unsafe_decision_count"] == 1
    assert manifest["safety_audit"]["unsafe_classes"] == ["network"]
    assert manifest["safety_audit"]["unsafe_capabilities"] == ["browser_automation", "network_access"]


def test_openhands_executor_invokes_local_cli_when_enabled(tmp_path, monkeypatch):
    args_path = tmp_path / "openhands-args.json"
    openhands_bin = tmp_path / "openhands"
    openhands_bin.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "import pathlib",
                "import sys",
                "payload = {",
                "    'args': sys.argv[1:],",
                "    'cwd': os.getcwd(),",
                "    'llm_model': os.environ.get('LLM_MODEL'),",
                "    'engineering_harness': os.environ.get('ENGINEERING_HARNESS'),",
                "}",
                "pathlib.Path(os.environ['OPENHANDS_ARGS_PATH']).write_text(json.dumps(payload))",
                "print(json.dumps({'type': 'action', 'action': 'write', 'path': 'app.py'}))",
                "print(json.dumps({'type': 'observation', 'status': 'ok', 'message': 'done sk-progress-secret'}))",
                "print('plain status line')",
            ]
        ),
        encoding="utf-8",
    )
    openhands_bin.chmod(0o755)
    monkeypatch.setenv(OPENHANDS_ENABLE_ENV, "1")
    monkeypatch.setenv("OPENHANDS_ARGS_PATH", str(args_path))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    progress_events = []
    invocation = ExecutorInvocation(
        project_root=tmp_path,
        task_id="openhands-task",
        name="agent smoke",
        command=None,
        prompt="Inspect the project.",
        timeout_seconds=15,
        model="anthropic/test-model",
        progress_callback=progress_events.append,
    )

    result = OpenHandsExecutorAdapter().execute(invocation)

    assert result.status == "passed"
    assert result.stdout.splitlines()[-1] == "plain status line"
    assert result.metadata["configured"] is True
    assert result.metadata["binary"] == "openhands"
    assert result.metadata["health"]["binary_found"] is True
    assert result.metadata["health"]["llm_model_configured"] is True
    assert result.metadata["watchdog"]["executor_id"] == "openhands"
    assert result.metadata["watchdog"]["status"] == "passed"
    assert result.metadata["openhands_jsonl"]["parsed_event_count"] == 2
    assert result.metadata["openhands_jsonl"]["non_json_line_count"] == 1
    assert result.metadata["openhands_jsonl"]["event_counts"] == {"action": 1, "observation": 1}
    assert result.metadata["openhands_jsonl"]["touched_paths"] == ["app.py"]
    assert result.metadata["openhands_jsonl"]["recent_events"][0]["action"] == "write"
    assert result.metadata["openhands_jsonl"]["recent_events"][1]["message"] == "done [REDACTED]"
    executor_events = [
        event["executor_event"]
        for event in progress_events
        if event.get("event") == "executor_event" and isinstance(event.get("executor_event"), dict)
    ]
    assert [event["type"] for event in executor_events] == ["action", "observation"]
    assert executor_events[0]["source"] == "openhands_jsonl"
    assert executor_events[0]["path"] == "app.py"
    assert executor_events[1]["message"] == "done [REDACTED]"
    assert "sk-progress-secret" not in json.dumps(progress_events)
    payload = json.loads(args_path.read_text(encoding="utf-8"))
    assert payload["args"] == [
        "--headless",
        "--json",
        "--override-with-envs",
        "-t",
        "Inspect the project.",
    ]
    assert payload["cwd"] == str(tmp_path)
    assert payload["llm_model"] == "anthropic/test-model"
    assert payload["engineering_harness"] == "1"


def test_openhands_executor_reports_missing_local_cli_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENHANDS_ENABLE_ENV, "1")
    monkeypatch.setenv("PATH", str(tmp_path))
    invocation = ExecutorInvocation(
        project_root=tmp_path,
        task_id="openhands-task",
        name="agent smoke",
        command=None,
        prompt="Inspect the project.",
        timeout_seconds=15,
        environment={"ENGINEERING_HARNESS_OPENHANDS_BINARY": "missing-openhands"},
    )

    result = OpenHandsExecutorAdapter().execute(invocation)

    assert result.status == "blocked"
    assert result.returncode is None
    assert result.metadata["configured"] is True
    assert result.metadata["missing_binary"] == "missing-openhands"
    assert result.metadata["health"]["binary"] == "missing-openhands"
    assert result.metadata["health"]["binary_found"] is False


def test_executor_diagnostics_status_reports_openhands_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(OPENHANDS_ENABLE_ENV, raising=False)
    monkeypatch.delenv(OPENHANDS_BINARY_ENV, raising=False)
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")

    status = Harness(project).status_summary()
    diagnostics = status["executor_diagnostics"]
    openhands = next(item for item in diagnostics["executors"] if item["id"] == "openhands")

    assert diagnostics["executor_count"] >= 4
    assert diagnostics["action_required_count"] >= 1
    assert openhands["status"] == "disabled"
    assert openhands["configured"] is False
    assert openhands["diagnostics"]["required_environment"] == OPENHANDS_ENABLE_ENV
    assert openhands["unsafe_capabilities"] == ["network_access", "browser_automation"]
    assert openhands["unsafe_classes"] == ["network"]
    assert status["runtime_dashboard"]["executor_diagnostics"] == diagnostics


def test_executor_diagnostics_status_reports_openhands_missing_binary_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENHANDS_ENABLE_ENV, "1")
    monkeypatch.setenv(OPENHANDS_BINARY_ENV, "missing-openhands-for-diagnostics")
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")

    diagnostics = Harness(project).status_summary()["executor_diagnostics"]
    openhands = next(item for item in diagnostics["executors"] if item["id"] == "openhands")

    assert openhands["status"] == "missing_binary"
    assert openhands["configured"] is False
    assert openhands["enabled"] is True
    assert openhands["diagnostics"]["health"]["binary"] == "missing-openhands-for-diagnostics"
    assert openhands["diagnostics"]["health"]["binary_found"] is False
    assert "Install OpenHands CLI" in openhands["diagnostics"]["recommended_action"]


def test_executor_diagnostics_status_reports_openhands_ready_with_model_env(tmp_path, monkeypatch):
    openhands_bin = tmp_path / "openhands"
    openhands_bin.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    openhands_bin.chmod(0o755)
    monkeypatch.setenv(OPENHANDS_ENABLE_ENV, "1")
    monkeypatch.setenv(OPENHANDS_BINARY_ENV, str(openhands_bin))
    monkeypatch.setenv("LLM_MODEL", "anthropic/test-model")
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")

    diagnostics = Harness(project).status_summary()["executor_diagnostics"]
    openhands = next(item for item in diagnostics["executors"] if item["id"] == "openhands")

    assert openhands["status"] == "ready"
    assert openhands["configured"] is True
    assert openhands["enabled"] is True
    assert openhands["diagnostics"]["health"]["binary_found"] is True
    assert openhands["diagnostics"]["health"]["binary_path"] == str(openhands_bin)
    assert openhands["diagnostics"]["health"]["llm_model_configured"] is True
    assert openhands["diagnostics"]["warnings"] == []


def test_openhands_jsonl_progress_is_persisted_to_drive_control(tmp_path, monkeypatch):
    openhands_bin = tmp_path / "openhands"
    openhands_bin.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "print(json.dumps({'type': 'action', 'action': 'write', 'path': 'src/app.py'}))",
                "print(json.dumps({'type': 'observation', 'status': 'ok', 'message': 'finished sk-drive-secret'}))",
            ]
        ),
        encoding="utf-8",
    )
    openhands_bin.chmod(0o755)
    monkeypatch.setenv(OPENHANDS_ENABLE_ENV, "1")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "openhands smoke",
        "executor": "openhands",
        "prompt": "Do not change files.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    state = harness.load_state()
    control = harness._drive_control(state)
    now = utc_now()
    control.update(
        {
            "status": "running",
            "active": True,
            "pid": os.getpid(),
            "started_at": now,
            "last_heartbeat_at": now,
        }
    )
    harness.save_state(state)

    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    state = harness.load_state()
    control = state["drive_control"]
    assert control["executor_event_count"] >= 2
    assert control["latest_executor_event"]["type"] == "observation"
    assert control["latest_executor_event"]["message"] == "finished [REDACTED]"
    assert control["executor_event_history"][-2]["path"] == "src/app.py"
    assert "sk-drive-secret" not in json.dumps(control)
    status_payload = Harness(project).status_summary()
    dashboard_drive = status_payload["runtime_dashboard"]["drive_control"]
    assert dashboard_drive["latest_executor_event"]["type"] == "observation"
    assert dashboard_drive["executor_event_count"] >= 2


def test_manifest_index_keeps_repeated_task_runs_with_same_slug(tmp_path, monkeypatch):
    monkeypatch.setattr("engineering_orchestrator.core.slug_now", lambda: "20240101T000000Z")
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('repeat')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    task = harness.next_task()
    first_result = harness.run_task(task)
    second_result = Harness(project).run_task(task)
    index = Harness(project).manifest_index()

    assert first_result["status"] == "passed"
    assert second_result["status"] == "passed"
    assert first_result["manifest"] == ".engineering/reports/tasks/20240101T000000Z-tests.json"
    assert second_result["manifest"] == ".engineering/reports/tasks/20240101T000000Z-tests_2.json"
    assert [item["manifest_path"] for item in index["manifests"]] == [
        first_result["manifest"],
        second_result["manifest"],
    ]
    assert [item["attempt"] for item in index["manifests"]] == [1, 2]
    assert index["latest_by_task"] == {"tests": second_result["manifest"]}


def test_harness_blocks_non_allowlisted_command(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "curl https://example.com"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert "allowlisted" in result["message"]


def test_policy_decision_schema_records_denied_command(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "curl https://example.com"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    capability_policy = policy_decision(manifest, "capability_policy", outcome="warning")
    assert capability_policy["effect"] == "warn"
    assert capability_policy["metadata"]["detected_capabilities"] == ["network_access"]
    assert capability_policy["metadata"]["unsafe_classes"] == ["network"]
    assert capability_policy["metadata"]["command_policy_blocked_detected_capabilities"] is True
    command_policy = policy_decision(manifest, "command_policy", outcome="denied")
    assert command_policy["effect"] == "deny"
    assert command_policy["severity"] == "error"
    assert command_policy["reason"] == "command prefix is not allowlisted"
    assert command_policy["input"]["command"]["command"] == "curl https://example.com"
    assert command_policy["input"]["executor"]["id"] == "shell"
    assert "python3 " in command_policy["metadata"]["allowed_prefixes"]
    assert manifest["policy_decision_summary"]["blocking"][0]["kind"] == "command_policy"
    assert manifest["policy_decision_summary"]["blocking"][0]["reason"] == "command prefix is not allowlisted"
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    assert manifest["safety_audit"]["unsafe_classes"] == ["network"]
    assert manifest["safety_audit"]["unsafe_capabilities"] == ["network_access"]
    index = Harness(project).manifest_index()
    assert index["policy_decision_summary"]["blocking"][0]["kind"] == "command_policy"


def test_policy_decision_schema_records_live_command_approval_gate(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('live')\" --live"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    command_policy = policy_decision(manifest, "command_policy", outcome="requires_approval")
    assert command_policy["effect"] == "requires_approval"
    assert command_policy["severity"] == "approval"
    assert command_policy["requires_approval"] is True
    assert command_policy["approval_flag"] == "--allow-live"
    assert command_policy["input"]["live"]["detected"] is True
    assert command_policy["input"]["live"]["matched_patterns"] == ["--live"]
    live = policy_decision(manifest, "live_approval", outcome="requires_approval")
    assert live["effect"] == "requires_approval"
    assert live["severity"] == "approval"
    assert live["requires_approval"] is True
    assert live["approval_flag"] == "--allow-live"
    assert live["metadata"]["matched_live_patterns"] == ["--live"]
    assert [item["kind"] for item in manifest["policy_decision_summary"]["requires_approval"]] == [
        "command_policy",
        "live_approval",
    ]
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]


def test_capability_policy_allows_requested_shell_executor_capabilities(tmp_path):
    project = tmp_path / "capability-allowed-project"
    project.mkdir()
    init_project(project, "python-agent", name="capability-allowed-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    command = roadmap["milestones"][0]["tasks"][0]["acceptance"][0]
    command["command"] = "python3 -c \"print('capability ok')\""
    command["requested_capabilities"] = ["local_process", "workspace_write", "stdout", "stderr", "exit_code"]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.validate_roadmap()["status"] == "passed"
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert result["runs"][0]["requested_capabilities"] == command["requested_capabilities"]
    assert set(command["requested_capabilities"]).issubset(set(result["runs"][0]["executor_capabilities"]))
    manifest = task_manifest(project, result)
    run = manifest["runs"][0]
    assert run["requested_capabilities"] == command["requested_capabilities"]
    assert set(command["requested_capabilities"]).issubset(set(run["executor_metadata"]["capabilities"]))
    decision = policy_decision(manifest, "capability_policy", outcome="allowed")
    assert decision["effect"] == "allow"
    assert decision["reason"] == "requested executor capabilities are supported"
    assert decision["metadata"]["requested_capabilities"] == command["requested_capabilities"]


def test_capability_policy_denies_unsupported_executor_capability(tmp_path):
    project = tmp_path / "capability-unsupported-project"
    project.mkdir()
    init_project(project, "python-agent", name="capability-unsupported-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    command = roadmap["milestones"][0]["tasks"][0]["acceptance"][0]
    command["command"] = "python3 -c \"from pathlib import Path; Path('should-not-run').write_text('x')\""
    command["requested_capabilities"] = ["agent"]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.validate_roadmap()["status"] == "passed"
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert "does not support requested capabilities" in result["message"]
    assert not (project / "should-not-run").exists()
    manifest = task_manifest(project, result)
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    assert decision["effect"] == "deny"
    assert decision["metadata"]["requested_capabilities"] == ["agent"]
    assert decision["metadata"]["unsupported_capabilities"] == ["agent"]
    assert manifest["policy_decision_summary"]["blocking"][0]["kind"] == "capability_policy"


@pytest.mark.parametrize(
    ("unsafe_capability", "expected_class"),
    [
        ("network", "network"),
        ("secret_access", "secret"),
        ("browser_automation", "network"),
        ("deployment", "deploy"),
        ("live_operations", "deploy"),
    ],
)
def test_capability_policy_denies_unsafe_executor_capability_requests(tmp_path, unsafe_capability, expected_class):
    project = tmp_path / f"capability-unsafe-{unsafe_capability}"
    project.mkdir()
    init_project(project, "python-agent", name=f"capability-unsafe-{unsafe_capability}")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    command = roadmap["milestones"][0]["tasks"][0]["acceptance"][0]
    command["command"] = "python3 -c \"print('unsafe capability should not run')\""
    command["requested_capabilities"] = [unsafe_capability]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    assert decision["effect"] == "deny"
    assert decision["metadata"]["unsafe_capabilities"] == [unsafe_capability]
    assert decision["metadata"]["unsafe_classes"] == [expected_class]
    assert (
        decision["metadata"]["unsafe_capability_classifications"]["core_classes"][expected_class]["supported"]
        is True
    )
    assert "not locally approvable" in decision["reason"]
    assert manifest["safety_audit"]["unsafe_classes"] == [expected_class]
    assert manifest["safety_audit"]["unsafe_capabilities"] == [unsafe_capability]
    assert manifest["policy_decision_summary"]["requires_approval"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], "non-empty list"),
        ("local_process", "non-empty list"),
        ([""], "non-empty string"),
        (["unknown_capability"], "unknown capability `unknown_capability`"),
    ],
)
def test_requested_capability_validation_errors(tmp_path, value, expected):
    project = tmp_path / "capability-validation-project"
    project.mkdir()
    init_project(project, "python-agent", name="capability-validation-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    command = roadmap["milestones"][0]["tasks"][0]["acceptance"][0]
    command["command"] = "python3 -c \"print('validation')\""
    command["requested_capabilities"] = value
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).validate_roadmap()

    assert result["status"] == "failed"
    assert any(expected in error for error in result["errors"])


def test_capability_policy_manifest_report_and_status_evidence(tmp_path):
    project = tmp_path / "capability-policy-manifest-project"
    project.mkdir()
    init_project(project, "python-agent", name="capability-policy-manifest-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    command = roadmap["milestones"][0]["tasks"][0]["acceptance"][0]
    command["command"] = "python3 -c \"print('blocked')\""
    command["requested_capabilities"] = ["agent"]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    run = manifest["runs"][0]
    assert run["requested_capabilities"] == ["agent"]
    assert "local_process" in run["executor_capabilities"]
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    assert decision["metadata"]["executor_capabilities"] == run["executor_capabilities"]
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    report_text = (project / result["report"]).read_text(encoding="utf-8")
    assert "Requested capabilities" in report_text
    assert "capability_policy" in report_text

    status = Harness(project).status_summary()
    assert status["capability_policy"]["blocking_count"] == 1
    assert status["capability_policy"]["blocking"][0]["kind"] == "capability_policy"
    assert status["runtime_dashboard"]["capability_policy"]["blocking_count"] == 1
    assert status["manifest_index"]["policy_decision_summary"]["blocking"][0]["kind"] == "capability_policy"


def test_capability_policy_existing_tasks_unchanged_without_requests(tmp_path):
    project = tmp_path / "capability-backcompat-project"
    project.mkdir()
    init_project(project, "python-agent", name="capability-backcompat-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task())

    assert result["status"] == "passed"
    manifest = task_manifest(project, result)
    assert all(decision["kind"] != "capability_policy" for decision in manifest["policy_decisions"])
    assert manifest["runs"][0]["requested_capabilities"] == []
    assert manifest["policy_decision_summary"]["total"] == 8


def unsafe_policy_project(tmp_path: Path, command_text: str, *, sandbox: str = "workspace-write") -> tuple[Path, dict]:
    project = tmp_path / "unsafe-policy-project"
    project.mkdir()
    init_project(project, "python-agent", name="unsafe-policy-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    command = roadmap["milestones"][0]["tasks"][0]["acceptance"][0]
    command["command"] = command_text
    command["sandbox"] = sandbox
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    result = Harness(project).run_task(Harness(project).next_task())
    return project, result


def test_policy_blocks_network_command_without_requested_capability(tmp_path):
    project, result = unsafe_policy_project(
        tmp_path,
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.invalid')\"",
    )

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    assert decision["metadata"]["detected_capabilities"] == ["network_access"]
    assert decision["metadata"]["unsafe_classes"] == ["network"]
    assert decision["metadata"]["operation_classification"]["matches"]["network"]


def test_policy_blocks_secret_env_access_and_redacts_reports(tmp_path):
    secret_value = "sk-secret-value-that-must-not-leak"
    project, result = unsafe_policy_project(
        tmp_path,
        f"python3 -c \"import os; print(os.environ.get('OPENAI_API_KEY', '{secret_value}'))\"",
    )

    assert result["status"] == "blocked"
    manifest_text = (project / result["manifest"]).read_text(encoding="utf-8")
    report_text = (project / result["report"]).read_text(encoding="utf-8")
    assert secret_value not in manifest_text
    assert secret_value not in report_text
    assert "[REDACTED]" in manifest_text
    assert "[REDACTED]" in report_text
    manifest = json.loads(manifest_text)
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    assert decision["metadata"]["detected_capabilities"] == ["secret_access"]
    assert decision["metadata"]["unsafe_classes"] == ["secret"]


def test_redact_evidence_redacts_structured_sensitive_env_values():
    payload = {
        "OPENAI_API_KEY": "plain-env-secret",
        "nested": {"db_password": "plain-db-secret"},
        "llm_api_key_configured": True,
        "safe": "plain text",
    }

    redacted = redact_evidence(payload)

    assert redacted["OPENAI_API_KEY"] == "[REDACTED]"
    assert redacted["nested"]["db_password"] == "[REDACTED]"
    assert redacted["llm_api_key_configured"] is True
    assert redacted["safe"] == "plain text"


def test_policy_blocks_deploy_command_without_requested_capability(tmp_path):
    project, result = unsafe_policy_project(
        tmp_path,
        "python3 -c \"print('publish')\" && npm publish",
    )

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    assert decision["metadata"]["detected_capabilities"] == ["deployment"]
    assert decision["metadata"]["unsafe_classes"] == ["deploy"]


def test_policy_blocks_unsafe_sandbox_mode(tmp_path):
    project, result = unsafe_policy_project(
        tmp_path,
        "python3 -c \"print('sandbox should not run')\"",
        sandbox="danger-full-access",
    )

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    decision = policy_decision(manifest, "capability_policy", outcome="denied")
    classification = decision["metadata"]["operation_classification"]
    assert decision["metadata"]["detected_capabilities"] == ["filesystem_escape", "host_filesystem_write"]
    assert decision["metadata"]["unsafe_classes"] == ["filesystem"]
    assert classification["sandbox"]["unsafe"] is True
    assert classification["sandbox"]["mode"] == "danger-full-access"


def test_policy_records_local_audit_evidence_for_unsafe_capability_block(tmp_path):
    project, result = unsafe_policy_project(
        tmp_path,
        "python3 -c \"import socket; socket.socket()\"",
    )

    manifest = task_manifest(project, result)
    assert manifest["safety_audit"]["deny_by_default"] is True
    assert manifest["safety_audit"]["unsafe_decision_count"] == 1
    assert manifest["safety_audit"]["unsafe_classes"] == ["network"]
    report_text = (project / result["report"]).read_text(encoding="utf-8")
    assert "## Safety Audit" in report_text
    status = Harness(project).status_summary()
    assert status["safety_audit"]["unsafe_decision_count"] == 1
    decision_log = (project / ".engineering/state/decision-log.jsonl").read_text(encoding="utf-8")
    assert "safety_audit" in decision_log


def test_unsafe_capability_blocked_e2e(tmp_path):
    project, result = unsafe_policy_project(
        tmp_path,
        "python3 -c \"from pathlib import Path; Path('unsafe-e2e-ran.txt').write_text('bad'); import requests\"",
    )

    assert result["status"] == "blocked"
    assert not (project / "unsafe-e2e-ran.txt").exists()
    manifest = task_manifest(project, result)
    run = manifest["runs"][0]
    assert run["status"] == "blocked"
    assert run["safety_classification"]["unsafe_classes"] == ["network"]
    assert policy_decision(manifest, "capability_policy", outcome="denied")["metadata"][
        "unsafe_capabilities"
    ] == ["network_access"]


def test_policy_decision_schema_records_manual_approval_gate(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["manual_approval_required"] = True
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    manual = policy_decision(manifest, "manual_approval", outcome="requires_approval")
    assert manual["effect"] == "requires_approval"
    assert manual["requires_approval"] is True
    assert manual["approval_flag"] == "--allow-manual"
    assert manual["input"]["approvals"]["manual_required"] is True
    assert manual["input"]["approvals"]["allow_manual"] is False
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]


def test_harness_runs_implementation_before_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["implementation"] = [
        {
            "name": "write implementation marker",
            "command": "python3 -c \"from pathlib import Path; Path('implemented.txt').write_text('ok')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; assert Path('implemented.txt').read_text() == 'ok'\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    assert [run["phase"] for run in result["runs"]] == ["implementation", "acceptance-1"]


def test_harness_can_repair_after_failed_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_task_iterations"] = 2
    task["repair"] = [
        {
            "name": "repair marker",
            "command": "python3 -c \"from pathlib import Path; Path('repair.txt').write_text('fixed')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; assert Path('repair.txt').read_text() == 'fixed'\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    phases = [run["phase"] for run in result["runs"]]
    assert phases == ["acceptance-1", "repair-1", "acceptance-2"]


def test_failure_isolation_records_acceptance_failure_after_repair(tmp_path):
    project = tmp_path / "failure-isolation-project"
    project.mkdir()
    init_project(project, "python-agent", name="failure-isolation-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["max_task_iterations"] = 2
    task["repair"] = [
        {
            "name": "repair marker",
            "command": "python3 -c \"from pathlib import Path; Path('repair.txt').write_text('fixed')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"raise SystemExit(3)\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "failed"
    assert [run["phase"] for run in result["runs"]] == ["acceptance-1", "repair-1", "acceptance-2"]
    manifest = task_manifest(project, result)
    isolation = manifest["failure_isolation"]
    assert result["failure_isolation"] == isolation
    assert isolation["schema_version"] == 1
    assert isolation["task_id"] == "tests"
    assert isolation["status"] == "failed"
    assert isolation["phase"] == "acceptance-2"
    assert isolation["failure_kind"] == "acceptance_failure"
    assert isolation["retry_exhausted"] is True
    assert isolation["retry_exhaustion"]["repair_iteration_exhausted"] is True
    assert isolation["retry_exhaustion"]["task_attempt_exhausted"] is True
    assert isolation["report_paths"]["task_report"] == result["report"]
    assert isolation["report_paths"]["task_manifest"] == result["manifest"]
    assert isolation["relevant_report_paths"] == [result["report"], result["manifest"]]
    assert isolation["blocking_policy_decisions"] == []
    assert isolation["file_scope_violations"] == []
    assert "acceptance-2" in isolation["local_next_action"]

    summary = Harness(project).status_summary()["failure_isolation"]
    assert summary["unresolved_count"] == 1
    assert summary["latest_isolated_failures"][0]["task_id"] == "tests"
    assert summary["latest_isolated_failures"][0]["manifest_path"] == result["manifest"]


def test_acceptance_failure_diagnostics_preserve_fast_failure_details(tmp_path):
    project = tmp_path / "acceptance-diagnostics-project"
    project.mkdir()
    init_project(project, "python-agent", name="acceptance-diagnostics-project")
    missing_module = "definitely_missing_dependency_eo004"
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["acceptance"][0] = {
        "name": "fast missing dependency",
        "command": (
            "python3 -c \"import sys; "
            "print('stdout tail marker'); "
            "print('stderr tail marker', file=sys.stderr); "
            f"import {missing_module}\""
        ),
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task())
    manifest = task_manifest(project, result)
    run = result["runs"][0]
    diagnostics = run["diagnostics"]

    assert result["status"] == "failed"
    assert diagnostics["command"] == task["acceptance"][0]["command"]
    assert diagnostics["cwd"] == str(project)
    assert diagnostics["returncode"] == 1
    assert "stdout tail marker" in diagnostics["stdout_tail"]
    assert "stderr tail marker" in diagnostics["stderr_tail"]
    assert missing_module in diagnostics["stderr_tail"]
    assert diagnostics["missing_dependency_hints"][0]["kind"] == "python_module"
    assert diagnostics["missing_dependency_hints"][0]["name"] == missing_module
    assert manifest["runs"][0]["diagnostics"] == diagnostics
    assert manifest["failure_isolation"]["acceptance_diagnostics"]["failed_commands"][0]["command"] == diagnostics["command"]
    assert "stdout tail marker" in manifest["failure_isolation"]["acceptance_diagnostics"]["repair_prompt_input_summary"]

    report_text = (project / result["report"]).read_text(encoding="utf-8")
    assert "Acceptance diagnostics:" in report_text
    assert "missing_dependency_hints" in report_text


def test_repair_agent_prompt_consumes_acceptance_diagnostics(tmp_path):
    captured: dict[str, str] = {}
    missing_module = "definitely_missing_dependency_eo004"

    class RecordingCodexExecutor:
        metadata = ExecutorMetadata(
            id="codex",
            name="Recording Codex",
            kind="agent",
            adapter="test.recording-codex",
            input_mode="prompt",
            capabilities=("agent", "workspace_write", "stdout", "stderr"),
            requires_agent_approval=True,
        )

        def prepare_invocation(self, invocation, task_context):
            return CodexExecutorAdapter().prepare_invocation(invocation, task_context)

        def display_command(self, invocation):
            return f"recording-codex <task:{invocation.task_id}>"

        def execute(self, invocation):
            captured["prompt"] = invocation.prompt or ""
            captured["context_pack_path"] = invocation.context_pack["path"]
            (invocation.project_root / f"{missing_module}.py").write_text("VALUE = 'fixed'\n", encoding="utf-8")
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="repair ok",
                stderr="",
                metadata={},
            )

    project = tmp_path / "repair-diagnostics-project"
    project.mkdir()
    init_project(project, "python-agent", name="repair-diagnostics-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_task_iterations"] = 2
    task["repair"] = [
        {
            "name": "agent repair from diagnostics",
            "executor": "codex",
            "prompt": "Repair the failing acceptance command using the diagnostic summary.",
        }
    ]
    task["acceptance"][0] = {
        "name": "imports repaired dependency",
        "command": (
            "python3 -c \"import sys; "
            "print('stdout tail repair'); "
            "print('stderr tail repair', file=sys.stderr); "
            f"import {missing_module}\""
        ),
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(
        project,
        executor_registry=ExecutorRegistry((RecordingCodexExecutor(), ShellExecutorAdapter())),
    ).run_task(Harness(project).next_task(), allow_agent=True)
    context_pack = json.loads((project / captured["context_pack_path"]).read_text(encoding="utf-8"))
    prompt = captured["prompt"]

    assert result["status"] == "passed"
    assert [run["phase"] for run in result["runs"]] == ["acceptance-1", "repair-1", "acceptance-2"]
    assert "Repair prompt input summary:" in prompt
    assert task["acceptance"][0]["command"] in prompt
    assert str(project) in prompt
    assert "returned `1`" in prompt
    assert "stdout tail repair" in prompt
    assert "stderr tail repair" in prompt
    assert missing_module in prompt
    assert "missing dependency" in prompt
    assert context_pack["repair_prompt_input"]["failed_commands"][0]["name"] == "imports repaired dependency"
    assert context_pack["repair_prompt_input"]["missing_dependency_hints"][0]["name"] == missing_module


def test_failure_isolation_records_policy_block(tmp_path):
    project = tmp_path / "failure-isolation-policy-project"
    project.mkdir()
    init_project(project, "python-agent", name="failure-isolation-policy-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "curl https://example.com"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    isolation = manifest["failure_isolation"]
    assert isolation["task_id"] == "tests"
    assert isolation["phase"] == "acceptance-1"
    assert isolation["failure_kind"] == "policy_block"
    assert isolation["retry_exhausted"] is False
    assert isolation["blocking_policy_decisions"][0]["kind"] == "command_policy"
    assert isolation["blocking_policy_decisions"][0]["reason"] == "command prefix is not allowlisted"
    assert "blocking policy" in isolation["local_next_action"]


def test_failure_isolation_records_file_scope_violation(tmp_path):
    project = tmp_path / "failure-isolation-file-scope-project"
    project.mkdir()
    init_project(project, "python-agent", name="failure-isolation-file-scope-project")
    init_git_repo(project)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "write unscoped file",
            "command": "python3 -c \"from pathlib import Path; Path('outside.txt').write_text('outside')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('acceptance ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "failed"
    manifest = task_manifest(project, result)
    isolation = manifest["failure_isolation"]
    assert isolation["phase"] == "file-scope-guard"
    assert isolation["failure_kind"] == "file_scope_violation"
    assert isolation["file_scope_violations"] == ["outside.txt"]
    assert isolation["blocking_policy_decisions"][0]["kind"] == "file_scope_guard"
    assert "file-scope violations" in isolation["local_next_action"]


def test_status_json_includes_latest_isolated_failures(tmp_path, capsys):
    project = tmp_path / "isolated-failure-status-project"
    project.mkdir()
    init_project(project, "python-agent", name="isolated-failure-status-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["acceptance"][0]["command"] = "python3 -c \"raise SystemExit(4)\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    result = Harness(project).run_task(Harness(project).next_task())

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    isolation = payload["failure_isolation"]
    assert isolation["schema_version"] == 1
    assert isolation["unresolved_count"] == 1
    latest = isolation["latest_isolated_failures"][0]
    assert latest["task_id"] == "tests"
    assert latest["status"] == "failed"
    assert latest["failure_kind"] == "acceptance_failure"
    assert latest["manifest_path"] == result["manifest"]
    assert latest["report_path"] == result["report"]


def test_executor_no_progress_watchdog_isolates_silent_acceptance_and_status_json(
    tmp_path,
    monkeypatch,
    capsys,
):
    project = tmp_path / "executor-no-progress-project"
    project.mkdir()
    init_project(project, "python-agent", name="executor-no-progress-project")
    (project / "silent_parent.py").write_text(
        "\n".join(
            [
                "import subprocess",
                "import sys",
                "import time",
                "child = \"import pathlib, time; time.sleep(2); pathlib.Path('child-survived.txt').write_text('bad')\"",
                "subprocess.Popen([sys.executable, '-c', child])",
                "time.sleep(30)",
            ]
        ),
        encoding="utf-8",
    )
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["acceptance"][0] = {
        "name": "silent acceptance",
        "command": "python3 silent_parent.py",
        "timeout_seconds": 10,
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    monkeypatch.setenv("ENGINEERING_HARNESS_EXECUTOR_NO_PROGRESS_ACCEPTANCE_SECONDS", "1")

    result = Harness(project).run_task(Harness(project).next_task())

    assert result["status"] == "failed"
    run = result["runs"][0]
    assert run["phase"] == "acceptance-1"
    assert run["status"] == "no_progress"
    assert run["returncode"] is None
    watchdog = run["executor_result"]["metadata"]["watchdog"]
    assert watchdog["status"] == "no_progress"
    assert watchdog["phase"] == "acceptance-1"
    assert watchdog["executor_id"] == "shell"
    assert watchdog["command_name"] == "silent acceptance"
    assert watchdog["pid"] > 0
    assert watchdog["no_progress_timeout_seconds"] == 1
    assert watchdog["threshold_seconds"] == 1
    assert watchdog["last_output_at"] == watchdog["started_at"]
    assert watchdog["termination"]["owned_process_group"] is True
    assert watchdog["termination"]["terminated_process_group"] is True

    time.sleep(2.2)
    assert not (project / "child-survived.txt").exists()

    manifest = task_manifest(project, result)
    manifest_run = manifest["runs"][0]
    assert manifest_run["status"] == "no_progress"
    assert manifest_run["no_progress_timeout_seconds"] == 1
    isolation = manifest["failure_isolation"]
    assert isolation["failure_kind"] == "executor_no_progress"
    assert isolation["phase"] == "acceptance-1"
    assert isolation["executor_watchdog"]["executor"] == "shell"
    assert isolation["executor_watchdog"]["command_name"] == "silent acceptance"
    assert isolation["executor_watchdog"]["threshold_seconds"] == 1
    assert isolation["report_paths"]["task_report"] == result["report"]
    assert "watchdog evidence" in isolation["local_next_action"]

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    latest = payload["failure_isolation"]["latest_isolated_failures"][0]
    assert latest["failure_kind"] == "executor_no_progress"
    assert latest["executor_watchdog"]["status"] == "no_progress"
    assert payload["executor_watchdog"]["phase_no_progress_seconds"]["acceptance"] == 1


def test_runtime_dashboard_executor_no_progress_state_surfaces_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    project = tmp_path / "runtime-dashboard-no-progress-project"
    project.mkdir()
    init_project(project, "python-agent", name="runtime-dashboard-no-progress-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["acceptance"][0] = {
        "name": "silent dashboard acceptance",
        "command": "python3 -c \"import time; time.sleep(30)\"",
        "timeout_seconds": 10,
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    monkeypatch.setenv("ENGINEERING_HARNESS_EXECUTOR_NO_PROGRESS_ACCEPTANCE_SECONDS", "1")

    result = Harness(project).run_task(Harness(project).next_task())

    assert result["status"] == "failed"
    assert result["runs"][0]["status"] == "no_progress"

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    no_progress = payload["runtime_dashboard"]["executor_no_progress"]
    assert no_progress["enabled"] is True
    assert no_progress["phase_no_progress_seconds"]["acceptance"] == 1
    assert no_progress["has_unresolved_no_progress"] is True
    assert no_progress["latest_no_progress_failure"]["failure_kind"] == "executor_no_progress"
    assert no_progress["latest_no_progress_failure"]["executor_watchdog"]["status"] == "no_progress"


def test_executor_timeout_watchdog_marks_implementation_timeout(tmp_path):
    project = tmp_path / "executor-timeout-project"
    project.mkdir()
    init_project(project, "python-agent", name="executor-timeout-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["implementation"] = [
        {
            "name": "slow implementation",
            "command": "python3 -c \"import time; time.sleep(30)\"",
            "timeout_seconds": 1,
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('should not run')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task(), allow_agent=True)

    assert result["status"] == "failed"
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["phase"] == "implementation"
    assert run["status"] == "timeout"
    assert run["returncode"] is None
    watchdog = run["executor_result"]["metadata"]["watchdog"]
    assert watchdog["status"] == "timeout"
    assert watchdog["reason"] == "runtime_timeout"
    assert watchdog["timeout_seconds"] == 1
    assert watchdog["threshold_seconds"] == 1

    manifest = task_manifest(project, result)
    isolation = manifest["failure_isolation"]
    assert isolation["failure_kind"] == "executor_timeout"
    assert isolation["phase"] == "implementation"
    assert isolation["executor_watchdog"]["timeout_seconds"] == 1
    assert "timeout evidence" in isolation["local_next_action"]


def test_executor_no_progress_watchdog_marks_silent_self_iteration_planner(tmp_path):
    project = tmp_path / "executor-watchdog-planner-project"
    project.mkdir()
    init_project(project, "python-agent", name="executor-watchdog-planner-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["executor_watchdog"] = {
        "phase_no_progress_seconds": {
            "planner": 1,
        },
    }
    roadmap["self_iteration"] = {
        "enabled": True,
        "max_stages_per_iteration": 1,
        "planner": {
            "name": "silent planner",
            "command": "python3 -c \"import time; time.sleep(30)\"",
            "timeout_seconds": 10,
        },
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_self_iteration(reason="executor-watchdog-test")

    assert result["status"] == "failed"
    assert result["run"]["status"] == "no_progress"
    watchdog = result["run"]["executor_result"]["metadata"]["watchdog"]
    assert watchdog["phase"] == "self-iteration"
    assert watchdog["executor_id"] == "shell"
    assert watchdog["no_progress_timeout_seconds"] == 1
    assert result["failure_isolation"]["kind"] == "engineering-harness.planner-failure-isolation"
    assert result["failure_isolation"]["failure_kind"] == "executor_no_progress"
    assert result["failure_isolation"]["executor_watchdog"]["threshold_seconds"] == 1
    report = (project / result["report"]).read_text(encoding="utf-8")
    assert "## Failure Isolation" in report


def test_harness_runs_e2e_after_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; Path('accepted.txt').write_text('ok')\""
    task["e2e"] = [
        {
            "name": "simulate user path",
            "command": "python3 -c \"from pathlib import Path; assert Path('accepted.txt').read_text() == 'ok'; Path('e2e.txt').write_text('ok')\"",
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert (project / "e2e.txt").read_text(encoding="utf-8") == "ok"
    assert [run["phase"] for run in result["runs"]] == ["acceptance-1", "e2e"]
    assert result["task"]["e2e"][0]["name"] == "simulate user path"


def test_phase_level_state_is_durable_and_ordered_for_repairing_e2e_task(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_task_iterations"] = 2
    task["implementation"] = [
        {
            "name": "write implementation marker",
            "command": "python3 -c \"from pathlib import Path; Path('implemented.txt').write_text('ok')\"",
        }
    ]
    task["repair"] = [
        {
            "name": "repair acceptance marker",
            "command": "python3 -c \"from pathlib import Path; Path('repair.txt').write_text('fixed')\"",
        }
    ]
    task["acceptance"][0] = {
        "name": "accepts after repair",
        "command": (
            "python3 -c \"from pathlib import Path; "
            "assert Path('implemented.txt').read_text() == 'ok'; "
            "assert Path('repair.txt').read_text() == 'fixed'\""
        ),
    }
    task["e2e"] = [
        {
            "name": "simulate repaired user path",
            "command": "python3 -c \"from pathlib import Path; Path('e2e.txt').write_text('ok')\"",
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    task_state = state["tasks"]["tests"]
    history = task_state["phase_history"]

    assert [item["sequence"] for item in history] == list(range(1, len(history) + 1))
    assert [(item["phase"], item["event"]) for item in history] == [
        ("implementation", "before"),
        ("implementation", "after"),
        ("acceptance-1", "before"),
        ("acceptance-1", "after"),
        ("repair-1", "before"),
        ("repair-1", "after"),
        ("acceptance-2", "before"),
        ("acceptance-2", "after"),
        ("e2e", "before"),
        ("e2e", "after"),
        ("file-scope-guard", "before"),
        ("file-scope-guard", "after"),
        ("manifest-writing", "before"),
        ("manifest-writing", "after"),
        ("final-result", "before"),
        ("final-result", "after"),
    ]

    after_by_phase = {item["phase"]: item for item in history if item["event"] == "after"}
    assert after_by_phase["implementation"]["status"] == "passed"
    assert after_by_phase["acceptance-1"]["status"] == "failed"
    assert after_by_phase["acceptance-1"]["runs"][0]["returncode"] != 0
    assert after_by_phase["repair-1"]["status"] == "passed"
    assert after_by_phase["acceptance-2"]["status"] == "passed"
    assert after_by_phase["e2e"]["status"] == "passed"
    assert after_by_phase["file-scope-guard"]["status"] == "skipped"
    assert after_by_phase["manifest-writing"]["metadata"]["manifest_path"] == result["manifest"]
    assert after_by_phase["final-result"]["status"] == "passed"
    assert task_state["current_phase"] is None
    assert task_state["last_phase_event"]["phase"] == "final-result"
    assert task_state["phase_states"]["acceptance-1"]["status"] == "failed"


def test_interrupted_drive_resume_replay_guard_skips_passed_phases_after_stale_recovery(tmp_path, capsys):
    project = tmp_path / "interrupted-replay-project"
    project.mkdir()
    init_project(project, "python-agent", name="interrupted-replay-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    task = roadmap["milestones"][0]["tasks"][0]
    task["agent_approval_required"] = False
    task["implementation"] = [
        {
            "name": "write implementation marker",
            "command": (
                "python3 -c \"from pathlib import Path; "
                "Path('implementation.log').open('a', encoding='utf-8').write('implementation\\n')\""
            ),
        }
    ]
    task["acceptance"][0] = {
        "name": "write acceptance marker",
        "command": (
            "python3 -c \"from pathlib import Path; "
            "Path('acceptance.log').open('a', encoding='utf-8').write('acceptance\\n')\""
        ),
    }
    task["e2e"] = [
        {
            "name": "write e2e marker",
            "command": (
                "python3 -c \"from pathlib import Path; "
                "assert Path('implementation.log').read_text(encoding='utf-8').splitlines() == ['implementation']; "
                "assert Path('acceptance.log').read_text(encoding='utf-8').splitlines() == ['acceptance']; "
                "Path('e2e.log').open('a', encoding='utf-8').write('e2e\\n')\""
            ),
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    task_obj = harness.next_task()
    assert task_obj is not None
    assert harness.start_drive()["started"] is True
    state = harness.load_state()
    state.setdefault("tasks", {}).setdefault(task_obj.id, {})["attempts"] = 1
    harness.save_state(state)
    state = harness.load_state()
    interrupted_runs = []
    implementation_status, _ = harness._run_command_group(
        task_obj.implementation,
        phase="implementation",
        runs=interrupted_runs,
        dry_run=False,
        allow_live=False,
        allow_manual=False,
        allow_agent=False,
        task=task_obj,
        state=state,
        persist_state=True,
    )
    acceptance_status, _ = harness._run_command_group(
        task_obj.acceptance,
        phase="acceptance-1",
        runs=interrupted_runs,
        dry_run=False,
        allow_live=False,
        allow_manual=False,
        allow_agent=False,
        task=task_obj,
        state=state,
        persist_state=True,
    )
    assert implementation_status == "passed"
    assert acceptance_status == "passed"

    state = harness_state(project)
    previous_pid = unused_pid()
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    capsys.readouterr()
    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "completed"
    assert payload["stale_running_recovery"]["previous_pid"] == previous_pid
    result = payload["results"][0]
    assert result["status"] == "passed"
    assert [(run["phase"], run["status"]) for run in result["runs"]] == [
        ("implementation", "reused"),
        ("acceptance-1", "reused"),
        ("e2e", "passed"),
    ]
    assert (project / "implementation.log").read_text(encoding="utf-8").splitlines() == ["implementation"]
    assert (project / "acceptance.log").read_text(encoding="utf-8").splitlines() == ["acceptance"]
    assert (project / "e2e.log").read_text(encoding="utf-8").splitlines() == ["e2e"]

    state = harness_state(project)
    task_state = state["tasks"]["tests"]
    assert task_state["current_phase"] is None
    reused_events = [event for event in task_state["phase_history"] if event["event"] == "reused"]
    assert [event["phase"] for event in reused_events] == ["implementation", "acceptance-1"]
    assert all(event["metadata"]["replay_guard"]["reason"] == "passed_phase_for_current_definition" for event in reused_events)

    manifest = task_manifest(project, result)
    assert manifest["replay_guard"]["reused_phase_count"] == 2
    assert [phase["phase"] for phase in manifest["replay_guard"]["reused_phases"]] == [
        "implementation",
        "acceptance-1",
    ]
    drive_json = json.loads((project / payload["drive_report_json"]).read_text(encoding="utf-8"))
    assert drive_json["replay_guard"]["reused_phase_count"] == 2
    assert "Phase Replay Guard" in (project / payload["drive_report"]).read_text(encoding="utf-8")

    capsys.readouterr()
    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["replay_guard"]["reused_phase_count"] == 2
    assert status_payload["runtime_dashboard"]["replay_guard"]["reused_phase_count"] == 2
    assert status_payload["runtime_dashboard"]["current_phase"] is None
    assert status_payload["failure_isolation"]["unresolved_count"] == 0
    assert Harness(project).validate_roadmap()["status"] == "passed"


def test_interrupted_phase_replay_guard_reruns_when_task_command_group_changes(tmp_path, capsys):
    project = tmp_path / "interrupted-replay-command-change-project"
    project.mkdir()
    init_project(project, "python-agent", name="interrupted-replay-command-change-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["agent_approval_required"] = False
    task["implementation"] = [
        {
            "name": "write implementation marker",
            "command": (
                "python3 -c \"from pathlib import Path; "
                "Path('implementation.log').open('a', encoding='utf-8').write('old\\n')\""
            ),
        }
    ]
    task["acceptance"][0] = {
        "name": "accept changed implementation",
        "command": (
            "python3 -c \"from pathlib import Path; "
            "assert Path('implementation.log').read_text(encoding='utf-8').splitlines() == ['old', 'new']; "
            "Path('accepted.log').write_text('ok', encoding='utf-8')\""
        ),
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    task_obj = harness.next_task()
    assert task_obj is not None
    assert harness.start_drive()["started"] is True
    state = harness.load_state()
    state.setdefault("tasks", {}).setdefault(task_obj.id, {})["attempts"] = 1
    harness.save_state(state)
    state = harness.load_state()
    interrupted_runs = []
    implementation_status, _ = harness._run_command_group(
        task_obj.implementation,
        phase="implementation",
        runs=interrupted_runs,
        dry_run=False,
        allow_live=False,
        allow_manual=False,
        allow_agent=False,
        task=task_obj,
        state=state,
        persist_state=True,
    )
    assert implementation_status == "passed"

    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["implementation"][0]["command"] = (
        "python3 -c \"from pathlib import Path; "
        "Path('implementation.log').open('a', encoding='utf-8').write('new\\n')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    state = harness_state(project)
    state["drive_control"].update(
        {
            "status": "idle",
            "active": False,
            "pid": None,
            "current_task": None,
            "current_activity": "manual-interruption-clear",
        }
    )
    write_harness_state(project, state)

    capsys.readouterr()
    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    result = payload["results"][0]

    assert result["status"] == "passed"
    assert [(run["phase"], run["status"]) for run in result["runs"]] == [
        ("implementation", "passed"),
        ("acceptance-1", "passed"),
    ]
    assert (project / "implementation.log").read_text(encoding="utf-8").splitlines() == ["old", "new"]
    implementation_decision = next(
        decision
        for decision in result["replay_guard"]["decisions"]
        if decision["phase"] == "implementation"
    )
    assert implementation_decision["status"] == "not_reused"
    assert implementation_decision["reason"] == "command_group_changed"
    assert result["replay_guard"]["reused_phase_count"] == 0
    assert Harness(project).validate_roadmap()["status"] == "passed"


def test_harness_fails_task_when_e2e_fails(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = "python3 -c \"print('accepted')\""
    task["e2e"] = [
        {
            "name": "failing user path",
            "command": "python3 -c \"raise SystemExit(7)\"",
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "failed"
    assert result["runs"][-1]["phase"] == "e2e"
    assert result["runs"][-1]["returncode"] == 7
    assert "Required e2e command failed" in result["message"]


def test_browser_user_experience_e2e_static_smoke_captures_dom_evidence_and_status(tmp_path, capsys):
    project = tmp_path / "browser-ux-project"
    project.mkdir()
    init_project(project, "node-frontend", name="browser-ux-project")
    (project / "docs").mkdir()
    (project / "tests/e2e").mkdir(parents=True)
    (project / "index.html").write_text(
        """
        <!doctype html>
        <html>
          <head><title>Feedback</title></head>
          <body>
            <main>
              <h1>Feedback workspace</h1>
              <form id="feedback-form">
                <label>Email <input name="email" type="email" required></label>
                <label>Message <textarea name="message" required></textarea></label>
                <button type="submit">Send feedback</button>
              </form>
            </main>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    (project / "docs/frontend-experience.md").write_text(
        "dashboard operator feedback-workflow acceptance evidence\n",
        encoding="utf-8",
    )
    (project / "tests/e2e/feedback-workflow.journey.json").write_text(
        json.dumps(
            {
                "journey_id": "feedback-workflow",
                "persona": "operator",
                "routes": [
                    {
                        "path": "/",
                        "expect_text": ["Feedback workspace"],
                        "expect_roles": ["main", "form", "button", "textbox"],
                        "expect_forms": [
                            {
                                "selector": "#feedback-form",
                                "fields": ["email", "message"],
                                "submit_text": "Send feedback",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["experience"] = {
        "kind": "dashboard",
        "personas": ["operator"],
        "primary_surfaces": ["feedback workspace"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "feedback-workflow",
                "persona": "operator",
                "goal": "Submit local feedback and see the form state.",
            }
        ],
    }
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["**"]
    task["acceptance"][0] = {"name": "acceptance ok", "command": "python3 -c \"print('accepted')\""}
    task["e2e"] = [
        {
            "name": "feedback-workflow browser user-experience gate passes",
            "command": browser_user_experience_command("feedback-workflow"),
            "user_experience_gate": {
                "kind": "engineering-harness.browser-user-experience",
                "journey": {"id": "feedback-workflow", "persona": "operator"},
                "route_form_role_declarations": ["tests/e2e/feedback-workflow.journey.json"],
                "evidence_paths": {
                    "dom": "artifacts/browser-e2e/feedback-workflow/dom-evidence.json",
                    "dom_snapshot": "artifacts/browser-e2e/feedback-workflow/dom-snapshot.txt",
                    "screenshot": "artifacts/browser-e2e/feedback-workflow/screenshot.png",
                },
            },
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())
    dom_evidence = project / "artifacts/browser-e2e/feedback-workflow/dom-evidence.json"
    status = Harness(project).status_summary()
    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)

    assert result["status"] == "passed"
    assert result["runs"][-1]["phase"] == "e2e"
    assert result["runs"][-1]["user_experience_gate"]["kind"] == "engineering-harness.browser-user-experience"
    evidence = json.loads(dom_evidence.read_text(encoding="utf-8"))
    assert evidence["status"] == "passed"
    assert evidence["runner"] == "static-html-smoke"
    assert evidence["routes"][0]["forms"][0]["id"] == "feedback-form"
    browser_ux = status["browser_user_experience"]
    assert browser_ux["status"] == "passed"
    assert browser_ux["configured_gate_count"] == 1
    assert browser_ux["journeys"][0]["declaration_summary"]["form_count"] == 1
    assert browser_ux["journeys"][0]["evidence_paths"]["dom"]["exists"] is True
    assert status_payload["runtime_dashboard"]["browser_user_experience"]["status"] == "passed"


def test_browser_user_experience_e2e_failure_is_reported_as_user_experience_gate(tmp_path):
    project = tmp_path / "browser-ux-failure-project"
    project.mkdir()
    init_project(project, "node-frontend", name="browser-ux-failure-project")
    (project / "tests/e2e").mkdir(parents=True)
    (project / "index.html").write_text(
        "<main><h1>Feedback workspace</h1><form id='feedback-form'><input name='email'></form></main>",
        encoding="utf-8",
    )
    (project / "tests/e2e/feedback-workflow.journey.json").write_text(
        json.dumps(
            {
                "journey_id": "feedback-workflow",
                "persona": "operator",
                "routes": [
                    {
                        "path": "/",
                        "expect_text": ["Feedback workspace"],
                        "expect_roles": ["main", "button"],
                        "expect_forms": [
                            {
                                "selector": "#feedback-form",
                                "fields": ["email", "message"],
                                "submit_text": "Send feedback",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["experience"] = {
        "kind": "dashboard",
        "personas": ["operator"],
        "primary_surfaces": ["feedback workspace"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "feedback-workflow",
                "persona": "operator",
                "goal": "Submit local feedback and see the form state.",
            }
        ],
    }
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0] = {"name": "acceptance ok", "command": "python3 -c \"print('accepted')\""}
    task["e2e"] = [
        {
            "name": "feedback-workflow browser user-experience gate passes",
            "command": browser_user_experience_command("feedback-workflow"),
            "user_experience_gate": {
                "kind": "engineering-harness.browser-user-experience",
                "journey": {"id": "feedback-workflow", "persona": "operator"},
                "route_form_role_declarations": ["tests/e2e/feedback-workflow.journey.json"],
                "evidence_paths": {
                    "dom": "artifacts/browser-e2e/feedback-workflow/dom-evidence.json",
                    "dom_snapshot": "artifacts/browser-e2e/feedback-workflow/dom-snapshot.txt",
                },
            },
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())
    manifest = task_manifest(project, result)
    status = Harness(project).status_summary()

    assert result["status"] == "failed"
    assert "Required user-experience gate failed" in result["message"]
    assert "Required e2e command failed" not in result["message"]
    assert manifest["failure_isolation"]["failure_kind"] == "user_experience_gate_failure"
    assert manifest["failure_isolation"]["phase"] == "e2e"
    assert "browser user-experience gate evidence" in manifest["failure_isolation"]["local_next_action"]
    assert status["browser_user_experience"]["status"] == "failed"
    assert status["browser_user_experience"]["latest_failures"][0]["journey_id"] == "feedback-workflow"


def test_file_scope_guard_allows_in_scope_changes(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "write scoped file",
            "command": "python3 -c \"from pathlib import Path; Path('src').mkdir(exist_ok=True); Path('src/ok.txt').write_text('ok')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; assert Path('src/ok.txt').read_text() == 'ok'\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    assert result["safety"]["file_scope_guard"]["status"] == "passed"


def test_file_scope_guard_blocks_out_of_scope_changes(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "write unscoped file",
            "command": "python3 -c \"from pathlib import Path; Path('outside.txt').write_text('outside')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('acceptance ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "failed"
    assert result["safety"]["file_scope_guard"]["violations"] == ["outside.txt"]
    assert "outside file_scope" in result["message"]
    manifest = task_manifest(project, result)
    file_scope = policy_decision(manifest, "file_scope_guard", outcome="denied")
    assert file_scope["effect"] == "deny"
    assert file_scope["status"] == "failed"
    assert file_scope["input"]["file_scope"]["patterns"] == ["src/**"]
    assert file_scope["metadata"]["violations"] == ["outside.txt"]
    assert manifest["policy_decision_summary"]["blocking"][0]["kind"] == "file_scope_guard"
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    index = Harness(project).manifest_index()
    assert index["policy_decision_summary"]["blocking"][0]["kind"] == "file_scope_guard"


def test_file_scope_guard_blocks_changed_preexisting_dirty_out_of_scope_file(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    (project / "outside.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)
    (project / "outside.txt").write_text("dirty before", encoding="utf-8")

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "modify preexisting dirty file",
            "command": "python3 -c \"from pathlib import Path; Path('outside.txt').write_text('dirty after')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('acceptance ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "failed"
    assert result["safety"]["file_scope_guard"]["changed_preexisting_dirty_paths"] == ["outside.txt"]
    assert result["safety"]["file_scope_guard"]["violations"] == ["outside.txt"]


def test_git_checkpoint_refuses_dirty_worktree_that_predates_task(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task_payload = roadmap["milestones"][0]["tasks"][0]
    task_payload["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; Path('done.txt').write_text('done')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "configure task"], cwd=project, check=True, capture_output=True, text=True)
    (project / "preexisting.txt").write_text("dirty before task", encoding="utf-8")

    harness = Harness(project)
    task = harness.next_task()
    result = harness.run_task(task)
    checkpoint = harness.git_checkpoint(task)

    assert result["status"] == "passed"
    assert result["safety"]["git_preflight"]["dirty_before_paths"] == ["preexisting.txt"]
    assert checkpoint["status"] == "skipped"
    assert "dirty worktree existed before the task" in checkpoint["message"]


def test_checkpoint_readiness_clean_worktree(tmp_path):
    project = tmp_path / "checkpoint-clean-project"
    project.mkdir()
    init_project(project, "python-agent", name="checkpoint-clean-project")
    init_git_repo(project)

    readiness = Harness(project).status_summary()["checkpoint_readiness"]

    assert readiness["is_repository"] is True
    assert readiness["ready"] is True
    assert readiness["blocking"] is False
    assert readiness["reason"] == "clean"
    assert readiness["dirty_paths"] == []
    assert readiness["blocking_paths"] == []
    assert readiness["safe_to_checkpoint_paths"] == []


def test_status_json_exposes_checkpoint_readiness(tmp_path, capsys):
    project = tmp_path / "checkpoint-status-project"
    project.mkdir()
    init_project(project, "python-agent", name="checkpoint-status-project")
    init_git_repo(project)

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    readiness = payload["checkpoint_readiness"]
    assert readiness["kind"] == "engineering-harness.checkpoint-readiness"
    assert readiness["ready"] is True
    assert readiness["reason"] == "clean"
    assert payload["runtime_dashboard"]["checkpoint_readiness"] == readiness


def test_checkpoint_readiness_roadmap_only_materialization_dirtiness(tmp_path):
    project = tmp_path / "checkpoint-roadmap-project"
    project.mkdir()
    init_project(project, "python-agent", name="checkpoint-roadmap-project")
    init_git_repo(project)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["checkpoint_readiness_note"] = "roadmap materialization"
    roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")

    readiness = Harness(project).status_summary()["checkpoint_readiness"]

    assert readiness["ready"] is True
    assert readiness["blocking"] is False
    assert readiness["reason"] == "harness_materialization_dirty"
    assert readiness["dirty_paths"] == [".engineering/roadmap.yaml"]
    assert readiness["safe_to_checkpoint_paths"] == [".engineering/roadmap.yaml"]
    assert readiness["blocking_paths"] == []
    assert readiness["classifications"]["harness_materialization"] == [".engineering/roadmap.yaml"]


def test_checkpoint_readiness_blocks_user_dirty_worktree(tmp_path):
    project = tmp_path / "checkpoint-mixed-project"
    project.mkdir()
    init_project(project, "python-agent", name="checkpoint-mixed-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["acceptance"][0]["command"] = "python3 -c \"print('checkpoint readiness')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "user-modified.txt").write_text("base", encoding="utf-8")
    (project / "user-staged.txt").write_text("base", encoding="utf-8")
    (project / "user-deleted.txt").write_text("base", encoding="utf-8")
    init_git_repo(project)

    roadmap["checkpoint_readiness_note"] = "safe roadmap materialization"
    roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
    (project / "src").mkdir()
    (project / "src/in-scope.txt").write_text("task dirty", encoding="utf-8")
    (project / "user-modified.txt").write_text("modified", encoding="utf-8")
    (project / "user-staged.txt").write_text("staged", encoding="utf-8")
    subprocess.run(["git", "add", "user-staged.txt"], cwd=project, check=True)
    (project / "user-deleted.txt").unlink()
    (project / "user-untracked.txt").write_text("untracked", encoding="utf-8")

    readiness = Harness(project).status_summary()["checkpoint_readiness"]

    assert readiness["ready"] is False
    assert readiness["blocking"] is True
    assert readiness["reason"] == "mixed_unrelated_user_dirty"
    assert set(readiness["dirty_paths"]) == {
        ".engineering/roadmap.yaml",
        "src/in-scope.txt",
        "user-deleted.txt",
        "user-modified.txt",
        "user-staged.txt",
        "user-untracked.txt",
    }
    assert readiness["safe_to_checkpoint_paths"] == [".engineering/roadmap.yaml", "src/in-scope.txt"]
    assert readiness["blocking_paths"] == [
        "user-deleted.txt",
        "user-modified.txt",
        "user-staged.txt",
        "user-untracked.txt",
    ]
    states = {item["path"]: set(item["states"]) for item in readiness["dirty_path_states"]}
    assert "modified" in states["user-modified.txt"]
    assert "staged" in states["user-staged.txt"]
    assert "deleted" in states["user-deleted.txt"]
    assert "untracked" in states["user-untracked.txt"]
    assert "will not commit or clean" in readiness["recommended_action"]


def test_merge_plan_reports_branch_conflicts_order_and_acceptance(tmp_path, capsys):
    project = tmp_path / "merge-plan-branches"
    project.mkdir()
    init_project(project, "python-agent", name="merge-plan-branches")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "task-a",
            "title": "Task A",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [{"name": "task a acceptance", "command": "python3 -c \"print('task-a ok')\""}],
        },
        {
            "id": "task-b",
            "title": "Task B",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [{"name": "task b acceptance", "command": "python3 -c \"print('task-b ok')\""}],
        },
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "shared.txt").write_text("base\n", encoding="utf-8")
    init_git_repo(project)
    base = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(["git", "checkout", "-b", "task-a"], cwd=project, check=True, capture_output=True, text=True)
    (project / "shared.txt").write_text("task a\n", encoding="utf-8")
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "task a"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", base], cwd=project, check=True, capture_output=True, text=True)

    subprocess.run(["git", "checkout", "-b", "task-b"], cwd=project, check=True, capture_output=True, text=True)
    (project / "shared.txt").write_text("task b\n", encoding="utf-8")
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "task b"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", base], cwd=project, check=True, capture_output=True, text=True)

    assert cli_main(
        [
            "merge-plan",
            "--project-root",
            str(project),
            "--base",
            base,
            "--branch",
            "task-a",
            "--branch",
            "task-b",
            "--write",
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "engineering-harness.merge-planner"
    assert payload["status"] == "needs_attention"
    assert payload["safety"]["destructive_git_actions_performed"] is False
    assert payload["changed_files"] == ["a.txt", "b.txt", "shared.txt"]
    assert payload["likely_conflicts"]["status"] == "conflicts_likely"
    assert any(
        conflict["kind"] == "candidate_overlap" and conflict["paths"] == ["shared.txt"]
        for conflict in payload["likely_conflicts"]["items"]
    )
    assert [item["candidate_id"] for item in payload["recommended_merge_order"]] == [
        "branch:task-a",
        "branch:task-b",
    ]
    commands = [command["command"] for command in payload["post_merge_acceptance"]["commands"]]
    assert "python3 -c \"print('task-a ok')\"" in commands
    assert "python3 -c \"print('task-b ok')\"" in commands

    report_text = (project / payload["merge_plan_report"]).read_text(encoding="utf-8")
    assert "merge planner" in report_text
    assert "worktree" in report_text
    assert "Recommended Merge Order" in report_text
    assert "Likely Conflicts" in report_text
    assert "Post-Merge Acceptance" in report_text
    sidecar = json.loads((project / payload["merge_plan_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["likely_conflicts"]["count"] == payload["likely_conflicts"]["count"]


def test_merge_plan_reports_dirty_worktree_paths_and_manual_acceptance(tmp_path, capsys):
    project = tmp_path / "merge-plan-worktree"
    project.mkdir()
    init_project(project, "python-agent", name="merge-plan-worktree")
    (project / "tracked.txt").write_text("base\n", encoding="utf-8")
    init_git_repo(project)
    subprocess.run(["git", "branch", "task-dirty"], cwd=project, check=True, capture_output=True, text=True)
    worktree = tmp_path / "task-dirty-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "task-dirty"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    assert cli_main(
        [
            "merge-plan",
            "--project-root",
            str(project),
            "--worktree",
            str(worktree),
            "--post-merge-acceptance",
            "python3 -m pytest -q",
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs_attention"
    assert payload["dirty_paths"] == ["tracked.txt", "untracked.txt"]
    candidate = payload["candidates"][0]
    assert candidate["source"] == "worktree"
    assert candidate["branch"] == "task-dirty"
    assert candidate["dirty"] is True
    assert candidate["dirty_paths"] == ["tracked.txt", "untracked.txt"]
    assert payload["recommended_merge_order"][0]["candidate_id"].startswith("worktree:")
    assert payload["post_merge_acceptance"]["commands"] == [
        {
            "source": "manual",
            "task_id": None,
            "phase": "post-merge acceptance",
            "name": "manual post-merge acceptance",
            "command": "python3 -m pytest -q",
        }
    ]


def test_parallel_drive_plans_safe_dispatch_waves_and_dependencies(tmp_path):
    project = tmp_path / "parallel-plan-project"
    project.mkdir()
    init_project(project, "python-agent", name="parallel-plan-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "task-a",
            "title": "Task A",
            "status": "pending",
            "file_scope": ["src/shared.txt"],
            "acceptance": [{"name": "a", "command": "python3 -c \"print('a')\""}],
        },
        {
            "id": "task-b",
            "title": "Task B",
            "status": "pending",
            "file_scope": ["src/shared.txt"],
            "acceptance": [{"name": "b", "command": "python3 -c \"print('b')\""}],
        },
        {
            "id": "task-c",
            "title": "Task C",
            "status": "pending",
            "depends_on": ["task-a"],
            "file_scope": ["src/c.txt"],
            "acceptance": [{"name": "c", "command": "python3 -c \"print('c')\""}],
        },
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    plan = plan_parallel_drive(project, max_workers=2, max_tasks=3)

    assert plan["status"] == "planned"
    assert plan["selected_count"] == 2
    assert [task["task_id"] for task in plan["selected_tasks"]] == ["task-a", "task-b"]
    assert plan["dispatch_waves"][0]["tasks"] == ["task-a"]
    assert plan["dispatch_waves"][1]["tasks"] == ["task-b"]
    skipped = {task["task_id"]: task for task in plan["skipped_tasks"]}
    assert skipped["task-c"]["skip_reasons"][0]["code"] == "dependency_not_satisfied"


def test_parallel_drive_runs_workers_merges_and_cleans_worktrees(tmp_path, capsys):
    project = tmp_path / "parallel-drive-project"
    project.mkdir()
    init_project(project, "python-agent", name="parallel-drive-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "task-a",
            "title": "Task A",
            "status": "pending",
            "file_scope": ["src/a.txt"],
            "acceptance": [
                {
                    "name": "write a",
                    "command": "python3 -c \"from pathlib import Path; Path('src').mkdir(exist_ok=True); Path('src/a.txt').write_text('a')\"",
                }
            ],
        },
        {
            "id": "task-b",
            "title": "Task B",
            "status": "pending",
            "file_scope": ["src/b.txt"],
            "acceptance": [
                {
                    "name": "write b",
                    "command": "python3 -c \"from pathlib import Path; Path('src').mkdir(exist_ok=True); Path('src/b.txt').write_text('b')\"",
                }
            ],
        },
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code, payload = run_parallel_drive_json(capsys, project, "--max-workers", "2", "--max-tasks", "2")

    assert exit_code == 0
    assert payload["kind"] == "engineering-harness.parallel-drive-run-manifest"
    assert payload["status"] == "completed"
    assert payload["summary"]["merged_count"] == 2
    assert (project / "src/a.txt").read_text(encoding="utf-8") == "a"
    assert (project / "src/b.txt").read_text(encoding="utf-8") == "b"
    for worker in payload["workers"]:
        assert worker["status"] == "merged"
        assert worker["merge"]["status"] == "merged"
        assert worker["cleanup"]["worktree_exists"] is False
        assert worker["cleanup"]["branch_exists"] is False
        assert not Path(worker["worktree_path"]).exists()
        assert (project / worker["manifest"]).exists()
        manifest = json.loads((project / worker["manifest"]).read_text(encoding="utf-8"))
        assert manifest["parallel_drive"]["worker_status"] == "merged"
        assert manifest["parallel_drive"]["merge"]["status"] == "merged"
    state = harness_state(project)
    assert state["tasks"]["task-a"]["status"] == "passed"
    assert state["tasks"]["task-b"]["status"] == "passed"
    assert load_parallel_drive_state(project)["status"] == "completed"
    status_payload = Harness(project).status_summary()
    assert status_payload["runtime_dashboard"]["parallel_drive"]["status"] == "completed"


def test_parallel_drive_preserves_failed_worker_branch_and_worktree(tmp_path, capsys):
    project = tmp_path / "parallel-drive-failure-project"
    project.mkdir()
    init_project(project, "python-agent", name="parallel-drive-failure-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "task-fails",
            "title": "Task Fails",
            "status": "pending",
            "file_scope": ["src/fail.txt"],
            "acceptance": [{"name": "fail", "command": "python3 -c \"raise SystemExit(7)\""}],
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code, payload = run_parallel_drive_json(capsys, project, "--max-workers", "1", "--max-tasks", "1")

    assert exit_code == 1
    assert payload["status"] == "failed"
    worker = payload["workers"][0]
    assert worker["status"] == "failed"
    assert worker["preservation"]["preserved"] is True
    assert Path(worker["worktree_path"]).exists()
    assert (project / worker["manifest"]).exists()
    branch = subprocess.run(
        ["git", "rev-parse", "--verify", worker["branch"]],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert branch.returncode == 0


def test_policy_decision_schema_records_dirty_worktree_warning(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)
    (project / "preexisting.txt").write_text("dirty before task", encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    manifest = task_manifest(project, result)
    warning = policy_decision(manifest, "git_preflight", outcome="warning")
    assert warning["effect"] == "warn"
    assert warning["severity"] == "warning"
    assert warning["status"] == "dirty"
    assert warning["input"]["worktree"]["dirty_before_paths"] == ["preexisting.txt"]
    assert warning["metadata"]["dirty_before_paths"] == ["preexisting.txt"]


def test_validate_roadmap_catches_missing_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"] = []
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.validate_roadmap()

    assert result["status"] == "failed"
    assert any("acceptance" in error for error in result["errors"])


def test_validate_roadmap_allows_missing_experience_for_backward_compatibility(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")

    harness = Harness(project)
    result = harness.validate_roadmap()

    assert result["status"] == "passed"
    assert result["errors"] == []
    assert harness.status_summary()["experience"]["source"] == "derived"


def test_domain_frontend_generator_dashboard_only_for_autonomous_theorem_prover():
    plan = build_domain_frontend_plan(
        project_name="Lean Proof Runner",
        profile="lean-formalization",
        goal_text="Build an autonomous theorem prover that searches proofs and records proof artifacts.",
    )

    assert plan["kind"] == "dashboard"
    assert plan["domain"] == "autonomous-theorem-prover"
    assert plan["surface_policy"] == "dashboard-only"
    assert plan["frontend_required"] is True
    assert "proof attempt queue" in plan["primary_surfaces"]
    assert plan["auth"]["required"] is False
    assert plan["decision_contract"]["kind"] == DOMAIN_FRONTEND_DECISION_KIND
    assert plan["decision_contract"]["status"] == "required"
    assert plan["decision_contract"]["generated_by"] == DOMAIN_FRONTEND_GENERATOR_ID


def test_domain_frontend_generator_submission_review_return_workflow():
    plan = build_domain_frontend_plan(
        project_name="Student Paper Review",
        profile="python-agent",
        goal_text="Build a student paper submission, reviewer comments, returned decision, and revision workflow.",
    )

    assert plan["kind"] == "submission-review"
    assert plan["domain"] == "student-paper-review"
    assert plan["surface_policy"] == "submission-review-return"
    assert "returned work view" in plan["primary_surfaces"]
    assert plan["auth"]["roles"] == ["student", "reviewer"]
    assert any("return" in item["id"] for item in plan["e2e_journeys"])


def test_domain_frontend_generator_account_role_flows_for_multi_role_system():
    plan = build_domain_frontend_plan(
        project_name="Operations Console",
        profile="node-frontend",
        goal_text="Build account login, role assignment, admin operator approval, permission denial, and audit flows.",
    )

    assert plan["kind"] == "multi-role-app"
    assert plan["domain"] == "multi-role-system"
    assert plan["surface_policy"] == "account-role-flows"
    assert plan["auth"]["required"] is True
    assert "account setup" in plan["primary_surfaces"]
    assert "role assignment" in plan["primary_surfaces"]
    assert "access denied state" in plan["primary_surfaces"]


def test_domain_frontend_generator_app_specific_views_for_ordinary_software():
    plan = build_domain_frontend_plan(
        project_name="Recipe Tracker",
        profile="node-frontend",
        goal_text="Build a recipe tracker app with collection, editor, shopping list, empty, and error views.",
    )

    assert plan["kind"] == "app-specific"
    assert plan["domain"] == "ordinary-software"
    assert plan["surface_policy"] == "app-specific-views"
    assert plan["auth"]["required"] is False
    assert "primary app workspace" in plan["primary_surfaces"]
    assert plan["e2e_journeys"][0]["id"] == "user-completes-primary-workflow"


def test_domain_frontend_generator_e2e_exposes_roadmap_frontend_tasks_and_status(tmp_path, capsys):
    project = tmp_path / "proof-dashboard"
    project.mkdir()
    proposal = plan_goal_roadmap(
        project_root=project,
        project_name="Proof Dashboard",
        profile="lean-formalization",
        goal_text="Build an autonomous theorem prover dashboard for proof attempts and local artifacts.",
        stage_count=1,
    )
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text(json.dumps(proposal["roadmap"]), encoding="utf-8")

    harness = Harness(project)
    frontend = harness.frontend_task_plan()
    status = harness.status_summary()
    cli_exit = cli_main(["status", "--project-root", str(project), "--json"])
    status_payload = json.loads(capsys.readouterr().out)

    assert proposal["roadmap"]["experience"]["decision_contract"]["domain"] == "autonomous-theorem-prover"
    assert proposal["roadmap"]["planning"]["domain_frontend"]["surface_policy"] == "dashboard-only"
    assert frontend["status"] == "proposed"
    assert frontend["domain_frontend"]["domain"] == "autonomous-theorem-prover"
    assert frontend["milestone"]["domain_frontend"]["status"] == "required"
    assert status["domain_frontend"]["experience_kind"] == "dashboard"
    assert status["runtime_dashboard"]["domain_frontend"]["status"] == "required"
    assert status["runtime_dashboard"]["frontend_experience"]["domain"] == "autonomous-theorem-prover"
    assert cli_exit == 0
    assert status_payload["runtime_dashboard"]["domain_frontend"]["domain"] == "autonomous-theorem-prover"


@pytest.mark.parametrize(
    ("project_name", "roadmap_updates", "task_title", "expected_kind", "expected_persona", "auth_required"),
    [
        (
            "autonomous-worker",
            {},
            "Run the autonomous research worker and inspect latest artifacts.",
            "dashboard",
            "operator",
            False,
        ),
        (
            "student-review",
            {},
            "Build the student submission review workflow with reviewer comments and revision decisions.",
            "submission-review",
            "student",
            True,
        ),
        (
            "role-operations",
            {},
            "Define a multi-role admin operator approver flow with login, permissions, and audit log.",
            "multi-role-app",
            "admin",
            True,
        ),
        (
            "recipe-tracker",
            {},
            "Build a recipe tracker app with editor, collection, detail, empty, and error views.",
            "app-specific",
            "user",
            False,
        ),
        (
            "api-service",
            {"project_kind": "api"},
            "Validate REST API OpenAPI endpoints with a documented client example.",
            "api-only",
            "api client",
            False,
        ),
        (
            "cli-tool",
            {"project_kind": "cli"},
            "Validate the CLI command-line journey and documented command output.",
            "cli-only",
            "developer",
            False,
        ),
    ],
)
def test_default_frontend_experience_planner_derives_common_cases(
    tmp_path,
    project_name,
    roadmap_updates,
    task_title,
    expected_kind,
    expected_persona,
    auth_required,
):
    roadmap = roadmap_without_experience(project_name, task_title=task_title)
    roadmap.update(roadmap_updates)

    summary = status_summary_for_roadmap(tmp_path, project_name, roadmap)
    experience = summary["experience"]

    assert experience["source"] == "derived"
    assert experience["derived"] is True
    assert experience["kind"] == expected_kind
    assert experience["recommendation"] == expected_kind
    assert expected_persona in experience["personas"]
    assert experience["auth"]["required"] is auth_required
    assert experience["primary_surfaces"]
    assert experience["e2e_journeys"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_kind", "expected_scope", "expected_term"),
    [
        ("valid/dashboard.json", "dashboard", "frontend/**", "artifact"),
        ("valid/submission-review.json", "submission-review", "frontend/**", "revision"),
        ("valid/multi-role-app.json", "multi-role-app", "frontend/**", "audit"),
        ("valid/api-only.json", "api-only", "openapi/**", "openapi"),
        ("valid/cli-only.json", "cli-only", "cli/**", "documented commands"),
    ],
)
def test_frontend_task_generator_proposes_kind_specific_tasks(
    tmp_path,
    fixture_name,
    expected_kind,
    expected_scope,
    expected_term,
):
    project = tmp_path / expected_kind
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text((ROADMAP_FIXTURES / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")

    result = Harness(project).frontend_task_plan()

    assert result["status"] == "proposed"
    assert result["materialized"] is False
    assert result["experience"]["kind"] == expected_kind
    assert result["milestone"]["id"] == "frontend-visualization"
    assert result["milestone"]["generated_by"] == "engineering-harness-frontend-task-generator"
    assert len(result["tasks"]) == 1 + len(result["experience"]["e2e_journeys"])

    contract_task = result["tasks"][0]
    assert contract_task["frontend"]["task_kind"] == "experience-contract"
    assert contract_task["file_scope"] == ["docs/**", "tests/**", "templates/**"]
    assert contract_task["acceptance"][0]["command"].startswith("python3 ")
    assert contract_task["e2e"]

    journey_task = next(task for task in result["tasks"] if task["frontend"]["task_kind"] == "journey-check")
    assert expected_scope in journey_task["file_scope"]
    assert journey_task["acceptance"][0]["command"].startswith("python3 ")
    assert journey_task["e2e"][0]["command"].startswith("python3 ")
    assert expected_term in json.dumps(journey_task).lower()
    assert "use existing project conventions" in journey_task["frontend"]["stack_policy"]
    if expected_kind in {"app-specific", "dashboard", "multi-role-app", "submission-review"}:
        gate = journey_task["e2e"][0]["user_experience_gate"]
        assert gate["kind"] == "engineering-harness.browser-user-experience"
        assert gate["route_form_role_declarations"]
        assert gate["evidence_paths"]["dom"].startswith("artifacts/browser-e2e/")
        assert "playwright_template" in gate["commands"]
        assert journey_task["frontend"]["browser_user_experience_gate"]["runner"]["fallback"]["kind"] == "static-html-smoke"
    else:
        assert "user_experience_gate" not in journey_task["e2e"][0]


def test_frontend_task_generator_materializes_derived_plan_and_validates(tmp_path):
    project = tmp_path / "api-service"
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap = roadmap_without_experience(
        "api-service",
        task_title="Validate REST API OpenAPI endpoints with a documented client example.",
    )
    roadmap["project_kind"] = "api"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    proposal = Harness(project).frontend_task_plan()
    assert proposal["status"] == "proposed"
    assert proposal["experience"]["source"] == "derived"
    assert "frontend-visualization" not in roadmap_path.read_text(encoding="utf-8")

    result = Harness(project).materialize_frontend_tasks(reason="test")

    assert result["status"] == "materialized"
    assert result["materialized"] is True
    assert result["tasks_added"] == 2
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    generated = updated["milestones"][-1]
    assert generated["id"] == "frontend-visualization"
    assert generated["experience_kind"] == "api-only"
    assert generated["experience_source"] == "derived"
    assert generated["tasks"][1]["file_scope"] == [
        "src/**",
        "api/**",
        "openapi/**",
        "docs/**",
        "examples/**",
        "tests/**",
        "templates/**",
        "package.json",
        "pyproject.toml",
    ]
    assert generated["tasks"][1]["frontend"]["candidate_check_paths"]
    assert Harness(project).validate_roadmap()["status"] == "passed"

    log_path = project / ".engineering/state/decision-log.jsonl"
    assert "frontend_task_generation" in log_path.read_text(encoding="utf-8")
    assert Harness(project).materialize_frontend_tasks()["status"] == "skipped"


def test_frontend_tasks_cli_proposes_by_default_and_materializes_on_flag(tmp_path):
    project = tmp_path / "cli-tool"
    project.mkdir()
    init_project(project, "python-agent", name="cli-tool")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["title"] = "Validate CLI command output and generated reports."
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    before = roadmap_path.read_text(encoding="utf-8")

    propose_exit = cli_main(["frontend-tasks", "--project-root", str(project), "--json"])
    after_proposal = roadmap_path.read_text(encoding="utf-8")
    materialize_exit = cli_main(["frontend-tasks", "--project-root", str(project), "--materialize", "--json"])

    assert propose_exit == 0
    assert after_proposal == before
    assert materialize_exit == 0
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    generated = updated["milestones"][-1]
    assert generated["id"] == "frontend-visualization"
    assert generated["experience_kind"] == "cli-only"
    assert any("cli/**" in task["file_scope"] for task in generated["tasks"])


@pytest.mark.parametrize(
    "fixture_name",
    [
        "valid/api-only.json",
        "valid/dashboard.json",
        "valid/submission-review.json",
        "valid/multi-role-app.json",
        "valid/cli-only.json",
    ],
)
def test_valid_roadmap_fixtures_pass_validation(tmp_path, fixture_name):
    result = validate_roadmap_fixture(tmp_path, fixture_name)

    assert result["status"] == "passed"
    assert result["errors"] == []


@pytest.mark.parametrize(
    ("experience", "expected_error"),
    [
        ("dashboard", "top-level `experience` must be a mapping"),
        (
            {
                "kind": "desktop-app",
                "personas": ["operator"],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.kind `desktop-app` is not supported",
        ),
        (
            {
                "kind": "dashboard",
                "personas": [],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.personas must include at least one item",
        ),
        (
            {
                "kind": "dashboard",
                "personas": ["operator"],
                "primary_surfaces": [""],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.primary_surfaces[0] must be a non-empty string",
        ),
        (
            {
                "kind": "multi-role-app",
                "personas": ["operator"],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": True, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.auth.roles must include at least one role when auth.required is true",
        ),
        (
            {
                "kind": "dashboard",
                "personas": ["operator"],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [],
            },
            "experience.e2e_journeys must define at least one journey",
        ),
        (
            {
                "kind": "submission-review",
                "personas": ["student"],
                "primary_surfaces": ["submission portal"],
                "auth": {"required": True, "roles": ["student"]},
                "e2e_journeys": [
                    {"id": "reviewer-checks-work", "persona": "reviewer", "goal": "Review submitted work."}
                ],
            },
            "experience.e2e_journeys[0].persona `reviewer` must match one of experience.personas",
        ),
    ],
)
def test_invalid_experience_shapes_fail_validation(tmp_path, experience, expected_error):
    roadmap = roadmap_fixture_payload("valid/dashboard.json")
    roadmap["experience"] = experience

    result = validate_roadmap_payload(tmp_path, roadmap)

    assert result["status"] == "failed"
    assert result["error_count"] >= 1
    assert any(expected_error in error for error in result["errors"]), result["errors"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_error"),
    [
        ("invalid/missing-acceptance.json", "must define at least one acceptance command"),
        ("invalid/duplicate-task-id.json", "duplicate task id: duplicate-fixture-task"),
        ("invalid/unknown-executor.json", "has unknown executor `spaceship`"),
        ("invalid/continuation-stage-without-tasks.json", "must define at least one task"),
    ],
)
def test_invalid_roadmap_fixtures_fail_validation(tmp_path, fixture_name, expected_error):
    result = validate_roadmap_fixture(tmp_path, fixture_name)

    assert result["status"] == "failed"
    assert result["error_count"] >= 1
    assert any(expected_error in error for error in result["errors"]), result["errors"]


def test_harness_blocks_codex_executor_without_agent_approval(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0] = {"name": "agent work", "executor": "codex", "prompt": "Do not change files."}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert "allow-agent" in result["message"]
    assert result["runs"][0]["executor"] == "codex"
    assert result["runs"][0]["executor_metadata"]["id"] == "codex"
    assert result["runs"][0]["executor_metadata"]["kind"] == "agent"
    assert result["runs"][0]["executor_metadata"]["requires_agent_approval"] is True
    assert result["runs"][0]["executor_result"]["status"] == "blocked"

    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["runs"][0]["executor_metadata"] == result["runs"][0]["executor_metadata"]
    assert manifest["runs"][0]["executor_result"]["status"] == "blocked"
    assert policy_decision(manifest, "executor_policy", outcome="allowed")["executor"] == "codex"
    executor_approval = policy_decision(manifest, "executor_approval", outcome="requires_approval")
    assert executor_approval["effect"] == "requires_approval"
    assert executor_approval["approval_flag"] == "--allow-agent"
    assert executor_approval["requires_approval"] is True


def test_discover_projects_finds_configured_and_candidate_projects(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    init_project(configured, "python-agent", name="configured")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "package.json").write_text('{"name": "candidate"}', encoding="utf-8")

    projects = discover_projects(tmp_path)
    by_root = {project.root: project for project in projects}

    assert by_root[configured.resolve()].configured is True
    assert by_root[candidate.resolve()].configured is False
    assert by_root[candidate.resolve()].profile == "node-frontend"


def test_drive_runs_until_roadmap_is_empty(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["drive", "--project-root", str(project)])

    assert exit_code == 0
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["status"] == "passed"
    assert list((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))


def test_workspace_dispatch_runs_one_project_in_deterministic_order(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    zeta = init_workspace_project(workspace, "zeta-project", marker="zeta-marker.txt")
    alpha = init_workspace_project(workspace, "alpha-project", marker="alpha-marker.txt")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--scheduler-policy", "path-order", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatched"
    assert payload["scheduler_policy"] == "path-order"
    assert [item["project"] for item in payload["queue"]] == ["alpha-project", "zeta-project"]
    assert payload["selected"]["project"] == "alpha-project"
    assert payload["selected"]["selected_reason"]["code"] == "path_order_first_eligible"
    assert payload["eligible_count"] == 2
    assert (alpha / "alpha-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not (zeta / "zeta-marker.txt").exists()

    zeta_item = next(item for item in payload["queue"] if item["project"] == "zeta-project")
    assert zeta_item["eligible"] is True
    assert zeta_item["dispatch_status"] == "skipped"
    assert {reason["code"] for reason in zeta_item["skip_reasons"]} == {"one_project_per_invocation"}

    report = workspace / payload["dispatch_report"]
    sidecar = workspace / payload["dispatch_report_json"]
    assert report.exists()
    assert sidecar.exists()
    report_text = report.read_text(encoding="utf-8")
    assert "Workspace Drive Dispatch Report" in report_text
    assert "one_project_per_invocation" in report_text
    assert json.loads(sidecar.read_text(encoding="utf-8"))["status"] == "dispatched"


def test_local_full_lifecycle_unattended_smoke(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    project = workspace / "local-full-lifecycle-smoke-project"
    project.mkdir(parents=True)

    assert cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Local Full Lifecycle Smoke",
            "--profile",
            "python-agent",
            "--goal",
            "Build a deterministic local artifact and expose unattended run evidence.",
            "--experience-kind",
            "cli",
            "--stage-count",
            "1",
            "--materialize",
            "--json",
        ]
    ) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "materialized"
    assert planned["goal_intake"]["safety"]["allow_live_services"] is False
    assert Harness(project).validate_roadmap()["status"] == "passed"

    seed_local_full_lifecycle_smoke_task(project)
    validation = Harness(project).validate_roadmap()
    assert validation["status"] == "passed"

    assert cli_main(
        [
            "workspace-drive",
            "--workspace",
            str(workspace),
            "--scheduler-policy",
            "path-order",
            "--max-tasks",
            "2",
            "--time-budget-seconds",
            "60",
            "--json",
        ]
    ) == 0
    dispatch = json.loads(capsys.readouterr().out)
    drive = dispatch["drive"]
    result = drive["results"][0]

    assert dispatch["status"] == "dispatched"
    assert dispatch["selected"]["project"] == "Local Full Lifecycle Smoke"
    assert dispatch["selected"]["drive_status"] == "completed"
    assert dispatch["lease"]["status"] == "released"
    assert drive["status"] == "completed"
    assert drive["message"] == "Roadmap queue is empty."
    assert drive["checkpoint_readiness"]["blocking"] is False
    assert drive["failure_isolation"]["unresolved_count"] == 0
    assert result["status"] == "passed"
    assert result["task"]["id"] == "local-full-lifecycle-artifact"
    assert [run["phase"] for run in result["runs"]] == ["implementation", "acceptance-1", "e2e"]
    assert all(run["status"] == "passed" and run["executor"] == "shell" for run in result["runs"])

    artifact_path = project / "artifacts/local-full-lifecycle/implementation.txt"
    e2e_path = project / "artifacts/local-full-lifecycle/e2e-evidence.json"
    assert artifact_path.read_text(encoding="utf-8") == "implemented\n"
    assert json.loads(e2e_path.read_text(encoding="utf-8")) == {
        "artifact": "artifacts/local-full-lifecycle/implementation.txt",
        "status": "passed",
    }

    manifest = task_manifest(project, result)
    assert manifest["status"] == "passed"
    assert manifest["task_id"] == "local-full-lifecycle-artifact"
    assert [run["phase"] for run in manifest["runs"]] == ["implementation", "acceptance-1", "e2e"]
    assert next(run for run in manifest["runs"] if run["phase"] == "e2e")["status"] == "passed"
    assert manifest["safety"]["git_preflight"]["status"] == "skipped"
    assert manifest["safety"]["file_scope_guard"]["status"] == "skipped"
    policy_summary = manifest["policy_decision_summary"]
    assert policy_summary["blocking"] == []
    assert policy_summary["requires_approval"] == []
    assert policy_summary["by_outcome"].get("denied", 0) == 0
    assert policy_summary["by_outcome"].get("requires_approval", 0) == 0
    assert {"agent_approval", "command_policy", "executor_policy", "git_preflight", "file_scope_guard"}.issubset(
        set(policy_summary["by_kind"])
    )
    assert "failure_isolation" not in manifest

    report = (project / result["report"]).read_text(encoding="utf-8")
    assert "# Task Report: local-full-lifecycle-artifact" in report
    assert "### implementation: write local artifact" in report
    assert "### e2e: local lifecycle e2e evidence" in report
    assert "## Policy Decisions" in report

    index = Harness(project).manifest_index()
    assert index["manifest_count"] == 1
    assert index["status_counts"] == {"passed": 1}
    assert index["latest_by_task"] == {"local-full-lifecycle-artifact": result["manifest"]}
    assert index["failure_isolation"]["isolated_count"] == 0
    assert index["policy_decision_summary"]["blocking"] == []

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    dashboard = status["runtime_dashboard"]
    scorecard = status["goal_gap_scorecard"]
    scorecard_by_id = scorecard_category_map(scorecard)

    assert status["next_task"] is None
    assert status["manifest_index"]["manifest_count"] == 1
    assert status["failure_isolation"]["unresolved_count"] == 0
    assert status["checkpoint_readiness"]["kind"] == "engineering-harness.checkpoint-readiness"
    assert status["checkpoint_readiness"]["blocking"] is False
    assert dashboard["checkpoint_readiness"] == status["checkpoint_readiness"]
    assert dashboard["failure_isolation"]["unresolved_count"] == 0
    assert dashboard["workspace_dispatch"]["selected"]["project"] == "Local Full Lifecycle Smoke"
    assert dashboard["workspace_dispatch"]["latest_report"]["json_path"] == dispatch["dispatch_report_json"]
    assert dashboard["latest_reports"]["task_reports"]["files"][0]["json_path"] == result["manifest"]
    assert dashboard["latest_reports"]["drive_reports"]["files"][0]["json_path"] == dispatch["selected"]["drive_report_json"]
    assert dashboard["goal_gap_scorecard"] == scorecard
    assert scorecard_by_id["real_e2e_evidence"]["status"] == "complete"
    assert scorecard_by_id["failure_isolation"]["status"] == "complete"
    assert scorecard_by_id["runtime_dashboard"]["status"] == "complete"
    assert scorecard_by_id["workspace_dispatch_fairness_backoff"]["status"] == "complete"


def test_workspace_dispatch_checkpoint_readiness_visibility(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dirty = init_workspace_project(workspace, "aa-checkpoint-dirty", marker="dirty-should-not-run.txt")
    clean = init_workspace_project(workspace, "bb-checkpoint-clean", marker="clean-marker.txt")
    dirty_roadmap_path = dirty / ".engineering/roadmap.yaml"
    dirty_roadmap = json.loads(dirty_roadmap_path.read_text(encoding="utf-8"))
    dirty_roadmap["milestones"][0]["tasks"][0]["file_scope"] = ["src/**"]
    dirty_roadmap_path.write_text(json.dumps(dirty_roadmap), encoding="utf-8")
    init_git_repo(dirty)
    (dirty / "user-notes.txt").write_text("operator draft", encoding="utf-8")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--max-tasks", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["project"] == "bb-checkpoint-clean"
    by_project = {item["project"]: item for item in payload["queue"]}
    dirty_item = by_project["aa-checkpoint-dirty"]
    assert dirty_item["eligible"] is False
    assert dirty_item["checkpoint_readiness"]["blocking"] is True
    assert dirty_item["checkpoint_readiness"]["blocking_paths"] == ["user-notes.txt"]
    assert "checkpoint_not_ready" in {reason["code"] for reason in dirty_item["skip_reasons"]}
    assert not (dirty / "dirty-should-not-run.txt").exists()
    assert (clean / "clean-marker.txt").read_text(encoding="utf-8") == "ok"

    sidecar = json.loads((workspace / payload["dispatch_report_json"]).read_text(encoding="utf-8"))
    sidecar_dirty = next(item for item in sidecar["queue"] if item["project"] == "aa-checkpoint-dirty")
    assert sidecar_dirty["checkpoint_readiness"]["recommended_action"] == dirty_item["checkpoint_readiness"][
        "recommended_action"
    ]
    assert "Checkpoint readiness" in (workspace / payload["dispatch_report"]).read_text(encoding="utf-8")

    assert cli_main(["status", "--project-root", str(clean), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    dashboard_dirty = next(
        item
        for item in status_payload["runtime_dashboard"]["workspace_dispatch"]["queue"]
        if item["project"] == "aa-checkpoint-dirty"
    )
    assert dashboard_dirty["checkpoint_readiness"]["blocking"] is True
    assert "checkpoint_not_ready" in dashboard_dirty["skip_codes"]


def test_workspace_dispatch_priority_score_uses_path_tie_breaker(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace_project(workspace, "zeta-priority-project", marker="zeta-priority-marker.txt")
    alpha = init_workspace_project(workspace, "alpha-priority-project", marker="alpha-priority-marker.txt")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--max-tasks", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scheduler_policy"] == "fair"
    assert payload["selected"]["project"] == "alpha-priority-project"
    queue = payload["queue"]
    assert [item["project"] for item in queue[:2]] == ["alpha-priority-project", "zeta-priority-project"]
    assert queue[0]["score"] == queue[1]["score"]
    assert queue[0]["selected_reason"]["code"] == "highest_fair_score"
    assert queue[0]["score_components"]["workspace_history"]["never_selected_bonus"] == 80
    assert (alpha / "alpha-priority-marker.txt").read_text(encoding="utf-8") == "ok"


def test_workspace_dispatch_priority_score_missing_history_fallback(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = init_workspace_project(workspace, "missing-history-score-project", marker="missing-history-marker.txt")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--max-tasks", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    selected = payload["selected"]
    assert selected["project"] == "missing-history-score-project"
    components = selected["score_components"]
    assert selected["score"] == components["total"]
    assert components["workspace_history"]["has_workspace_history"] is False
    assert components["workspace_history"]["selected_count"] == 0
    assert components["workspace_history"]["never_selected_bonus"] == 80
    assert components["pending_tasks"]["count"] == 1
    sidecar = json.loads((workspace / payload["dispatch_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["selected"]["score_components"]["workspace_history"]["has_workspace_history"] is False
    assert "Score components" in (workspace / payload["dispatch_report"]).read_text(encoding="utf-8")
    assert (project / "missing-history-marker.txt").read_text(encoding="utf-8") == "ok"


def test_workspace_dispatch_priority_score_uses_goal_gap_retrospective(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plain = init_workspace_project(workspace, "aa-plain-score-project", marker="plain-score-marker.txt")
    gap = init_workspace_project(workspace, "zz-goal-gap-score-project")

    gap_roadmap_path = gap / ".engineering/roadmap.yaml"
    gap_roadmap = json.loads(gap_roadmap_path.read_text(encoding="utf-8"))
    gap_roadmap["milestones"][0]["tasks"] = [
        {
            "id": "gap-first-task",
            "title": "Gap first task",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [{"name": "gap first marker", "command": "python3 -c \"from pathlib import Path; Path('gap-first-marker.txt').write_text('ok')\""}],
        },
        {
            "id": "gap-second-task",
            "title": "Gap second task",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [{"name": "gap second marker", "command": "python3 -c \"from pathlib import Path; Path('gap-second-marker.txt').write_text('ok')\""}],
        },
    ]
    gap_roadmap_path.write_text(json.dumps(gap_roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(gap), "--max-tasks", "1", "--json"]) == 0
    drive_payload = json.loads(capsys.readouterr().out)
    assert drive_payload["goal_gap_retrospective"]["remaining_risks"]

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--max-tasks", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["project"] == "zz-goal-gap-score-project"
    gap_item = next(item for item in payload["queue"] if item["project"] == "zz-goal-gap-score-project")
    plain_item = next(item for item in payload["queue"] if item["project"] == "aa-plain-score-project")
    goal_gap = gap_item["score_components"]["goal_gap"]
    assert goal_gap["source"] == drive_payload["drive_report_json"]
    assert goal_gap["points"] > 0
    assert gap_item["score"] > plain_item["score"]
    assert not (plain / "plain-score-marker.txt").exists()
    assert (gap / "gap-second-marker.txt").read_text(encoding="utf-8") == "ok"


def test_workspace_dispatch_fair_scheduler_prioritizes_starved_project(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = init_workspace_project(workspace, "aa-fair-cooldown-project")
    second = init_workspace_project(workspace, "bb-starved-project", marker="second-starved-marker.txt")

    first_roadmap_path = first / ".engineering/roadmap.yaml"
    first_roadmap = json.loads(first_roadmap_path.read_text(encoding="utf-8"))
    first_roadmap["milestones"][0]["tasks"] = [
        {
            "id": "first-task",
            "title": "First task",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [{"name": "first marker", "command": "python3 -c \"from pathlib import Path; Path('first-marker.txt').write_text('ok')\""}],
        },
        {
            "id": "second-task",
            "title": "Second task",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [{"name": "second marker", "command": "python3 -c \"from pathlib import Path; Path('second-marker.txt').write_text('ok')\""}],
        },
    ]
    first_roadmap_path.write_text(json.dumps(first_roadmap), encoding="utf-8")

    tick1_exit, tick1 = run_workspace_drive_json(capsys, workspace, "--max-tasks", "1")
    tick2_exit, tick2 = run_workspace_drive_json(capsys, workspace, "--max-tasks", "1")

    assert [tick1_exit, tick2_exit] == [0, 0]
    assert tick1["selected"]["project"] == "aa-fair-cooldown-project"
    assert tick2["selected"]["project"] == "bb-starved-project"
    first_tick2 = next(item for item in tick2["queue"] if item["project"] == "aa-fair-cooldown-project")
    second_tick2 = next(item for item in tick2["queue"] if item["project"] == "bb-starved-project")
    assert first_tick2["eligible"] is True
    assert second_tick2["eligible"] is True
    assert first_tick2["score_components"]["cooldown"]["active"] is True
    assert first_tick2["score_components"]["cooldown"]["points"] < 0
    assert second_tick2["score_components"]["workspace_history"]["selected_count"] == 0
    assert second_tick2["score"] > first_tick2["score"]
    assert (first / "first-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not (first / "second-marker.txt").exists()
    assert (second / "second-starved-marker.txt").read_text(encoding="utf-8") == "ok"


def test_workspace_dispatch_backoff_selects_alternate_project(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    failed = init_workspace_project(workspace, "aa-nonproductive-failed-project")
    alternate = init_workspace_project(workspace, "bb-alternate-after-failure")
    configure_workspace_self_iteration_project(failed, "raise SystemExit(7)\n")
    configure_workspace_self_iteration_project(
        alternate,
        workspace_backoff_planner_source(workspace_backoff_stage("alternate-stage", "alternate-task")),
    )

    first_exit, first = run_workspace_drive_json(capsys, workspace, "--self-iterate", "--max-self-iterations", "1")
    failed_after_first = project_text_snapshot(failed)
    second_exit, second = run_workspace_drive_json(capsys, workspace, "--self-iterate", "--max-self-iterations", "1")

    assert first_exit == 1
    assert first["selected"]["project"] == "aa-nonproductive-failed-project"
    assert second_exit == 0
    assert second["selected"]["project"] == "bb-alternate-after-failure"
    assert project_text_snapshot(failed) == failed_after_first
    failed_item = next(item for item in second["queue"] if item["project"] == "aa-nonproductive-failed-project")
    assert failed_item["eligible"] is True
    assert failed_item["dispatch_status"] == "skipped"
    backoff = failed_item["backoff"]
    assert backoff["decision"] == "active_penalty"
    assert backoff["active"] is True
    assert backoff["reason"] == "drive_failed"
    assert backoff["source_report"] == first["dispatch_report_json"]
    assert backoff["source_drive_report_json"] == first["selected"]["drive_report_json"]
    assert backoff["age_seconds"] >= 0
    assert backoff["threshold_seconds"] == 3600
    assert backoff["expires_at"]
    assert failed_item["score_components"]["nonproductive_backoff"] == backoff
    assert failed_item["score"] < next(
        item for item in second["queue"] if item["project"] == "bb-alternate-after-failure"
    )["score"]

    sidecar = json.loads((workspace / second["dispatch_report_json"]).read_text(encoding="utf-8"))
    sidecar_failed = next(item for item in sidecar["queue"] if item["project"] == "aa-nonproductive-failed-project")
    assert sidecar_failed["backoff"]["reason"] == "drive_failed"
    report_text = (workspace / second["dispatch_report"]).read_text(encoding="utf-8")
    assert "Nonproductive backoff" in report_text
    assert "drive_failed" in report_text

    assert cli_main(["status", "--project-root", str(failed), "--json"]) == 0
    dashboard = json.loads(capsys.readouterr().out)["runtime_dashboard"]["workspace_dispatch"]
    dashboard_failed = next(item for item in dashboard["queue"] if item["project"] == "aa-nonproductive-failed-project")
    assert dashboard_failed["backoff"]["active"] is True
    assert dashboard_failed["backoff"]["source_report"] == first["dispatch_report_json"]


def test_workspace_dispatch_planner_validation_backoff_selects_alternate_project(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rejected = init_workspace_project(workspace, "aa-planner-validation-project")
    alternate = init_workspace_project(workspace, "bb-planner-validation-alternate")
    invalid_stage = {"id": "invalid-planner-stage", "title": "Invalid Planner Stage", "tasks": []}
    configure_workspace_self_iteration_project(
        rejected,
        workspace_backoff_planner_source(invalid_stage),
    )
    configure_workspace_self_iteration_project(
        alternate,
        workspace_backoff_planner_source(workspace_backoff_stage("valid-planner-stage", "valid-planner-task")),
    )

    first_exit, first = run_workspace_drive_json(capsys, workspace, "--self-iterate", "--max-self-iterations", "1")
    second_exit, second = run_workspace_drive_json(capsys, workspace, "--self-iterate", "--max-self-iterations", "1")

    assert first_exit == 1
    assert first["selected"]["project"] == "aa-planner-validation-project"
    assert first["drive"]["self_iterations"][0]["status"] == "rejected"
    assert first["drive"]["self_iterations"][0]["validation"]["status"] == "failed"
    assert second_exit == 0
    assert second["selected"]["project"] == "bb-planner-validation-alternate"
    rejected_item = next(item for item in second["queue"] if item["project"] == "aa-planner-validation-project")
    assert rejected_item["backoff"]["active"] is True
    assert rejected_item["backoff"]["reason"] == "planner_validation_failed"
    assert rejected_item["backoff"]["outcome"]["progress"]["planned_self_iteration_count"] == 0


def test_workspace_dispatch_nonproductive_budget_without_progress_backoff(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exhausted = init_workspace_project(workspace, "aa-budget-without-progress-project")
    alternate = init_workspace_project(workspace, "bb-budget-alternate-project")
    configure_workspace_continuation_project(
        exhausted,
        [
            workspace_backoff_stage("budget-stage-a", "budget-task-a"),
            workspace_backoff_stage("budget-stage-b", "budget-task-b"),
        ],
    )
    configure_workspace_continuation_project(
        alternate,
        [workspace_backoff_stage("alternate-budget-stage", "alternate-budget-task")],
    )

    first_exit, first = run_workspace_drive_json(
        capsys,
        workspace,
        "--rolling",
        "--max-continuations",
        "0",
    )
    second_exit, second = run_workspace_drive_json(
        capsys,
        workspace,
        "--rolling",
        "--max-continuations",
        "1",
    )

    assert first_exit == 0
    assert first["selected"]["project"] == "aa-budget-without-progress-project"
    assert first["drive"]["status"] == "budget_exhausted"
    assert first["drive"]["results"] == []
    assert first["drive"]["continuations"] == []
    assert second_exit == 0
    assert second["selected"]["project"] == "bb-budget-alternate-project"
    exhausted_item = next(item for item in second["queue"] if item["project"] == "aa-budget-without-progress-project")
    assert exhausted_item["backoff"]["active"] is True
    assert exhausted_item["backoff"]["reason"] == "budget_without_progress"
    assert exhausted_item["backoff"]["outcome"]["progress"]["useful_progress"] is False


def test_workspace_dispatch_nonproductive_backoff_ignores_productive_budget_exhaustion(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    productive = init_workspace_project(workspace, "aa-productive-budget-project")
    alternate = init_workspace_project(workspace, "bb-productive-budget-alternate", marker="productive-alternate.txt")
    roadmap_path = productive / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "productive-first",
            "title": "Productive First",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [
                {
                    "name": "first marker",
                    "command": "python3 -c \"from pathlib import Path; Path('productive-first.txt').write_text('ok')\"",
                }
            ],
        },
        {
            "id": "productive-second",
            "title": "Productive Second",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [
                {
                    "name": "second marker",
                    "command": "python3 -c \"from pathlib import Path; Path('productive-second.txt').write_text('ok')\"",
                }
            ],
        },
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    first_exit, first = run_workspace_drive_json(capsys, workspace, "--max-tasks", "1")
    second_exit, second = run_workspace_drive_json(capsys, workspace, "--max-tasks", "1")

    assert first_exit == 0
    assert first["drive"]["status"] == "budget_exhausted"
    assert first["drive"]["results"][0]["status"] == "passed"
    productive_item = next(item for item in second["queue"] if item["project"] == "aa-productive-budget-project")
    assert second_exit == 0
    assert productive_item["backoff"]["active"] is False
    assert productive_item["backoff"]["decision"] == "productive"
    assert productive_item["backoff"]["points"] == 0
    assert productive_item["backoff"]["outcome"]["productive"] is True
    assert productive_item["backoff"]["outcome"]["progress"]["completed_result_count"] == 1
    assert (productive / "productive-first.txt").read_text(encoding="utf-8") == "ok"


def test_workspace_dispatch_backoff_expiry_restores_fair_order(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exhausted = init_workspace_project(workspace, "aa-expired-backoff-project")
    alternate = init_workspace_project(workspace, "bb-expired-backoff-alternate")
    configure_workspace_continuation_project(
        exhausted,
        [workspace_backoff_stage(f"expired-stage-{index}", f"expired-task-{index}") for index in range(5)],
    )
    configure_workspace_continuation_project(
        alternate,
        [workspace_backoff_stage("expired-alternate-stage", "expired-alternate-task")],
    )

    first_exit, first = run_workspace_drive_json(
        capsys,
        workspace,
        "--rolling",
        "--max-continuations",
        "0",
        "--nonproductive-backoff-seconds",
        "1",
    )
    sidecar_path = workspace / first["dispatch_report_json"]
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["started_at"] = "2000-01-01T00:00:00Z"
    sidecar["finished_at"] = "2000-01-01T00:00:00Z"
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8")

    second_exit, second = run_workspace_drive_json(
        capsys,
        workspace,
        "--rolling",
        "--max-continuations",
        "0",
        "--nonproductive-backoff-seconds",
        "1",
    )

    assert first_exit == 0
    assert second_exit == 0
    assert second["selected"]["project"] == "aa-expired-backoff-project"
    exhausted_item = next(item for item in second["queue"] if item["project"] == "aa-expired-backoff-project")
    assert exhausted_item["backoff"]["decision"] == "expired"
    assert exhausted_item["backoff"]["active"] is False
    assert exhausted_item["backoff"]["reason"] == "budget_without_progress"
    assert exhausted_item["backoff"]["points"] == 0
    assert exhausted_item["score"] > next(
        item for item in second["queue"] if item["project"] == "bb-expired-backoff-alternate"
    )["score"]


def test_workspace_dispatch_priority_score_keeps_approval_and_failure_skips_blocking(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    approval = init_workspace_project(workspace, "aa-approval-score-blocked", marker="approval-score-marker.txt")
    isolated = init_workspace_project(workspace, "bb-isolated-score-blocked")
    eligible = init_workspace_project(workspace, "cc-score-eligible", marker="eligible-score-marker.txt")

    approval_roadmap_path = approval / ".engineering/roadmap.yaml"
    approval_roadmap = json.loads(approval_roadmap_path.read_text(encoding="utf-8"))
    approval_roadmap["milestones"][0]["tasks"][0]["manual_approval_required"] = True
    approval_roadmap_path.write_text(json.dumps(approval_roadmap), encoding="utf-8")
    assert cli_main(["drive", "--project-root", str(approval), "--json"]) == 1
    capsys.readouterr()

    isolated_roadmap_path = isolated / ".engineering/roadmap.yaml"
    isolated_roadmap = json.loads(isolated_roadmap_path.read_text(encoding="utf-8"))
    isolated_task = isolated_roadmap["milestones"][0]["tasks"][0]
    isolated_task["max_attempts"] = 1
    isolated_task["acceptance"][0]["command"] = "python3 -c \"raise SystemExit(9)\""
    isolated_roadmap_path.write_text(json.dumps(isolated_roadmap), encoding="utf-8")
    result = Harness(isolated).run_task(Harness(isolated).next_task(), allow_agent=True)
    assert result["status"] == "failed"

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--max-tasks", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    by_project = {item["project"]: item for item in payload["queue"]}
    assert payload["selected"]["project"] == "cc-score-eligible"
    assert by_project["aa-approval-score-blocked"]["score"] is None
    assert by_project["aa-approval-score-blocked"]["score_components"]["blocked"] is True
    assert "waiting_on_approvals" in by_project["aa-approval-score-blocked"]["score_components"]["skip_codes"]
    assert by_project["bb-isolated-score-blocked"]["score"] is None
    assert by_project["bb-isolated-score-blocked"]["score_components"]["blocked"] is True
    assert "unresolved_isolated_failures" in by_project["bb-isolated-score-blocked"]["score_components"]["skip_codes"]
    assert not (approval / "approval-score-marker.txt").exists()
    assert not (isolated / "bb-isolated-score-blocked-marker.txt").exists()
    assert (eligible / "eligible-score-marker.txt").read_text(encoding="utf-8") == "ok"


def test_runtime_dashboard_workspace_dispatch_priority_score_visibility(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alpha = init_workspace_project(workspace, "alpha-score-dashboard", marker="alpha-score-dashboard-marker.txt")
    init_workspace_project(workspace, "zeta-score-dashboard", marker="zeta-score-dashboard-marker.txt")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--max-tasks", "1", "--json"]) == 0
    dispatch_payload = json.loads(capsys.readouterr().out)

    assert cli_main(["status", "--project-root", str(alpha), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    dispatch = payload["runtime_dashboard"]["workspace_dispatch"]
    assert dispatch["scheduler_policy"] == "fair"
    assert dispatch["selected"]["project"] == dispatch_payload["selected"]["project"]
    selected = next(item for item in dispatch["queue"] if item["selected"])
    assert selected["score"] == dispatch_payload["selected"]["score"]
    assert selected["score_components"]["policy"] == "fair"
    assert selected["selected_reason"]["code"] == "highest_fair_score"


def test_runtime_dashboard_dispatch_queue_and_lease_status(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    zeta = init_workspace_project(workspace, "zeta-dashboard-project", marker="zeta-dashboard-marker.txt")
    alpha = init_workspace_project(workspace, "alpha-dashboard-project", marker="alpha-dashboard-marker.txt")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 0
    dispatch_payload = json.loads(capsys.readouterr().out)
    assert dispatch_payload["status"] == "dispatched"
    assert dispatch_payload["selected"]["project"] == "alpha-dashboard-project"

    assert cli_main(["status", "--project-root", str(alpha), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    dashboard = payload["runtime_dashboard"]
    dispatch = dashboard["workspace_dispatch"]
    assert dispatch["status"] == "reported"
    assert dispatch["workspace_root"] == str(workspace.resolve())
    assert dispatch["latest_report"]["path"] == dispatch_payload["dispatch_report"]
    assert dispatch["latest_report_lease"]["status"] == "released"
    assert dispatch["queue_count"] == 2
    assert {item["project"] for item in dispatch["queue"]} == {
        "alpha-dashboard-project",
        "zeta-dashboard-project",
    }
    selected = next(item for item in dispatch["queue"] if item["selected"])
    assert selected["project"] == "alpha-dashboard-project"
    assert dashboard["latest_reports"]["workspace_dispatch_reports"]["files"][0]["path"] == dispatch_payload["dispatch_report"]
    assert dashboard["latest_reports"]["workspace_dispatch_reports"]["files"][0]["json_path"] == dispatch_payload["dispatch_report_json"]
    assert (alpha / "alpha-dashboard-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not (zeta / "zeta-dashboard-marker.txt").exists()


def test_workspace_dispatch_rejects_fresh_lease(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = init_workspace_project(workspace, "fresh-lease-project", marker="fresh-marker.txt")
    lease = write_workspace_dispatch_lease(workspace, owner_pid=os.getpid())

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "lease_held"
    assert payload["queue"] == []
    assert payload["lease"]["status"] == "held"
    assert payload["lease"]["acquired"] is False
    assert payload["lease"]["assessment"]["pid_alive"] is True
    assert payload["lease"]["assessment"]["holder"]["owner_pid"] == lease["owner_pid"]
    assert (workspace / payload["dispatch_report"]).exists()
    assert (workspace / payload["dispatch_report_json"]).exists()
    assert "Workspace Dispatch Lease" in (workspace / payload["dispatch_report"]).read_text(encoding="utf-8")
    sidecar = json.loads((workspace / payload["dispatch_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["status"] == "lease_held"
    assert sidecar["lease"]["assessment"]["holder"]["workspace"] == str(workspace.resolve())
    assert workspace_dispatch_lease_path(workspace).exists()
    assert not (project / "fresh-marker.txt").exists()


def test_workspace_dispatch_recovers_stale_pid_lease(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = init_workspace_project(workspace, "stale-pid-project", marker="stale-pid-marker.txt")
    write_workspace_dispatch_lease(workspace, owner_pid=unused_pid())

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatched"
    assert payload["selected"]["project"] == "stale-pid-project"
    assert payload["lease"]["status"] == "released"
    assert payload["lease"]["recovered"] is True
    assert payload["lease"]["recovery"]["reason"] == "pid_gone"
    assert payload["lease"]["selected_project"]["project"] == "stale-pid-project"
    assert (project / "stale-pid-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not workspace_dispatch_lease_dir(workspace).exists()


def test_workspace_dispatch_recovers_stale_heartbeat_lease(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = init_workspace_project(workspace, "stale-heartbeat-project", marker="stale-heartbeat-marker.txt")
    write_workspace_dispatch_lease(
        workspace,
        owner_pid=os.getpid(),
        heartbeat_at="2000-01-01T00:00:00Z",
        stale_after_seconds=1,
    )

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatched"
    assert payload["lease"]["status"] == "released"
    assert payload["lease"]["recovery"]["reason"] == "heartbeat_stale"
    assert payload["lease"]["recovery"]["assessment"]["pid_alive"] is True
    assert payload["lease"]["heartbeat_count"] >= 4
    assert (project / "stale-heartbeat-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not workspace_dispatch_lease_dir(workspace).exists()


def test_workspace_dispatch_stale_running_recovery_evidence(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stale_project = init_workspace_project(
        workspace,
        "aa-stale-running-project",
        marker="stale-running-workspace-marker.txt",
    )
    alternate = init_workspace_project(workspace, "bb-alternate-project", marker="alternate-should-not-run.txt")
    roadmap_path = stale_project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(stale_project)
    assert harness.start_drive()["started"] is True
    previous_pid = unused_pid()
    state = harness_state(stale_project)
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(stale_project, state)

    assert cli_main(
        [
            "workspace-drive",
            "--workspace",
            str(workspace),
            "--scheduler-policy",
            "path-order",
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatched"
    assert payload["selected"]["project"] == "aa-stale-running-project"
    recovery = payload["selected"]["stale_running_recovery"]
    assert recovery["status"] == "recovered"
    assert recovery["reason"] == "dead_pid_and_stale_heartbeat"
    assert recovery["previous_pid"] == previous_pid
    assert payload["stale_running_recoveries"][0]["previous_pid"] == previous_pid

    stale_item = next(item for item in payload["queue"] if item["project"] == "aa-stale-running-project")
    assert stale_item["eligible"] is True
    assert stale_item["stale_running_recovery"]["previous_pid"] == previous_pid
    assert stale_item["summary"]["drive_control"]["stale_running_recovery"]["previous_pid"] == previous_pid
    alternate_item = next(item for item in payload["queue"] if item["project"] == "bb-alternate-project")
    assert "one_project_per_invocation" in {reason["code"] for reason in alternate_item["skip_reasons"]}
    assert (stale_project / "stale-running-workspace-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not (alternate / "alternate-should-not-run.txt").exists()

    sidecar = json.loads((workspace / payload["dispatch_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["stale_running_recoveries"][0]["previous_pid"] == previous_pid
    assert sidecar["selected"]["stale_running_recovery"]["previous_pid"] == previous_pid
    report_text = (workspace / payload["dispatch_report"]).read_text(encoding="utf-8")
    assert "Project Stale Running Recovery" in report_text
    assert "dead_pid_and_stale_heartbeat" in report_text

    assert cli_main(["status", "--project-root", str(stale_project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    dashboard_drive = status_payload["runtime_dashboard"]["drive_control"]
    assert dashboard_drive["stale_running_recovery"]["previous_pid"] == previous_pid
    dispatch = status_payload["runtime_dashboard"]["workspace_dispatch"]
    assert dispatch["stale_running_recoveries"][0]["previous_pid"] == previous_pid
    dashboard_item = next(item for item in dispatch["queue"] if item["project"] == "aa-stale-running-project")
    assert dashboard_item["stale_running_recovery"]["previous_pid"] == previous_pid


def test_workspace_dispatch_releases_lease_on_completion_with_json_report_evidence(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = init_workspace_project(workspace, "release-project", marker="release-marker.txt")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatched"
    assert payload["lease"]["status"] == "released"
    assert payload["lease"]["release"]["status"] == "released"
    assert payload["lease"]["owner_pid"] == os.getpid()
    assert payload["lease"]["selected_project"]["project"] == "release-project"
    assert payload["lease"]["command_options"]["allow_live"] is False
    assert payload["lease"]["command_options"]["allow_manual"] is False
    assert payload["lease"]["command_options"]["allow_agent"] is False
    assert payload["limits"]["push_after_task"] is False
    assert payload["limits"]["commit_after_task"] is False
    assert payload["lease"]["heartbeat_count"] >= 4
    report_text = (workspace / payload["dispatch_report"]).read_text(encoding="utf-8")
    assert "Machine-readable lease" in report_text
    sidecar = json.loads((workspace / payload["dispatch_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["lease"]["status"] == "released"
    assert (project / "release-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not workspace_dispatch_lease_dir(workspace).exists()


def test_workspace_dispatch_queue_explains_safety_skips(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paused = init_workspace_project(workspace, "paused-project", marker="paused-marker.txt")
    cancelled = init_workspace_project(workspace, "cancelled-project", marker="cancelled-marker.txt")
    stale = init_workspace_project(workspace, "stale-project", marker="stale-marker.txt")
    approval = init_workspace_project(workspace, "approval-project", marker="approval-marker.txt")
    missing = workspace / "candidate-without-roadmap"
    missing.mkdir()
    (missing / "package.json").write_text("{}", encoding="utf-8")
    invalid = workspace / "invalid-project"
    (invalid / ".engineering").mkdir(parents=True)
    (invalid / ".engineering/roadmap.yaml").write_text(
        json.dumps(
            {
                "version": 1,
                "project": "invalid-project",
                "profile": "python-agent",
                "milestones": [{"id": "broken", "tasks": [{"id": "broken-task"}]}],
            }
        ),
        encoding="utf-8",
    )
    outside_roadmap = tmp_path / "outside-roadmap.json"
    outside_roadmap.write_text(
        json.dumps(roadmap_without_experience("outside-roadmap-project", task_title="Outside scope")),
        encoding="utf-8",
    )
    outside = workspace / "roadmap-symlink-project"
    (outside / ".engineering").mkdir(parents=True)
    (outside / ".engineering/roadmap.yaml").symlink_to(outside_roadmap)

    assert cli_main(["pause", "--project-root", str(paused), "--reason", "test"]) == 0
    assert cli_main(["cancel", "--project-root", str(cancelled), "--reason", "test"]) == 0
    stale_harness = Harness(stale)
    assert stale_harness.start_drive()["started"] is True
    stale_state = harness_state(stale)
    stale_state["drive_control"]["pid"] = os.getpid()
    stale_state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(stale, stale_state)

    approval_roadmap_path = approval / ".engineering/roadmap.yaml"
    approval_roadmap = json.loads(approval_roadmap_path.read_text(encoding="utf-8"))
    approval_roadmap["milestones"][0]["tasks"][0]["manual_approval_required"] = True
    approval_roadmap_path.write_text(json.dumps(approval_roadmap), encoding="utf-8")
    assert cli_main(["drive", "--project-root", str(approval)]) == 1
    capsys.readouterr()

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_eligible_project"
    by_project = {item["project"]: item for item in payload["queue"]}

    def skip_codes(project_name: str) -> set[str]:
        return {reason["code"] for reason in by_project[project_name]["skip_reasons"]}

    assert "paused" in skip_codes("paused-project")
    assert "cancelled" in skip_codes("cancelled-project")
    assert "stale_running" in skip_codes("stale-project")
    assert "waiting_on_approvals" in skip_codes("approval-project")
    assert "missing_roadmap" in skip_codes("candidate-without-roadmap")
    assert "invalid_roadmap" in skip_codes("invalid-project")
    assert "outside_local_scope" in skip_codes("roadmap-symlink-project")
    assert not (paused / "paused-marker.txt").exists()
    assert not (cancelled / "cancelled-marker.txt").exists()
    assert not (stale / "stale-marker.txt").exists()
    assert not (approval / "approval-marker.txt").exists()
    assert (workspace / payload["dispatch_report"]).exists()


def test_workspace_drive_skips_without_mutating_skipped_project(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paused = init_workspace_project(workspace, "aa-paused-project", marker="paused-marker.txt")
    eligible = init_workspace_project(workspace, "bb-eligible-project", marker="eligible-marker.txt")
    assert cli_main(["pause", "--project-root", str(paused), "--reason", "operator"]) == 0
    capsys.readouterr()

    paused_state_path = paused / ".engineering/state/harness-state.json"
    paused_roadmap_path = paused / ".engineering/roadmap.yaml"
    state_before = paused_state_path.read_text(encoding="utf-8")
    roadmap_before = paused_roadmap_path.read_text(encoding="utf-8")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["project"] == "bb-eligible-project"
    assert (eligible / "eligible-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not (paused / "paused-marker.txt").exists()
    assert paused_state_path.read_text(encoding="utf-8") == state_before
    assert paused_roadmap_path.read_text(encoding="utf-8") == roadmap_before
    paused_item = next(item for item in payload["queue"] if item["project"] == "aa-paused-project")
    assert paused_item["dispatch_status"] == "skipped"
    assert {reason["code"] for reason in paused_item["skip_reasons"]} == {"paused"}


def test_workspace_multi_project_scheduler_e2e(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    failed = init_workspace_project(workspace, "aa-e2e-nonproductive-project")
    alternate = init_workspace_project(workspace, "bb-e2e-alternate-project")
    paused = init_workspace_project(workspace, "cc-e2e-paused-project", marker="paused-e2e-marker.txt")
    configure_workspace_self_iteration_project(failed, "raise SystemExit(7)\n")
    configure_workspace_self_iteration_project(
        alternate,
        workspace_backoff_planner_source(workspace_backoff_stage("e2e-alternate-stage", "e2e-alternate-task")),
    )
    assert cli_main(["pause", "--project-root", str(paused), "--reason", "operator"]) == 0
    capsys.readouterr()
    paused_snapshot = project_text_snapshot(paused)

    write_workspace_dispatch_lease(workspace, owner_pid=os.getpid())
    lease_exit, lease_payload = run_workspace_drive_json(
        capsys,
        workspace,
        "--self-iterate",
        "--max-self-iterations",
        "1",
    )
    assert lease_exit == 1
    assert lease_payload["status"] == "lease_held"
    assert lease_payload["queue_summary"]["item_count"] == 0
    assert lease_payload["lease"]["assessment"]["holder"]["owner_pid"] == os.getpid()
    assert not (alternate / "e2e-alternate-task.txt").exists()
    workspace_dispatch_lease_path(workspace).unlink()
    workspace_dispatch_lease_dir(workspace).rmdir()

    first_exit, first = run_workspace_drive_json(
        capsys,
        workspace,
        "--self-iterate",
        "--max-self-iterations",
        "1",
    )
    failed_after_first = project_text_snapshot(failed)
    second_exit, second = run_workspace_drive_json(
        capsys,
        workspace,
        "--self-iterate",
        "--max-self-iterations",
        "1",
    )

    assert first_exit == 1
    assert first["selected"]["project"] == "aa-e2e-nonproductive-project"
    assert first["selected"]["project_lease"]["status"] == "idle"
    assert first["selected"]["resource_budget"]["per_invocation"]["max_self_iterations"] == 1
    assert second_exit == 0
    assert second["selected"]["project"] == "bb-e2e-alternate-project"
    assert second["queue_summary"]["selected_project"] == "bb-e2e-alternate-project"
    assert second["queue_summary"]["item_count"] == 3
    assert (alternate / ".engineering/roadmap.yaml").exists()
    assert project_text_snapshot(failed) == failed_after_first
    assert project_text_snapshot(paused) == paused_snapshot
    assert not (paused / "paused-e2e-marker.txt").exists()

    by_project = {item["project"]: item for item in second["queue"]}
    failed_item = by_project["aa-e2e-nonproductive-project"]
    paused_item = by_project["cc-e2e-paused-project"]
    assert failed_item["dispatch_status"] == "skipped"
    assert failed_item["backoff"]["active"] is True
    assert failed_item["backoff"]["reason"] == "drive_failed"
    assert failed_item["priority"]["starvation_prevention"]["nonproductive_backoff_active"] is True
    assert failed_item["resource_budget"]["per_invocation"]["self_iterate"] is True
    assert failed_item["project_lease"]["active"] is False
    assert failed_item["retry_backoff_summary"]["backoff_active"] is True
    assert failed_item["retry_backoff_summary"]["backoff_reason"] == "drive_failed"
    assert "paused" in {reason["code"] for reason in paused_item["skip_reasons"]}
    assert paused_item["score_components"]["blocked"] is True

    sidecar = json.loads((workspace / second["dispatch_report_json"]).read_text(encoding="utf-8"))
    sidecar_failed = next(item for item in sidecar["queue"] if item["project"] == "aa-e2e-nonproductive-project")
    assert sidecar["queue_summary"]["selected_project"] == "bb-e2e-alternate-project"
    assert sidecar_failed["retry_backoff_summary"]["nonproductive_backoff"]["source_report"] == first[
        "dispatch_report_json"
    ]
    report_text = (workspace / second["dispatch_report"]).read_text(encoding="utf-8")
    assert "Resource budget" in report_text
    assert "Project lease" in report_text
    assert "Retry/backoff summary" in report_text

    assert cli_main(["status", "--project-root", str(alternate), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    dispatch = status_payload["runtime_dashboard"]["workspace_dispatch"]
    dashboard_failed = next(item for item in dispatch["queue"] if item["project"] == "aa-e2e-nonproductive-project")
    assert dispatch["queue_summary"]["selected_project"] == "bb-e2e-alternate-project"
    assert dashboard_failed["project_lease"]["status"] == "idle"
    assert dashboard_failed["retry_backoff_summary"]["backoff_active"] is True
    assert dashboard_failed["resource_budget"]["per_invocation"]["max_self_iterations"] == 1


def test_daemon_supervisor_runtime_smoke_restartable_loop_continues_without_duplicate_work(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = init_workspace_project(workspace, "restartable-loop-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    first_task = roadmap["milestones"][0]["tasks"][0]
    first_task["id"] = "daemon-first-task"
    first_task["title"] = "Daemon first task"
    first_task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; "
        "Path('daemon.log').open('a', encoding='utf-8').write('first\\n')\""
    )
    roadmap["milestones"][0]["tasks"].append(
        {
            "id": "daemon-second-task",
            "title": "Daemon second task",
            "file_scope": ["**"],
            "acceptance": [
                {
                    "name": "write daemon second marker",
                    "command": (
                        "python3 -c \"from pathlib import Path; "
                        "Path('daemon.log').open('a', encoding='utf-8').write('second\\n')\""
                    ),
                }
            ],
        }
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    first_exit, first = run_daemon_supervisor_json(
        capsys,
        workspace,
        "--scheduler-policy",
        "path-order",
        "--max-ticks",
        "1",
        "--max-tasks",
        "1",
        "--idle-sleep-seconds",
        "0",
    )

    assert first_exit == 0
    assert first["status"] == "stopped"
    assert first["stop_reason"]["code"] == "max_ticks"
    assert first["run_window"]["tick_count"] == 1
    assert first["ticks"][0]["dispatch_status"] == "dispatched"
    assert first["ticks"][0]["drive_status"] == "budget_exhausted"
    assert (project / "daemon.log").read_text(encoding="utf-8").splitlines() == ["first"]

    interrupted = daemon_supervisor_state(workspace)
    interrupted_loop_id = interrupted["loop_id"]
    interrupted["status"] = "running"
    interrupted["active"] = True
    interrupted["owner_pid"] = unused_pid()
    interrupted["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_daemon_supervisor_state(workspace, interrupted)

    second_exit, second = run_daemon_supervisor_json(
        capsys,
        workspace,
        "--scheduler-policy",
        "path-order",
        "--max-ticks",
        "2",
        "--max-tasks",
        "1",
        "--idle-sleep-seconds",
        "0",
    )

    assert second_exit == 0
    assert second["status"] == "stopped"
    assert second["restartable_loop"]["resumed_from"]["loop_id"] == interrupted_loop_id
    assert second["restartable_loop"]["recovered_previous"]["reason"] == "pid_gone"
    assert second["restartable_loop"]["completed_dispatch_reports"][0]["dispatch_report_json"] == first["ticks"][0]["dispatch_report_json"]
    assert [tick["dispatch_status"] for tick in second["ticks"]] == ["dispatched", "no_eligible_project"]
    assert second["ticks"][0]["drive_status"] == "budget_exhausted"
    assert second["ticks"][1]["decision"]["reason"] == "no_eligible_project"
    assert second["stop_reason"]["code"] == "idle_limit"
    assert (project / "daemon.log").read_text(encoding="utf-8").splitlines() == ["first", "second"]

    state = harness_state(project)
    assert state["tasks"]["daemon-first-task"]["status"] == "passed"
    assert state["tasks"]["daemon-second-task"]["status"] == "passed"
    assert len(list((project / ".engineering/reports/tasks").glob("*daemon-first-task.json"))) == 1
    assert len(list((project / ".engineering/reports/tasks").glob("*daemon-second-task.json"))) == 1

    runtime_report = workspace / second["runtime_report"]
    runtime_sidecar = workspace / second["runtime_report_json"]
    assert runtime_report.exists()
    assert runtime_sidecar.exists()
    assert "Daemon Supervisor Runtime Report" in runtime_report.read_text(encoding="utf-8")
    sidecar = json.loads(runtime_sidecar.read_text(encoding="utf-8"))
    assert sidecar["restartable_loop"]["recovered_previous"]["reason"] == "pid_gone"

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    supervisor = status_payload["runtime_dashboard"]["daemon_supervisor_runtime"]
    assert supervisor["status"] == "stopped"
    assert supervisor["stop_reason"]["code"] == "idle_limit"
    assert supervisor["restartable_loop"]["recovered_previous"]["reason"] == "pid_gone"
    assert supervisor["restartable_loop"]["completed_dispatch_report_count"] >= 3
    assert supervisor["latest_report"]["json_path"] == second["runtime_report_json"]
    assert status_payload["daemon_supervisor_runtime"]["state"]["latest_report_json"] == second["runtime_report_json"]


def test_supervisor_runtime_run_window_records_idle_sleep_decision(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    exit_code, payload = run_daemon_supervisor_json(
        capsys,
        workspace,
        "--max-ticks",
        "3",
        "--run-window-seconds",
        "60",
        "--idle-sleep-seconds",
        "7",
        "--idle-stop-count",
        "1",
    )

    assert exit_code == 0
    assert payload["status"] == "stopped"
    assert payload["run_window"]["window_seconds"] == 60
    assert payload["run_window"]["deadline_at"]
    assert payload["run_window"]["tick_count"] == 1
    assert payload["ticks"][0]["dispatch_status"] == "no_eligible_project"
    assert payload["ticks"][0]["decision"]["action"] == "stop"
    assert payload["ticks"][0]["decision"]["reason"] == "no_eligible_project"
    assert payload["ticks"][0]["decision"]["sleep_seconds"] == 7
    assert payload["stop_reason"]["code"] == "idle_limit"

    state = daemon_supervisor_state(workspace)
    assert state["run_window"]["deadline_at"] == payload["run_window"]["deadline_at"]
    assert state["last_decision"]["sleep_seconds"] == 7
    assert state["stop_reason"]["code"] == "idle_limit"


def test_workspace_supervisor_multi_tick_e2e_loop_preserves_skips_and_reports(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = init_workspace_project(workspace, "aa-ready-first", marker="first-marker.txt")
    second = init_workspace_project(workspace, "bb-ready-second", marker="second-marker.txt")
    approval = init_workspace_project(workspace, "cc-approval-blocked", marker="approval-marker.txt")
    isolated = init_workspace_project(workspace, "dd-isolated-failure")

    approval_roadmap_path = approval / ".engineering/roadmap.yaml"
    approval_roadmap = json.loads(approval_roadmap_path.read_text(encoding="utf-8"))
    approval_roadmap["milestones"][0]["tasks"][0]["manual_approval_required"] = True
    approval_roadmap_path.write_text(json.dumps(approval_roadmap), encoding="utf-8")
    capsys.readouterr()
    assert cli_main(["drive", "--project-root", str(approval), "--json"]) == 1
    approval_payload = json.loads(capsys.readouterr().out)
    assert approval_payload["status"] == "blocked"
    assert not (approval / "approval-marker.txt").exists()

    isolated_roadmap_path = isolated / ".engineering/roadmap.yaml"
    isolated_roadmap = json.loads(isolated_roadmap_path.read_text(encoding="utf-8"))
    isolated_task = isolated_roadmap["milestones"][0]["tasks"][0]
    isolated_task["max_attempts"] = 1
    isolated_task["acceptance"][0]["command"] = "python3 -c \"raise SystemExit(7)\""
    isolated_roadmap_path.write_text(json.dumps(isolated_roadmap), encoding="utf-8")
    isolated_result = Harness(isolated).run_task(Harness(isolated).next_task(), allow_agent=True)
    assert isolated_result["status"] == "failed"
    assert Harness(isolated).status_summary()["failure_isolation"]["unresolved_count"] == 1

    approval_snapshot = project_text_snapshot(approval)
    isolated_snapshot = project_text_snapshot(isolated)

    write_workspace_dispatch_lease(workspace, owner_pid=os.getpid())
    fresh_exit, fresh_payload = run_workspace_drive_json(
        capsys,
        workspace,
        "--max-tasks",
        "1",
        "--time-budget-seconds",
        "1",
    )
    assert fresh_exit == 1
    assert fresh_payload["status"] == "lease_held"
    assert fresh_payload["queue"] == []
    assert fresh_payload["lease"]["assessment"]["status"] == "held"
    assert fresh_payload["lease"]["assessment"]["pid_alive"] is True
    assert not (first / "first-marker.txt").exists()
    assert not (second / "second-marker.txt").exists()
    assert project_text_snapshot(approval) == approval_snapshot
    assert project_text_snapshot(isolated) == isolated_snapshot

    write_workspace_dispatch_lease(workspace, owner_pid=unused_pid())
    tick1_exit, tick1 = run_workspace_drive_json(
        capsys,
        workspace,
        "--max-tasks",
        "1",
        "--time-budget-seconds",
        "1",
    )
    tick2_exit, tick2 = run_workspace_drive_json(
        capsys,
        workspace,
        "--max-tasks",
        "1",
        "--time-budget-seconds",
        "1",
    )
    tick3_exit, tick3 = run_workspace_drive_json(
        capsys,
        workspace,
        "--max-tasks",
        "1",
        "--time-budget-seconds",
        "1",
    )

    assert [tick1_exit, tick2_exit, tick3_exit] == [0, 0, 0]
    assert [tick1["status"], tick2["status"], tick3["status"]] == [
        "dispatched",
        "dispatched",
        "no_eligible_project",
    ]
    assert [tick1["selected"]["project"], tick2["selected"]["project"]] == [
        "aa-ready-first",
        "bb-ready-second",
    ]
    assert tick3["selected"] is None
    assert tick1["lease"]["status"] == "released"
    assert tick1["lease"]["recovered"] is True
    assert tick1["lease"]["recovery"]["reason"] == "pid_gone"
    assert tick1["lease"]["selected_project"]["project"] == "aa-ready-first"
    assert not workspace_dispatch_lease_dir(workspace).exists()

    def queue_item(payload: dict, project_name: str) -> dict:
        return next(item for item in payload["queue"] if item["project"] == project_name)

    def skip_codes(payload: dict, project_name: str) -> set[str]:
        return {reason["code"] for reason in queue_item(payload, project_name)["skip_reasons"]}

    assert [item["project"] for item in tick1["queue"]] == [
        "aa-ready-first",
        "bb-ready-second",
        "cc-approval-blocked",
        "dd-isolated-failure",
    ]
    assert skip_codes(tick1, "bb-ready-second") == {"one_project_per_invocation"}
    assert "waiting_on_approvals" in skip_codes(tick1, "cc-approval-blocked")
    assert "unresolved_isolated_failures" in skip_codes(tick1, "dd-isolated-failure")
    assert "no_pending_task" in skip_codes(tick2, "aa-ready-first")
    assert skip_codes(tick3, "aa-ready-first") == {"no_pending_task"}
    assert skip_codes(tick3, "bb-ready-second") == {"no_pending_task"}
    assert "waiting_on_approvals" in skip_codes(tick3, "cc-approval-blocked")
    assert "unresolved_isolated_failures" in skip_codes(tick3, "dd-isolated-failure")

    assert (first / "first-marker.txt").read_text(encoding="utf-8") == "ok"
    assert (second / "second-marker.txt").read_text(encoding="utf-8") == "ok"
    assert not (approval / "approval-marker.txt").exists()
    assert project_text_snapshot(approval) == approval_snapshot
    assert project_text_snapshot(isolated) == isolated_snapshot

    dispatches = [fresh_payload, tick1, tick2, tick3]
    assert len({payload["dispatch_report_json"] for payload in dispatches}) == len(dispatches)
    for payload in dispatches:
        report_path = workspace / payload["dispatch_report"]
        sidecar_path = workspace / payload["dispatch_report_json"]
        assert report_path.exists()
        assert sidecar_path.exists()
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["kind"] == "engineering-harness.workspace-drive-dispatch"
        assert sidecar["status"] == payload["status"]
        assert isinstance(sidecar.get("lease"), dict)
        assert isinstance(sidecar.get("queue"), list)
        assert "Machine-Readable Dispatch" in report_path.read_text(encoding="utf-8")


def test_drive_pause_resume_and_cancel_controls_are_durable(tmp_path):
    project = tmp_path / "paused-project"
    project.mkdir()
    init_project(project, "python-agent", name="paused-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('pause-marker.txt').write_text('ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["pause", "--project-root", str(project), "--reason", "test_pause"]) == 0
    assert cli_main(["drive", "--project-root", str(project)]) == 0
    assert not (project / "pause-marker.txt").exists()

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["drive_control"]["status"] == "paused"
    assert state["drive_control"]["pause_requested"] is True
    assert "tests" not in state.get("tasks", {})

    assert cli_main(["resume", "--project-root", str(project), "--reason", "test_resume"]) == 0
    assert cli_main(["drive", "--project-root", str(project)]) == 0
    assert (project / "pause-marker.txt").read_text(encoding="utf-8") == "ok"

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["drive_control"]["status"] == "idle"
    assert state["drive_control"]["last_drive_status"] == "completed"
    assert state["tasks"]["tests"]["status"] == "passed"

    cancelled = tmp_path / "cancelled-project"
    cancelled.mkdir()
    init_project(cancelled, "python-agent", name="cancelled-project")
    cancelled_roadmap_path = cancelled / ".engineering/roadmap.yaml"
    cancelled_roadmap = json.loads(cancelled_roadmap_path.read_text(encoding="utf-8"))
    cancelled_roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('cancel-marker.txt').write_text('ok')\""
    )
    cancelled_roadmap_path.write_text(json.dumps(cancelled_roadmap), encoding="utf-8")

    assert cli_main(["cancel", "--project-root", str(cancelled), "--reason", "test_cancel"]) == 0
    assert cli_main(["drive", "--project-root", str(cancelled)]) == 1
    assert not (cancelled / "cancel-marker.txt").exists()
    state = json.loads((cancelled / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["drive_control"]["status"] == "cancelled"
    assert state["drive_control"]["cancel_requested"] is True


def test_drive_watchdog_heartbeat_records_running_owner_and_task(tmp_path):
    project = tmp_path / "watchdog-running-project"
    project.mkdir()
    init_project(project, "python-agent", name="watchdog-running-project")

    harness = Harness(project)
    start = harness.start_drive()
    task = harness.next_task()
    assert start["started"] is True
    assert task is not None

    heartbeat = harness.drive_heartbeat(
        activity="acceptance-1:command",
        message="running acceptance command",
        task=task,
        phase="acceptance-1",
    )

    assert heartbeat is not None
    summary = Harness(project).status_summary()["drive_control"]
    assert summary["status"] == "running"
    assert summary["active"] is True
    assert summary["pid"] == os.getpid()
    assert summary["started_at"]
    assert summary["last_heartbeat_at"]
    assert summary["heartbeat_count"] >= 2
    assert summary["current_activity"] == "acceptance-1:command"
    assert summary["current_task"]["id"] == "tests"
    assert summary["current_task"]["phase"] == "acceptance-1"
    assert summary["last_progress_message"] == "running acceptance command"
    assert summary["watchdog"]["status"] == "running"
    assert summary["watchdog"]["stale"] is False

    Harness(project).finish_drive(status="completed", message="test complete")


def test_stale_running_recovery_allows_next_drive(tmp_path, capsys):
    project = tmp_path / "stale-running-recovery-project"
    project.mkdir()
    init_project(project, "python-agent", name="stale-running-recovery-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('recovered-marker.txt').write_text('ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    previous_pid = unused_pid()
    state = harness_state(project)
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "completed"
    assert payload["results"][0]["status"] == "passed"
    recovery = payload["stale_running_recovery"]
    assert recovery["status"] == "recovered"
    assert recovery["reason"] == "dead_pid_and_stale_heartbeat"
    assert recovery["previous_pid"] == previous_pid
    assert recovery["heartbeat_age_seconds"] > 1
    assert recovery["threshold_seconds"] == 1
    assert recovery["recovered_at"]
    assert recovery["recommended_follow_up"]
    assert (project / "recovered-marker.txt").read_text(encoding="utf-8") == "ok"

    state = harness_state(project)
    assert state["drive_control"]["status"] == "idle"
    assert state["drive_control"]["stale_running_recovery"]["previous_pid"] == previous_pid
    assert any(event["command"] == "stale-running-recovery" for event in state["drive_control"]["history"])

    report_json = json.loads((project / payload["drive_report_json"]).read_text(encoding="utf-8"))
    assert report_json["stale_running_recovery"]["previous_pid"] == previous_pid
    report_text = (project / payload["drive_report"]).read_text(encoding="utf-8")
    assert "Stale Running Recovery" in report_text
    assert "dead_pid_and_stale_heartbeat" in report_text

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    status_recovery = status_payload["drive_control"]["stale_running_recovery"]
    assert status_recovery["previous_pid"] == previous_pid
    assert status_payload["runtime_dashboard"]["drive_control"]["stale_running_recovery"]["previous_pid"] == previous_pid


def test_stale_running_dead_owner_recovery_e2e(tmp_path, capsys):
    project = tmp_path / "stale-running-dead-owner-recovery-e2e-project"
    project.mkdir()
    init_project(project, "python-agent", name="stale-running-dead-owner-recovery-e2e-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('accepted-dead-owner.txt').write_text('ok')\""
    )
    task["e2e"] = [
        {
            "name": "dead owner recovery user path",
            "command": (
                "python3 -c \"from pathlib import Path; "
                "assert Path('accepted-dead-owner.txt').read_text(encoding='utf-8') == 'ok'; "
                "Path('dead-owner-e2e.txt').write_text('ok', encoding='utf-8')\""
            ),
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    previous_pid = unused_pid()
    state = harness_state(project)
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    recovery = payload["stale_running_recovery"]
    assert payload["status"] == "completed"
    assert payload["results"][0]["status"] == "passed"
    assert [run["phase"] for run in payload["results"][0]["runs"]] == ["acceptance-1", "e2e"]
    assert recovery["status"] == "recovered"
    assert recovery["reason"] == "dead_pid_and_stale_heartbeat"
    assert recovery["previous_pid"] == previous_pid
    assert (project / "dead-owner-e2e.txt").read_text(encoding="utf-8") == "ok"

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["drive_control"]["status"] == "idle"
    assert status_payload["drive_control"]["stale_running_recovery"]["reason"] == "dead_pid_and_stale_heartbeat"
    assert (
        status_payload["runtime_dashboard"]["drive_control"]["stale_running_recovery"]["previous_pid"]
        == previous_pid
    )


def test_stale_running_missing_owner_recovery_allows_next_drive(tmp_path, capsys):
    project = tmp_path / "stale-running-missing-owner-recovery-project"
    project.mkdir()
    init_project(project, "python-agent", name="stale-running-missing-owner-recovery-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('missing-owner-marker.txt').write_text('ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    state = harness_state(project)
    state["drive_control"]["pid"] = None
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    recovery = payload["stale_running_recovery"]
    assert payload["status"] == "completed"
    assert recovery["status"] == "recovered"
    assert recovery["reason"] == "missing_pid_and_stale_heartbeat"
    assert recovery["previous_pid"] is None
    assert recovery["pid_alive"] is None
    assert (project / "missing-owner-marker.txt").read_text(encoding="utf-8") == "ok"

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["drive_control"]["stale_running_recovery"]["reason"] == "missing_pid_and_stale_heartbeat"
    assert status_payload["runtime_dashboard"]["drive_control"]["stale_running_recovery"]["previous_pid"] is None


def test_stale_running_recovery_blocks_live_pid_without_mutating_state(tmp_path, capsys):
    project = tmp_path / "stale-running-live-pid-project"
    project.mkdir()
    init_project(project, "python-agent", name="stale-running-live-pid-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('live-pid-marker.txt').write_text('no')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    state = harness_state(project)
    state["drive_control"]["pid"] = os.getpid()
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)
    state_before = harness_state(project)

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "stale"
    preflight = payload["stale_running_preflight"]
    assert preflight["status"] == "blocked"
    assert preflight["reason"] == "pid_alive"
    assert preflight["pid_alive"] is True
    assert preflight["heartbeat_age_seconds"] > 1
    assert not (project / "live-pid-marker.txt").exists()
    assert harness_state(project) == state_before

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["drive_control"]["stale_running_block"]["reason"] == "pid_alive"
    assert status_payload["runtime_dashboard"]["drive_control"]["stale_running_block"]["reason"] == "pid_alive"


def test_stale_running_recovery_protects_fresh_heartbeat_without_mutating_state(tmp_path, capsys):
    project = tmp_path / "stale-running-fresh-heartbeat-project"
    project.mkdir()
    init_project(project, "python-agent", name="stale-running-fresh-heartbeat-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 3600}
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('fresh-heartbeat-marker.txt').write_text('no')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    previous_pid = unused_pid()
    state = harness_state(project)
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = utc_now()
    write_harness_state(project, state)
    state_before = harness_state(project)

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "running"
    preflight = payload["stale_running_preflight"]
    assert preflight["status"] == "in_progress"
    assert preflight["reason"] == "heartbeat_fresh"
    assert preflight["blocking"] is False
    assert preflight["previous_pid"] == previous_pid
    assert preflight["pid_alive"] is False
    assert preflight["heartbeat_age_seconds"] <= 3600
    assert payload["drive_control"]["watchdog"]["status"] == "running"
    assert payload["drive_control"]["stale_running_block"] is None
    assert not (project / "fresh-heartbeat-marker.txt").exists()
    assert harness_state(project) == state_before


def test_stale_running_recovery_preserves_paused_and_cancelled_controls(tmp_path, capsys):
    paused = tmp_path / "stale-running-paused-control-project"
    paused.mkdir()
    init_project(paused, "python-agent", name="stale-running-paused-control-project")
    paused_roadmap_path = paused / ".engineering/roadmap.yaml"
    paused_roadmap = json.loads(paused_roadmap_path.read_text(encoding="utf-8"))
    paused_roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    paused_roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('paused-stale-marker.txt').write_text('no')\""
    )
    paused_roadmap_path.write_text(json.dumps(paused_roadmap), encoding="utf-8")

    assert cli_main(["pause", "--project-root", str(paused), "--reason", "operator_pause"]) == 0
    capsys.readouterr()
    paused_state = harness_state(paused)
    paused_state["drive_control"]["active"] = True
    paused_state["drive_control"]["pid"] = None
    paused_state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(paused, paused_state)

    assert cli_main(["drive", "--project-root", str(paused), "--json"]) == 0
    paused_payload = json.loads(capsys.readouterr().out)
    assert paused_payload["status"] == "paused"
    assert paused_payload["stale_running_preflight"]["status"] == "not_needed"
    assert harness_state(paused)["drive_control"]["status"] == "paused"
    assert not (paused / "paused-stale-marker.txt").exists()

    cancelled = tmp_path / "stale-running-cancelled-control-project"
    cancelled.mkdir()
    init_project(cancelled, "python-agent", name="stale-running-cancelled-control-project")
    cancelled_roadmap_path = cancelled / ".engineering/roadmap.yaml"
    cancelled_roadmap = json.loads(cancelled_roadmap_path.read_text(encoding="utf-8"))
    cancelled_roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    cancelled_roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('cancelled-stale-marker.txt').write_text('no')\""
    )
    cancelled_roadmap_path.write_text(json.dumps(cancelled_roadmap), encoding="utf-8")

    assert cli_main(["cancel", "--project-root", str(cancelled), "--reason", "operator_cancel"]) == 0
    capsys.readouterr()
    cancelled_state = harness_state(cancelled)
    cancelled_state["drive_control"]["active"] = True
    cancelled_state["drive_control"]["pid"] = None
    cancelled_state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(cancelled, cancelled_state)

    assert cli_main(["drive", "--project-root", str(cancelled), "--json"]) == 1
    cancelled_payload = json.loads(capsys.readouterr().out)
    assert cancelled_payload["status"] == "cancelled"
    assert cancelled_payload["stale_running_preflight"]["status"] == "not_needed"
    assert harness_state(cancelled)["drive_control"]["status"] == "cancelled"
    assert not (cancelled / "cancelled-stale-marker.txt").exists()


def test_drive_watchdog_status_output_includes_heartbeat_metadata(tmp_path, capsys):
    project = tmp_path / "watchdog-status-project"
    project.mkdir()
    init_project(project, "python-agent", name="watchdog-status-project")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    harness.drive_heartbeat(activity="drive-loop", message="status test heartbeat", clear_task=True)

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    drive_control = payload["drive_control"]
    assert drive_control["status"] == "running"
    assert drive_control["pid"] == os.getpid()
    assert drive_control["current_activity"] == "drive-loop"
    assert drive_control["last_progress_message"] == "status test heartbeat"
    assert drive_control["watchdog"]["status"] == "running"
    assert drive_control["watchdog"]["stale"] is False

    assert cli_main(["status", "--project-root", str(project)]) == 0
    text = capsys.readouterr().out
    assert "Drive control: running" in text
    assert "Drive watchdog: running" in text
    assert "Drive activity: drive-loop" in text
    assert "Drive progress: status test heartbeat" in text

    Harness(project).finish_drive(status="completed", message="status output checked")


def test_runtime_dashboard_watchdog_surfaces_current_task_phase(tmp_path, capsys):
    project = tmp_path / "runtime-dashboard-watchdog-project"
    project.mkdir()
    init_project(project, "python-agent", name="runtime-dashboard-watchdog-project")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    task = harness.next_task()
    assert task is not None
    harness.drive_heartbeat(
        activity="acceptance-1:command",
        message="runtime dashboard heartbeat",
        task=task,
        phase="acceptance-1",
    )

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    dashboard = payload["runtime_dashboard"]
    assert dashboard["kind"] == "engineering-harness.runtime-dashboard"
    assert dashboard["drive_watchdog"]["status"] == "running"
    assert dashboard["drive_watchdog"]["stale"] is False
    assert dashboard["current_task"]["source"] == "drive_control"
    assert dashboard["current_task"]["id"] == "tests"
    assert dashboard["current_task"]["phase"] == "acceptance-1"
    assert dashboard["current_phase"] == "acceptance-1"
    assert dashboard["drive_control"]["last_progress_message"] == "runtime dashboard heartbeat"

    Harness(project).finish_drive(status="completed", message="runtime dashboard checked")


def test_runtime_dashboard_approval_failure_goal_gap_payload(tmp_path, capsys):
    project = tmp_path / "runtime-dashboard-approval-project"
    project.mkdir()
    init_project(project, "python-agent", name="runtime-dashboard-approval-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["manual_approval_required"] = True
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('approval-dashboard.txt').write_text('ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 1
    drive_payload = json.loads(capsys.readouterr().out)
    assert drive_payload["status"] == "blocked"

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    dashboard = payload["runtime_dashboard"]
    assert dashboard["approval_leases"]["pending_count"] == 1
    assert dashboard["approval_leases"]["open_count"] == 1
    assert dashboard["approval_leases"]["pending_items"][0]["task_id"] == "tests"
    assert dashboard["failure_isolation"]["unresolved_count"] == 1
    latest_failure = dashboard["failure_isolation"]["latest_isolated_failures"][0]
    assert latest_failure["task_id"] == "tests"
    assert latest_failure["failure_kind"] == "policy_block"
    assert dashboard["latest_reports"]["drive_reports"]["files"][0]["path"] == drive_payload["drive_report"]
    assert dashboard["latest_reports"]["drive_reports"]["files"][0]["json_path"] == drive_payload["drive_report_json"]
    assert dashboard["goal_gap"]["source"] == "latest_drive_report"
    assert dashboard["goal_gap"]["source_report_json"] == drive_payload["drive_report_json"]
    action_ids = {item["id"] for item in dashboard["goal_gap"]["next_actions"]}
    assert "resolve-blockers" in action_ids


def test_operator_observability_console_payload_is_bounded_deterministic_and_historical(tmp_path, capsys):
    project = tmp_path / "operator-observability-console-project"
    project.mkdir()
    init_project(project, "python-agent", name="operator-observability-console-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "first-console-task",
            "title": "First console task",
            "status": "pending",
            "file_scope": ["**"],
            "acceptance": [
                {
                    "name": "first task writes evidence",
                    "command": "python3 -c \"from pathlib import Path; Path('first-console.txt').write_text('ok')\"",
                }
            ],
        },
        {
            "id": "approval-console-task",
            "title": "Approval blocked console task",
            "status": "pending",
            "manual_approval_required": True,
            "file_scope": ["**"],
            "acceptance": [
                {
                    "name": "approval task writes evidence",
                    "command": "python3 -c \"from pathlib import Path; Path('approval-console.txt').write_text('ok')\"",
                }
            ],
        },
    ]
    roadmap_path.write_text(json.dumps(roadmap, indent=2, sort_keys=True), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project), "--max-tasks", "2", "--json"]) == 1
    drive_payload = json.loads(capsys.readouterr().out)
    assert drive_payload["status"] == "blocked"

    first = Harness(project).status_summary()["operator_console"]
    second = Harness(project).status_summary()["operator_console"]

    assert first == second
    assert first["kind"] == "engineering-harness.operator-console"
    assert first["local_only"] is True
    assert first["requires_external_services"] is False
    assert first["bounds"]["within_limit"] is True
    assert first["bounds"]["estimated_json_bytes"] <= first["bounds"]["max_json_bytes"]
    limits = first["limits"]
    assert len(first["run_history"]["task_runs"]["recent"]) <= limits["recent_task_runs"]
    assert len(first["run_history"]["drive_runs"]["recent"]) <= limits["recent_drive_runs"]
    assert len(first["task_timelines"]["timelines"]) <= limits["timeline_tasks"]
    assert all(len(item["events"]) <= limits["timeline_events_per_task"] for item in first["task_timelines"]["timelines"])
    assert first["run_history"]["task_runs"]["status_counts"] == {"blocked": 1, "passed": 1}
    assert {item["status"] for item in first["run_history"]["task_runs"]["trend"]} == {"passed", "blocked"}
    assert [item["manifest_path"] for item in first["run_history"]["task_runs"]["trend"]]
    assert first["run_history"]["drive_runs"]["total_count"] == 1
    assert first["approvals"]["pending_count"] == 1
    assert first["failures"]["unresolved_count"] == 1
    assert first["checkpoint_readiness"]["kind"] == "engineering-harness.checkpoint-readiness"
    assert first["goal_gap_scorecard"]["categories"]
    assert first["replay_guard"]["kind"] == "engineering-harness.replay-guard-summary"
    action_ids = {item["id"] for item in first["recommended_actions"]}
    assert {"recover-isolated-failure", "review-approval-leases"}.issubset(action_ids)

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["operator_console"] == first


def test_operator_observability_console_e2e_generates_after_drive(tmp_path, capsys):
    project = tmp_path / "operator-observability-console-e2e-project"
    project.mkdir()
    init_project(project, "python-agent", name="operator-observability-console-e2e-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('accepted-console.txt').write_text('ok')\""
    )
    task["e2e"] = [
        {
            "name": "operator console local e2e evidence",
            "command": (
                "python3 -c \"from pathlib import Path; import json; "
                "p=Path('artifacts/browser-e2e/operator-console-e2e.json'); "
                "p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text(json.dumps({'status':'passed'}, sort_keys=True) + '\\n', encoding='utf-8'); "
                "print('operator console e2e passed')\""
            ),
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap, indent=2, sort_keys=True), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 0
    capsys.readouterr()

    assert cli_main(["operator-console", "--project-root", str(project), "--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["artifact"]["status"] == "written"
    assert payload["run_history"]["task_runs"]["status_counts"] == {"passed": 1}
    assert payload["e2e_artifacts"]["runs"][0]["status"] == "passed"
    assert payload["e2e_artifacts"]["files"][0]["path"] == "artifacts/browser-e2e/operator-console-e2e.json"
    json_path = project / payload["artifact"]["json_path"]
    markdown_path = project / payload["artifact"]["markdown_path"]
    assert json.loads(json_path.read_text(encoding="utf-8")) == payload
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# Operator Console" in markdown
    assert "Machine Payload" in markdown


@pytest.mark.parametrize(
    ("gate", "approval_kind", "decision_kind", "marker"),
    [
        ("manual", "manual", "manual_approval", "manual-marker.txt"),
        ("live", "live", "live_approval", "live-marker.txt"),
        ("agent", "agent", "agent_approval", "agent-marker.txt"),
    ],
)
def test_approval_queue_unblocks_manual_live_and_agent_gates(
    tmp_path,
    gate,
    approval_kind,
    decision_kind,
    marker,
):
    project = tmp_path / f"{gate}-approval-project"
    project.mkdir()
    init_project(project, "python-agent", name=f"{gate}-approval-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = (
        f"python3 -c \"from pathlib import Path; Path('{marker}').write_text('ok')\""
    )
    if gate == "manual":
        task["manual_approval_required"] = True
    elif gate == "agent":
        task["agent_approval_required"] = True
    elif gate == "live":
        task["acceptance"][0]["command"] += " --live"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    assert not (project / marker).exists()
    assert cli_main(["approvals", "--project-root", str(project), "--json"]) == 0

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    pending = [
        item
        for item in state["approval_queue"]["items"].values()
        if item["status"] == "pending"
    ]
    assert len(pending) == 1
    approval = pending[0]
    assert approval["approval_kind"] == approval_kind
    assert approval["decision_kind"] == decision_kind
    assert state["tasks"]["tests"]["status"] == "blocked"
    assert state["tasks"]["tests"]["attempts"] == 0

    assert cli_main(["approve", "--project-root", str(project), approval["id"], "--reason", "test approval"]) == 0
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["approval_queue"]["items"][approval["id"]]["status"] == "approved"
    assert state["tasks"]["tests"]["status"] == "pending"

    assert cli_main(["drive", "--project-root", str(project)]) == 0
    assert (project / marker).read_text(encoding="utf-8") == "ok"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["approval_queue"]["items"][approval["id"]]["status"] == "consumed"
    assert state["tasks"]["tests"]["status"] == "passed"


@pytest.mark.parametrize(
    ("gate", "approval_kind", "decision_kind"),
    [
        ("manual", "manual", "manual_approval"),
        ("agent", "agent", "agent_approval"),
        ("live", "live", "live_approval"),
    ],
)
def test_approval_lease_records_fingerprints_for_manual_agent_and_live(tmp_path, gate, approval_kind, decision_kind):
    project = tmp_path / f"{gate}-approval-lease-project"
    project.mkdir()
    init_project(project, "python-agent", name=f"{gate}-approval-lease-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = "python3 -c \"print('lease ok')\""
    if gate == "manual":
        task["manual_approval_required"] = True
    elif gate == "agent":
        task["agent_approval_required"] = True
    elif gate == "live":
        task["acceptance"][0]["command"] += " --live"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_task(Harness(project).next_task())
    assert result["status"] == "blocked"
    state = harness_state(project)
    approval = next(item for item in state["approval_queue"]["items"].values() if item["status"] == "pending")

    assert approval["approval_kind"] == approval_kind
    assert approval["decision_kind"] == decision_kind
    assert len(approval["approval_fingerprint"]) == 64
    assert approval["approval_fingerprint_version"] == 1
    assert approval["approval_fingerprint_payload"]["task"]["id"] == "tests"
    assert approval["approval_fingerprint_payload"]["approval"]["approval_kind"] == approval_kind
    assert approval["lease_ttl_seconds"] == 3600
    assert approval["lease_started_at"] is None
    assert approval["lease_expires_at"] is None

    approved = Harness(project).approve_approval(approval["id"], reason="lease test")
    lease = approved["approval"]
    assert approved["status"] == "approved"
    assert lease["lease_started_at"]
    assert lease["lease_expires_at"]
    assert lease["approval_fingerprint"] == approval["approval_fingerprint"]


def test_approval_lease_requeues_changed_command_after_approval(tmp_path, capsys):
    project = tmp_path / "changed-command-approval-lease-project"
    project.mkdir()
    init_project(project, "python-agent", name="changed-command-approval-lease-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["name"] = "live marker"
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('first.txt').write_text('first')\" --live"
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    first = next(item for item in state["approval_queue"]["items"].values() if item["status"] == "pending")
    assert cli_main(["approve", "--project-root", str(project), first["id"], "--reason", "approve first command"]) == 0

    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('second.txt').write_text('second')\" --live"
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    capsys.readouterr()
    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "blocked"
    assert not (project / "first.txt").exists()
    assert not (project / "second.txt").exists()
    state = harness_state(project)
    approvals = state["approval_queue"]["items"]
    stale = [item for item in approvals.values() if item["status"] == "stale"]
    pending = [item for item in approvals.values() if item["status"] == "pending"]
    assert len(stale) == 1
    assert len(pending) == 1
    assert stale[0]["id"] == first["id"]
    assert stale[0]["stale_reason"] == "approval fingerprint mismatch: current policy decision changed"
    assert stale[0]["approval_fingerprint"] != pending[0]["approval_fingerprint"]

    manifest = task_manifest(project, payload["results"][0])
    assert manifest["approval_queue"]["stale_count"] == 1
    assert manifest["approval_queue"]["pending_count"] == 1
    assert manifest["approval_queue"]["stale_reasons"] == {
        "approval fingerprint mismatch: current policy decision changed": 1
    }
    assert payload["approval_queue"]["stale_count"] == 1
    assert "## Approval Leases" in (project / payload["drive_report"]).read_text(encoding="utf-8")

    assert cli_main(["approvals", "--project-root", str(project), "--json"]) == 0
    approvals_payload = json.loads(capsys.readouterr().out)
    assert approvals_payload["stale_count"] == 1
    assert approvals_payload["pending_count"] == 1
    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["approval_queue"]["stale_count"] == 1


def test_approval_fingerprint_stales_executor_prompt_change_after_approval(tmp_path):
    project = tmp_path / "prompt-approval-fingerprint-project"
    project.mkdir()
    init_project(project, "python-agent", name="prompt-approval-fingerprint-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0] = {"name": "agent work", "executor": "codex", "prompt": "First prompt."}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    approval = next(item for item in state["approval_queue"]["items"].values() if item["status"] == "pending")
    assert approval["decision_kind"] == "executor_approval"
    assert cli_main(["approve", "--project-root", str(project), approval["id"], "--reason", "approve first prompt"]) == 0

    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["prompt"] = "Second prompt."
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    stale = [item for item in state["approval_queue"]["items"].values() if item["status"] == "stale"]
    pending = [item for item in state["approval_queue"]["items"].values() if item["status"] == "pending"]
    assert len(stale) == 1
    assert len(pending) == 1
    assert stale[0]["stale_reason"] == "approval fingerprint mismatch: current policy decision changed"
    assert pending[0]["decision_kind"] == "executor_approval"


def test_approval_lease_marks_expired_approval_stale_and_requeues(tmp_path):
    project = tmp_path / "expired-approval-lease-project"
    project.mkdir()
    init_project(project, "python-agent", name="expired-approval-lease-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["approval_leases"] = {"ttl_seconds": 1}
    task = roadmap["milestones"][0]["tasks"][0]
    task["manual_approval_required"] = True
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; Path('expired.txt').write_text('no')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    approval = next(item for item in state["approval_queue"]["items"].values() if item["status"] == "pending")
    assert cli_main(["approve", "--project-root", str(project), approval["id"], "--reason", "short lease"]) == 0
    state = harness_state(project)
    state["approval_queue"]["items"][approval["id"]]["lease_expires_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    stale = [item for item in state["approval_queue"]["items"].values() if item["status"] == "stale"]
    pending = [item for item in state["approval_queue"]["items"].values() if item["status"] == "pending"]
    assert len(stale) == 1
    assert len(pending) == 1
    assert stale[0]["stale_reason"] == "approval lease expired at 2000-01-01T00:00:00Z"
    assert pending[0]["lease_ttl_seconds"] == 1
    assert not (project / "expired.txt").exists()


def test_approval_lease_consumed_approval_does_not_satisfy_future_gate(tmp_path):
    project = tmp_path / "consumed-approval-lease-project"
    project.mkdir()
    init_project(project, "python-agent", name="consumed-approval-lease-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('consumed.txt').write_text('ok')\" --live"
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    approval = next(item for item in state["approval_queue"]["items"].values() if item["status"] == "pending")
    assert cli_main(["approve", "--project-root", str(project), approval["id"], "--reason", "consume once"]) == 0
    assert cli_main(["drive", "--project-root", str(project)]) == 0
    state = harness_state(project)
    assert state["approval_queue"]["items"][approval["id"]]["status"] == "consumed"
    assert (project / "consumed.txt").read_text(encoding="utf-8") == "ok"

    (project / "consumed.txt").unlink()
    state["tasks"]["tests"]["status"] = "pending"
    write_harness_state(project, state)

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    state = harness_state(project)
    approvals = state["approval_queue"]["items"]
    assert approvals[approval["id"]]["status"] == "consumed"
    assert len([item for item in approvals.values() if item["status"] == "pending"]) == 1
    assert not (project / "consumed.txt").exists()


def test_drive_can_commit_after_each_completed_task(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('done.txt').write_text('done')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    exit_code = cli_main(["drive", "--project-root", str(project), "--commit-after-task"])

    assert exit_code == 0
    last_subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=project, text=True).strip()
    assert last_subject == "chore(engineering): complete tests"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True).strip() == ""
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    checkpoint_events = [
        event for event in state["tasks"]["tests"]["phase_history"] if event["phase"] == "checkpoint-intent"
    ]
    assert [(event["event"], event["status"]) for event in checkpoint_events] == [
        ("before", "running"),
        ("after", "committed"),
    ]
    assert checkpoint_events[-1]["metadata"]["commit"]


def test_advance_materializes_next_continuation_stage(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Ship the full agent system.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create a generated validation task.",
                "source": {"path": "docs/spec-plan.md", "stage_number": 3},
                "spec_refs": ["EH-SPEC-001", "EH-SPEC-013"],
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "spec_refs": ["EH-SPEC-001", "EH-SPEC-013"],
                        "source_spec_task": {
                            "path": "docs/spec-plan.md",
                            "stage_number": 3,
                            "task_index": 1,
                            "task": "Generated Test",
                        },
                        "file_scope": ["tests/**"],
                        "acceptance": [
                            {
                                "name": "ok",
                                "command": "python3 -c \"print('ok')\"",
                                "spec_refs": ["EH-SPEC-001"],
                            }
                        ],
                        "e2e": [
                            {
                                "name": "journey ok",
                                "command": "python3 -c \"print('e2e')\"",
                                "spec_refs": ["EH-SPEC-013"],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["advance", "--project-root", str(project)])

    assert exit_code == 0
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert updated["milestones"][0]["id"] == "stage-a"
    assert updated["milestones"][0]["source"] == {"path": "docs/spec-plan.md", "stage_number": 3}
    assert updated["milestones"][0]["spec_refs"] == ["EH-SPEC-001", "EH-SPEC-013"]
    assert updated["milestones"][0]["tasks"][0]["id"] == "generated-test"
    assert updated["milestones"][0]["tasks"][0]["spec_refs"] == ["EH-SPEC-001", "EH-SPEC-013"]
    assert updated["milestones"][0]["tasks"][0]["source_spec_task"]["task"] == "Generated Test"
    assert updated["milestones"][0]["tasks"][0]["acceptance"][0]["spec_refs"] == ["EH-SPEC-001"]
    assert updated["milestones"][0]["tasks"][0]["e2e"][0]["name"] == "journey ok"
    assert updated["milestones"][0]["tasks"][0]["e2e"][0]["spec_refs"] == ["EH-SPEC-013"]


def test_validate_allows_materialized_continuation_stage_task_ids(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Materialize and continue validating.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create a generated validation task.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["tests/**"],
                        "acceptance": [{"name": "ok", "command": "python3 -c \"print('ok')\""}],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    advance = harness.advance_roadmap()
    validation = Harness(project).validate_roadmap()

    assert advance["status"] == "advanced"
    assert validation["status"] == "passed"


def test_drive_rolling_advances_and_runs_generated_tasks(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Continue until generated stages are complete.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create a generated validation task.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["tests/**"],
                        "acceptance": [
                            {
                                "name": "write marker",
                                "command": "python3 -c \"from pathlib import Path; Path('generated.txt').write_text('ok')\"",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["drive", "--project-root", str(project), "--rolling", "--max-continuations", "2"])

    assert exit_code == 0
    assert (project / "generated.txt").read_text(encoding="utf-8") == "ok"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["generated-test"]["status"] == "passed"
    report = next((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))
    assert "Continuations" in report.read_text(encoding="utf-8")


def test_drive_report_goal_gap_retrospective_budget_exhausted(tmp_path, capsys):
    project = tmp_path / "goal-gap-budget-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-budget-project")
    (project / "tests").mkdir()
    (project / "tests/test_goal_gap.py").write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "first-task",
            "title": "First Task",
            "status": "pending",
            "file_scope": ["tests/**"],
            "acceptance": [{"name": "first ok", "command": "python3 -c \"print('first ok')\""}],
        },
        {
            "id": "second-task",
            "title": "Second Task",
            "status": "pending",
            "file_scope": ["tests/**"],
            "acceptance": [{"name": "second ok", "command": "python3 -c \"print('second ok')\""}],
        },
    ]
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Plan the next reliability hardening stage.",
        "planner": {"name": "local planner", "command": "python3 -c \"print('planner placeholder')\""},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code = cli_main(["drive", "--project-root", str(project), "--max-tasks", "1", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "budget_exhausted"
    retrospective = payload["goal_gap_retrospective"]
    assert retrospective["kind"] == "engineering-harness.goal-gap-retrospective"
    assert retrospective["trigger"]["stop_class"] == "budget_exhausted"
    assert retrospective["task_counts"]["pending"] == 1
    assert retrospective["evidence"]["manifest_index"]["manifest_count"] == 1
    assert retrospective["evidence"]["latest_reports"]["task_reports"]["included_count"] == 1
    assert retrospective["evidence"]["tests"]["total_count"] == 1
    assert retrospective["evidence"]["git"]["is_repository"] is True
    assert retrospective["request_self_iteration"]["recommended"] is False
    assert retrospective["request_self_iteration"]["blocked_by"] == ["budget_exhausted", "pending_task_queue"]
    risk_ids = {item["id"] for item in retrospective["remaining_risks"]}
    assert {"budget_exhausted", "pending_roadmap_tasks"}.issubset(risk_ids)
    theme_ids = {item["id"] for item in retrospective["likely_next_stage_themes"]}
    assert "drain-queued-tasks" in theme_ids
    assert drive_report_goal_gap_retrospective(project, payload["drive_report"]) == retrospective
    sidecar = json.loads((project / payload["drive_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["goal_gap_retrospective"] == retrospective


def test_drive_report_goal_gap_retrospective_queue_empty_requests_self_iteration(tmp_path, capsys):
    project = tmp_path / "goal-gap-empty-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-empty-project")
    (project / "tests").mkdir()
    (project / "tests/test_goal_gap.py").write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('queue ok')\""
    roadmap["continuation"] = {"enabled": True, "goal": "Continue reliability hardening.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Plan the next reliability hardening stage.",
        "planner": {"name": "local planner", "command": "python3 -c \"print('planner placeholder')\""},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code = cli_main(["drive", "--project-root", str(project), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["message"] == "Roadmap queue is empty."
    retrospective = payload["goal_gap_retrospective"]
    assert retrospective["trigger"]["stop_class"] == "queue_empty"
    assert retrospective["request_self_iteration"]["recommended"] is True
    assert retrospective["request_self_iteration"]["blocked_by"] == []
    assert retrospective["evidence"]["self_iteration_context_packs"]["included_count"] == 0
    risk_ids = {item["id"] for item in retrospective["remaining_risks"]}
    assert {"roadmap_queue_empty", "self_iteration_context_not_refreshed"}.issubset(risk_ids)
    theme_ids = {item["id"] for item in retrospective["likely_next_stage_themes"]}
    assert "request-self-iteration" in theme_ids
    report_text = (project / payload["drive_report"]).read_text(encoding="utf-8")
    assert "Request self-iteration: `yes`" in report_text
    assert drive_report_goal_gap_retrospective(project, payload["drive_report"]) == retrospective
    sidecar = json.loads((project / payload["drive_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["goal_gap_retrospective"] == retrospective


def test_goal_gap_scorecard_empty_project_fallback_and_stable_ordering(tmp_path):
    project = tmp_path / "goal-gap-scorecard-empty-project"
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap = {
        "project": "goal-gap-scorecard-empty-project",
        "profile": "python-agent",
        "milestones": [],
        "self_iteration": {
            "enabled": True,
            "objective": "Score unattended reliability categories from local evidence.",
            "planner": {"name": "local planner", "command": "python3 -c \"print('planner')\""},
        },
    }
    (engineering_dir / "roadmap.yaml").write_text(json.dumps(roadmap), encoding="utf-8")

    scorecard = Harness(project).status_summary()["goal_gap_scorecard"]

    expected_order = [
        "stuck_detection",
        "stale_running_recovery",
        "checkpoint_boundaries",
        "failure_isolation",
        "duplicate_plan_guard",
        "goal_gap_retrospective",
        "runtime_dashboard",
        "approval_capability_policy_safety",
        "workspace_dispatch_fairness_backoff",
        "real_e2e_evidence",
    ]
    assert scorecard["category_order"] == expected_order
    assert [category["id"] for category in scorecard["categories"]] == expected_order
    assert scorecard["summary"]["category_count"] == len(expected_order)
    by_id = scorecard_category_map(scorecard)
    assert by_id["goal_gap_retrospective"]["status"] == "missing"
    assert by_id["real_e2e_evidence"]["status"] == "missing"
    assert by_id["checkpoint_boundaries"]["status"] == "missing"
    assert all(isinstance(category["risk_score"], int) for category in scorecard["categories"])
    assert all(isinstance(category["severity"], int) for category in scorecard["categories"])


def test_goal_gap_scorecard_completed_capability_detection_from_recent_manifests_status(tmp_path):
    project = tmp_path / "goal-gap-scorecard-complete-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-complete-project")
    (project / "src").mkdir()
    (project / "src/app.py").write_text("def marker():\n    return 'ok'\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests/test_app.py").write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; Path('accepted.txt').write_text('ok')\""
    task["e2e"] = [
        {
            "name": "local journey",
            "command": (
                "python3 -c \"from pathlib import Path; "
                "assert Path('accepted.txt').read_text() == 'ok'; "
                "Path('e2e.txt').write_text('ok')\""
            ),
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())
    status = Harness(project).status_summary()
    by_id = scorecard_category_map(status["goal_gap_scorecard"])

    assert result["status"] == "passed"
    assert status["manifest_index"]["manifest_count"] == 1
    assert by_id["real_e2e_evidence"]["status"] == "complete"
    assert by_id["runtime_dashboard"]["status"] == "complete"
    assert by_id["failure_isolation"]["status"] == "complete"
    assert by_id["real_e2e_evidence"]["risk_score"] < 20


def test_goal_gap_scorecard_unresolved_failure_or_approval_blockers_raise_severity(tmp_path, capsys):
    project = tmp_path / "goal-gap-scorecard-blocker-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-blocker-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["manual_approval_required"] = True
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('approval-scorecard.txt').write_text('ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project), "--json"]) == 1
    capsys.readouterr()
    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    scorecard = payload["goal_gap_scorecard"]
    by_id = scorecard_category_map(scorecard)

    assert scorecard["summary"]["overall_status"] == "blocked"
    assert scorecard["summary"]["status_counts"]["blocked"] >= 2
    assert by_id["approval_capability_policy_safety"]["status"] == "blocked"
    assert by_id["approval_capability_policy_safety"]["severity"] == 4
    assert by_id["failure_isolation"]["status"] == "blocked"
    assert by_id["failure_isolation"]["risk_score"] >= 80


def test_goal_gap_scorecard_status_and_dashboard_exposure(tmp_path, capsys):
    project = tmp_path / "goal-gap-scorecard-status-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-status-project")

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["goal_gap_scorecard"]["kind"] == "engineering-harness.goal-gap-scorecard"
    assert payload["runtime_dashboard"]["goal_gap_scorecard"] == payload["goal_gap_scorecard"]
    assert payload["runtime_dashboard"]["goal_gap_scorecard"]["categories"]


def test_goal_gap_scorecard_suppresses_active_drive_false_blockers(tmp_path, capsys):
    project = tmp_path / "goal-gap-scorecard-active-drive-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-active-drive-project")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    previous_pid = unused_pid()
    state = harness_state(project)
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = utc_now()
    state["drive_control"]["current_activity"] = "acceptance-1:command"
    write_harness_state(project, state)

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    drive_control = payload["drive_control"]
    dashboard_drive = payload["runtime_dashboard"]["drive_control"]
    by_id = scorecard_category_map(payload["goal_gap_scorecard"])
    dashboard_by_id = scorecard_category_map(payload["runtime_dashboard"]["goal_gap_scorecard"])

    assert drive_control["status"] == "running"
    assert drive_control["active"] is True
    assert drive_control["watchdog"]["status"] == "running"
    assert drive_control["watchdog"]["stale"] is False
    assert drive_control["watchdog"]["pid_alive"] is False
    assert drive_control["stale_running_preflight"]["status"] == "in_progress"
    assert drive_control["stale_running_preflight"]["reason"] == "heartbeat_fresh"
    assert drive_control["stale_running_preflight"]["blocking"] is False
    assert drive_control["stale_running_block"] is None
    assert dashboard_drive["stale_running_preflight"]["status"] == "in_progress"
    assert dashboard_drive["stale_running_block"] is None
    assert by_id["stale_running_recovery"]["status"] == "complete"
    assert "in_progress" in by_id["stale_running_recovery"]["rationale"]
    assert "recover-stale-running-drive" not in by_id["stale_running_recovery"]["recommended_next_stage_themes"]
    assert dashboard_by_id["stale_running_recovery"] == by_id["stale_running_recovery"]


def test_goal_gap_scorecard_blocks_stale_running_dead_pid(tmp_path):
    project = tmp_path / "goal-gap-scorecard-stale-running-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-stale-running-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 1}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    previous_pid = unused_pid()
    state = harness_state(project)
    state["drive_control"]["pid"] = previous_pid
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    payload = Harness(project).status_summary()
    preflight = payload["drive_control"]["stale_running_preflight"]
    by_id = scorecard_category_map(payload["goal_gap_scorecard"])

    assert payload["drive_control"]["status"] == "stale"
    assert preflight["status"] == "recoverable"
    assert preflight["reason"] == "dead_pid_and_stale_heartbeat"
    assert by_id["stale_running_recovery"]["status"] == "blocked"
    assert by_id["stale_running_recovery"]["severity"] == 4
    assert by_id["stale_running_recovery"]["recommended_next_stage_themes"] == ["recover-stale-running-drive"]


def test_goal_gap_scorecard_checkpoint_pending_roadmap_only_dirtiness(tmp_path):
    project = tmp_path / "goal-gap-scorecard-checkpoint-pending-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-checkpoint-pending-project")
    init_git_repo(project)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["checkpoint_readiness_note"] = "roadmap materialization"
    roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")

    payload = Harness(project).status_summary()
    readiness = payload["checkpoint_readiness"]
    by_id = scorecard_category_map(payload["goal_gap_scorecard"])
    theme_ids = {item["id"] for item in payload["goal_gap_scorecard"]["recommended_next_stage_themes"]}

    assert readiness["ready"] is True
    assert readiness["blocking"] is False
    assert readiness["safe_to_checkpoint_paths"] == [".engineering/roadmap.yaml"]
    assert readiness["blocking_paths"] == []
    assert by_id["checkpoint_boundaries"]["status"] == "partial"
    assert "checkpoint_pending" in by_id["checkpoint_boundaries"]["rationale"]
    assert ".engineering/roadmap.yaml" in by_id["checkpoint_boundaries"]["rationale"]
    assert "close-git-boundary" not in by_id["checkpoint_boundaries"]["recommended_next_stage_themes"]
    assert "close-git-boundary" not in theme_ids


def test_goal_gap_scorecard_checkpoint_pending_mixed_dirty_blockers(tmp_path):
    project = tmp_path / "goal-gap-scorecard-checkpoint-mixed-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-scorecard-checkpoint-mixed-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "operator.txt").write_text("base", encoding="utf-8")
    init_git_repo(project)

    roadmap["checkpoint_readiness_note"] = "safe roadmap materialization"
    roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
    (project / "src").mkdir()
    (project / "src/in-scope.txt").write_text("task dirty", encoding="utf-8")
    (project / "operator.txt").write_text("operator dirty", encoding="utf-8")

    payload = Harness(project).status_summary()
    readiness = payload["checkpoint_readiness"]
    by_id = scorecard_category_map(payload["goal_gap_scorecard"])

    assert readiness["blocking"] is True
    assert readiness["safe_to_checkpoint_paths"] == [".engineering/roadmap.yaml", "src/in-scope.txt"]
    assert readiness["blocking_paths"] == ["operator.txt"]
    assert by_id["checkpoint_boundaries"]["status"] == "blocked"
    assert by_id["checkpoint_boundaries"]["recommended_next_stage_themes"] == ["close-git-boundary"]


def test_drive_rolling_commit_after_task_checkpoints_materialization_before_generated_task(tmp_path, capsys):
    project = tmp_path / "rolling-checkpoint-project"
    project.mkdir()
    init_project(project, "python-agent", name="rolling-checkpoint-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Continue with generated tasks.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create an in-scope generated marker.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["generated.txt"],
                        "acceptance": [
                            {
                                "name": "write marker",
                                "command": "python3 -c \"from pathlib import Path; Path('generated.txt').write_text('ok')\"",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--max-continuations",
            "1",
            "--max-tasks",
            "1",
            "--commit-after-task",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    materialization = payload["continuations"][0]["materialization_checkpoint"]
    task_git = payload["results"][0]["git"]
    assert materialization["status"] == "committed"
    assert materialization["dirty_before_paths"] == []
    assert materialization["materialization_paths"] == [".engineering/roadmap.yaml"]
    assert task_git["status"] == "committed"
    subjects = subprocess.check_output(
        ["git", "log", "--format=%s", "-3"],
        cwd=project,
        text=True,
    ).splitlines()
    assert subjects == [
        "chore(engineering): complete generated-test",
        "chore(engineering): materialize roadmap continuation: stage-a",
        "initial",
    ]
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True).strip() == ""
    report = project / payload["drive_report"]
    report_text = report.read_text(encoding="utf-8")
    assert "Materialization checkpoint: `committed`" in report_text
    assert "No task checkpoint deferral was recorded." in report_text


def test_drive_rolling_commit_after_task_defers_when_materialization_has_user_dirtiness(tmp_path, capsys):
    project = tmp_path / "rolling-checkpoint-dirty-project"
    project.mkdir()
    init_project(project, "python-agent", name="rolling-checkpoint-dirty-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Continue with generated tasks.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create an in-scope generated marker.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["generated.txt"],
                        "acceptance": [
                            {
                                "name": "write marker",
                                "command": "python3 -c \"from pathlib import Path; Path('generated.txt').write_text('ok')\"",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)
    (project / "user.txt").write_text("user change", encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--max-continuations",
            "1",
            "--max-tasks",
            "1",
            "--commit-after-task",
            "--json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["results"] == []
    materialization = payload["continuations"][0]["materialization_checkpoint"]
    assert materialization["status"] == "deferred"
    assert materialization["reason"] == "checkpoint_readiness_blocked"
    assert materialization["dirty_before_paths"] == ["user.txt"]
    assert materialization["dirty_before_blocking_paths"] == ["user.txt"]
    subjects = subprocess.check_output(["git", "log", "--format=%s"], cwd=project, text=True).splitlines()
    assert subjects == ["initial"]
    dirty_paths = subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True)
    assert "user.txt" in dirty_paths
    assert ".engineering/roadmap.yaml" not in dirty_paths
    assert "generated.txt" not in dirty_paths
    report = project / payload["drive_report"]
    report_text = report.read_text(encoding="utf-8")
    assert "Materialization checkpoint: `deferred`" in report_text
    assert "Dirty before materialization: `user.txt`" in report_text
    assert "Task `generated-test` checkpoint deferred" not in report_text


def test_automatic_checkpoint_boundary_e2e_commits_self_iteration_materialization_and_task(tmp_path, capsys):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    stage = valid_self_iteration_stage("auto-boundary-stage", "auto-boundary-task")
    task = stage["tasks"][0]
    task.pop("implementation")
    task.pop("repair")
    task["file_scope"] = ["tests/**"]
    task["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('tests').mkdir(exist_ok=True); "
        "Path('tests/auto-boundary.txt').write_text('ok')\""
    )
    write_self_iteration_guard_planner(project, [stage])
    init_git_repo(project)

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--self-iterate",
            "--max-continuations",
            "1",
            "--max-self-iterations",
            "1",
            "--max-tasks",
            "1",
            "--commit-after-task",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    advanced = next(item for item in payload["continuations"] if item["status"] == "advanced")
    materialization = advanced["materialization_checkpoint"]
    task_git = payload["results"][0]["git"]
    assert materialization["status"] == "committed"
    assert materialization["dirty_before_paths"] == [".engineering/roadmap.yaml"]
    assert materialization["dirty_before_harness_paths"] == [".engineering/roadmap.yaml"]
    assert materialization["dirty_before_blocking_paths"] == []
    assert materialization["checkpointed_paths"] == [".engineering/roadmap.yaml"]
    assert task_git["status"] == "committed"
    assert task_git["checkpointed_paths"] == ["tests/auto-boundary.txt"]
    subjects = subprocess.check_output(["git", "log", "--format=%s", "-3"], cwd=project, text=True).splitlines()
    assert subjects == [
        "chore(engineering): complete auto-boundary-task",
        "chore(engineering): materialize roadmap continuation: auto-boundary-stage",
        "initial",
    ]
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True).strip() == ""


def test_automatic_checkpoint_boundary_blocks_unrelated_dirty_without_mutation(tmp_path, capsys):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    write_self_iteration_guard_planner(
        project,
        [valid_self_iteration_stage("blocked-boundary-stage", "blocked-boundary-task")],
        mutation="Path('planner-ran.txt').write_text('ran', encoding='utf-8')",
    )
    init_git_repo(project)
    before_text = roadmap_path.read_text(encoding="utf-8")
    (project / "operator-notes.txt").write_text("operator draft", encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--self-iterate",
            "--max-continuations",
            "1",
            "--max-self-iterations",
            "1",
            "--max-tasks",
            "1",
            "--commit-after-task",
            "--json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stalled"
    assert payload["results"] == []
    assert payload["self_iterations"][0]["status"] == "blocked"
    assert payload["self_iterations"][0]["checkpoint_gate"]["phase"] == "preflight"
    assert roadmap_path.read_text(encoding="utf-8") == before_text
    assert not (project / "planner-ran.txt").exists()
    materialization = payload["continuations"][0]["materialization_checkpoint"]
    assert materialization["status"] == "skipped"
    assert payload["self_iterations"][0]["blocking_paths"] == ["operator-notes.txt"]
    assert subprocess.check_output(["git", "log", "--format=%s"], cwd=project, text=True).splitlines() == ["initial"]
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True).strip() == (
        "?? operator-notes.txt"
    )


def test_drive_rolling_stops_when_continuation_is_exhausted(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "No stages remain.", "stages": []}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["drive", "--project-root", str(project), "--rolling"])

    assert exit_code == 0
    report = next((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))
    text = report.read_text(encoding="utf-8")
    assert "no unmaterialized continuation stage remains" in text


def test_rolling_drive_stops_on_unresolved_isolated_failure_before_self_iteration(tmp_path, capsys):
    project = tmp_path / "isolated-failure-rolling-project"
    project.mkdir()
    init_project(project, "python-agent", name="isolated-failure-rolling-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_attempts"] = 1
    task["acceptance"][0]["command"] = "python3 -c \"raise SystemExit(5)\""
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add another continuation stage.",
        "planner": {"name": "planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "planner.py").write_text(
        """
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
roadmap.setdefault("continuation", {"enabled": True, "stages": []}).setdefault("stages", []).append(
    {"id": "should-not-be-added", "title": "Should Not Be Added", "tasks": []}
)
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    failed = Harness(project).run_task(Harness(project).next_task())
    assert failed["status"] == "failed"

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--self-iterate",
            "--max-self-iterations",
            "1",
            "--json",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "isolated_failure"
    assert payload["results"] == []
    assert payload["continuations"] == []
    assert payload["self_iterations"] == []
    assert payload["failure_isolation"]["unresolved_count"] == 1
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert updated["continuation"]["stages"] == []
    report_text = (project / payload["drive_report"]).read_text(encoding="utf-8")
    assert "## Failure Isolation" in report_text
    assert "should-not-be-added" not in report_text


def self_iteration_guard_project(tmp_path: Path, *, max_stages_per_iteration: int = 1) -> tuple[Path, Path]:
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next generated test stage.",
        "max_stages_per_iteration": max_stages_per_iteration,
        "planner": {"name": "test planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    return project, roadmap_path


def valid_self_iteration_stage(stage_id: str = "guard-stage", task_id: str = "guard-task") -> dict:
    return {
        "id": stage_id,
        "title": "Guard Stage",
        "objective": "Add a locally verifiable generated task.",
        "tasks": [
            {
                "id": task_id,
                "title": "Guard Task",
                "file_scope": ["src/**", "tests/**", "docs/**"],
                "implementation": [
                    {
                        "name": "implement guard task",
                        "executor": "codex",
                        "prompt": "Implement the focused local guard task.",
                    }
                ],
                "repair": [
                    {
                        "name": "repair guard task",
                        "executor": "codex",
                        "prompt": "Repair the focused local guard task if validation fails.",
                    }
                ],
                "acceptance": [
                    {
                        "name": "local guard smoke",
                        "command": "python3 -c \"print('guard ok')\"",
                        "timeout_seconds": 30,
                    }
                ],
            }
        ],
    }


def append_existing_continuation_stage(roadmap_path: Path, stage: dict) -> dict:
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap.setdefault("continuation", {"enabled": True, "goal": "Continue autonomously.", "stages": []})
    roadmap["continuation"].setdefault("stages", []).append(stage)
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    return roadmap


def write_self_iteration_guard_planner(project: Path, stages: list[dict], *, mutation: str = "") -> None:
    stages_json = json.dumps(stages, indent=2)
    (project / "planner.py").write_text(
        f"""
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
{mutation}
continuation = roadmap.setdefault("continuation", {{"enabled": True, "goal": "Continue autonomously.", "stages": []}})
continuation["enabled"] = True
continuation.setdefault("stages", []).extend({stages_json})
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )


def self_iteration_context_pack_project(tmp_path: Path) -> tuple[Path, Path]:
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs/blueprint.md").write_text(
        "# Blueprint\n\nBuild from local reports. OPENAI_API_KEY=sk-context-secret\n",
        encoding="utf-8",
    )
    (project / "docs/operator.md").write_text("# Operator Notes\n\nUse local task reports.\n", encoding="utf-8")
    (project / "src").mkdir(exist_ok=True)
    (project / "src/app.py").write_text("def marker():\n    return 'context-pack'\n", encoding="utf-8")
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests/test_app.py").write_text(
        "def test_marker():\n    assert 'context' in 'context-pack'\n",
        encoding="utf-8",
    )

    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["goal"] = {
        "text": "Use current local reports to plan the next stage.",
        "blueprint": "docs/blueprint.md",
        "constraints": ["local-only"],
    }
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('context manifest ok')\""
    roadmap["self_iteration"]["planner"] = {
        "name": "context pack planner",
        "executor": "context-planner",
        "prompt": "Read the context pack and append the next local stage.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial context"], cwd=project, check=True, capture_output=True, text=True)

    assert cli_main(["drive", "--project-root", str(project), "--max-tasks", "1", "--json"]) == 0
    status = subprocess.run(["git", "status", "--short"], cwd=project, check=True, capture_output=True, text=True)
    if status.stdout.strip():
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-m", "record initial drive evidence"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        )
    return project, roadmap_path


def context_pack_stage(stage_id: str = "context-pack-stage", task_id: str = "context-pack-task") -> dict:
    return {
        "id": stage_id,
        "title": "Context Pack Stage",
        "objective": "Use the bounded planner context to add a local follow-up task.",
        "tasks": [
            {
                "id": task_id,
                "title": "Context Pack Task",
                "file_scope": ["src/**", "tests/**", "docs/**"],
                "acceptance": [
                    {
                        "name": "context pack task smoke",
                        "command": "python3 -c \"print('context pack task ok')\"",
                        "timeout_seconds": 30,
                    }
                ],
            }
        ],
    }


def context_pack_planner_registry(captured: dict, stage_id: str = "context-pack-stage") -> ExecutorRegistry:
    class ContextPackPlanner:
        metadata = ExecutorMetadata(
            id="context-planner",
            name="Context Pack Planner",
            kind="process",
            adapter="test.context-pack-planner",
            input_mode="prompt",
            capabilities=("local_process", "stdout", "stderr"),
        )

        def display_command(self, invocation):
            return "context-pack-planner <prompt>"

        def execute(self, invocation):
            prompt = invocation.prompt or ""
            captured["prompt"] = prompt
            context_line = next(line for line in prompt.splitlines() if line.startswith("Planner context pack:"))
            context_path = Path(context_line.split(":", 1)[1].strip())
            context = json.loads(context_path.read_text(encoding="utf-8"))
            captured["context_path"] = context_path
            captured["context"] = context

            roadmap_path = invocation.project_root / ".engineering/roadmap.yaml"
            roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
            roadmap.setdefault("continuation", {"enabled": True, "stages": []}).setdefault("stages", []).append(
                context_pack_stage(stage_id=stage_id, task_id=f"{stage_id}-task")
            )
            roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="context planner ok",
                stderr="",
            )

    return ExecutorRegistry((ShellExecutorAdapter(), ContextPackPlanner()))


def test_self_iteration_context_pack_smoke(tmp_path):
    project, roadmap_path = self_iteration_context_pack_project(tmp_path)
    captured: dict = {}

    result = Harness(project, executor_registry=context_pack_planner_registry(captured)).run_self_iteration(
        reason="context-pack-smoke"
    )

    assert result["status"] == "planned"
    assert "Planner context pack:" in captured["prompt"]
    assert captured["context_path"] == project / result["context_pack"]["path"]
    context = captured["context"]
    assert context["kind"] == "engineering-harness.self-iteration-context-pack"
    assert context["manifests"]["recent_task_manifests"]
    assert context["reports"]["task_reports"]["files"]
    assert context["reports"]["drive_reports"]["files"]
    assert any(item["path"] == "tests/test_app.py" for item in context["test_inventory"]["files"])
    assert any(item["path"] == "src/app.py" for item in context["source_inventory"]["files"])
    assert context["docs"]["blueprint"]["path"] == "docs/blueprint.md"
    assert "OPENAI_API_KEY=[REDACTED]" in context["docs"]["blueprint"]["excerpt"]
    assert "sk-context-secret" not in json.dumps(context)
    assert context["git"]["is_repository"] is True
    assert context["git"]["recent_commits"]
    assert context["git"]["status"]["returncode"] == 0
    assert json.loads(roadmap_path.read_text(encoding="utf-8"))["continuation"]["stages"][0]["id"] == (
        "context-pack-stage"
    )


def test_self_iteration_goal_gap_scorecard_in_context_pack(tmp_path):
    project, _roadmap_path = self_iteration_context_pack_project(tmp_path)
    captured: dict = {}

    result = Harness(project, executor_registry=context_pack_planner_registry(captured, "goal-gap-scorecard-stage")).run_self_iteration(
        reason="goal-gap-scorecard-context"
    )

    context = captured["context"]
    scorecard = context["goal_gap_scorecard"]
    by_id = scorecard_category_map(scorecard)
    assert result["context_pack"]["goal_gap_scorecard"] == scorecard
    assert context["summary"]["goal_gap_scorecard_max_risk_score"] == scorecard["summary"]["max_risk_score"]
    assert by_id["goal_gap_retrospective"]["status"] in {"complete", "partial"}
    assert [category["id"] for category in scorecard["categories"]] == scorecard["category_order"]

    report = (project / result["report"]).read_text(encoding="utf-8")
    assessment = json.loads((project / result["report_json"]).read_text(encoding="utf-8"))
    assert "## Goal-Gap Scorecard" in report
    assert assessment["goal_gap_scorecard"] == scorecard

    status_payload = Harness(project).status_summary()
    latest = status_payload["self_iteration"]["latest_assessment"]
    assert latest["goal_gap_scorecard"]["summary"]["category_count"] == scorecard["summary"]["category_count"]


def test_goal_gap_scorecard_planner_context_checkpoint_pending_recommendations(tmp_path):
    project, roadmap_path = self_iteration_context_pack_project(tmp_path)
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["checkpoint_readiness_note"] = "planner context checkpoint window"
    roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
    captured: dict = {}

    result = Harness(
        project,
        executor_registry=context_pack_planner_registry(captured, "checkpoint-pending-context-stage"),
    ).run_self_iteration(reason="checkpoint-pending-context")

    scorecard = captured["context"]["goal_gap_scorecard"]
    by_id = scorecard_category_map(scorecard)
    theme_ids = {item["id"] for item in scorecard["recommended_next_stage_themes"]}

    assert result["status"] == "planned"
    assert by_id["checkpoint_boundaries"]["status"] == "partial"
    assert "checkpoint_pending" in by_id["checkpoint_boundaries"]["rationale"]
    assert "close-git-boundary" not in by_id["checkpoint_boundaries"]["recommended_next_stage_themes"]
    assert "close-git-boundary" not in theme_ids
    assert captured["context"]["summary"]["goal_gap_scorecard_blocked_count"] == (
        scorecard["summary"]["status_counts"]["blocked"]
    )


def test_self_iteration_context_pack_snapshot_report_and_result(tmp_path):
    project, _roadmap_path = self_iteration_context_pack_project(tmp_path)
    captured: dict = {}

    result = Harness(project, executor_registry=context_pack_planner_registry(captured, "context-pack-contract")).run_self_iteration(
        reason="context-pack-contract"
    )

    context_path = project / result["context_pack"]["path"]
    snapshot_path = project / result["snapshot"]
    report_path = project / result["report"]
    context = json.loads(context_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")

    assert snapshot["context_pack"] == result["context_pack"]
    assert snapshot["context_pack"]["summary"] == context["summary"]
    assert result["context_pack"]["summary"]["recent_manifest_count"] >= 1
    assert "## Planner Context Pack" in report
    assert result["context_pack"]["path"] in report
    assert '"manifest_count"' in report
    assert context["summary"]["source_file_count"] >= 1


def test_duplicate_plan_context_pack_includes_bounded_duplicate_summary(tmp_path):
    project, roadmap_path = self_iteration_context_pack_project(tmp_path)
    existing_stage = valid_self_iteration_stage("existing-context-stage", "existing-context-task")
    existing_stage["tasks"][0].pop("implementation")
    existing_stage["tasks"][0].pop("repair")
    append_existing_continuation_stage(roadmap_path, existing_stage)
    captured: dict = {}

    result = Harness(project, executor_registry=context_pack_planner_registry(captured, "context-pack-distinct")).run_self_iteration(
        reason="duplicate-summary"
    )

    assert result["status"] == "planned"
    context = captured["context"]
    duplicate_plan = context["duplicate_plan"]
    assert duplicate_plan["algorithm"] == "sha256:self-iteration-stage-plan:v1"
    assert duplicate_plan["stage_count"] == 1
    assert duplicate_plan["included_count"] == 1
    assert duplicate_plan["stages"][0]["stage_id"] == "existing-context-stage"
    assert len(duplicate_plan["stages"][0]["fingerprint"]) == 64
    assert len(duplicate_plan["stages"][0]["identity_fingerprint"]) == 64
    assert duplicate_plan["stages"][0]["task_ids"] == ["existing-context-task"]
    assert context["summary"]["duplicate_plan_fingerprint_count"] == 1
    assert context["summary"]["duplicate_plan_duplicate_group_count"] == 0
    assert result["context_pack"]["summary"]["duplicate_plan_fingerprint_count"] == 1


def test_self_iteration_output_guard_accepts_valid_appended_stage(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    write_self_iteration_guard_planner(project, [valid_self_iteration_stage()])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "planned"
    assert result["validation"]["status"] == "passed"
    assert result["validation"]["new_stage_ids"] == ["guard-stage"]
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == ["guard-stage"]
    report = Path(project, result["report"]).read_text(encoding="utf-8")
    assert "## Output Validation" in report
    assert "- Status: `passed`" in report


def test_self_iteration_reports_new_stage_requirement_refs(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs/spec.md").write_text(
        "# Project Spec\n\n### EH-SPEC-001: Local planning\n\n### EH-SPEC-013: Self iteration\n",
        encoding="utf-8",
    )
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["spec"] = {"schema_version": 1, "path": "docs/spec.md"}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    refs = ["EH-SPEC-001", "EH-SPEC-013"]
    stage = valid_self_iteration_stage("spec-guard-stage", "spec-guard-task")
    stage["spec_refs"] = refs
    stage["tasks"][0]["spec_refs"] = refs
    stage["tasks"][0]["acceptance"][0]["spec_refs"] = ["EH-SPEC-001"]
    stage["tasks"][0]["e2e"] = [
        {
            "name": "spec guard e2e",
            "command": "python3 -c \"print('spec guard e2e ok')\"",
            "timeout_seconds": 30,
            "spec_refs": ["EH-SPEC-013"],
        }
    ]
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="spec-ref-summary")

    assert result["status"] == "planned"
    assert result["validation"]["spec_traceability"]["required"] is True
    requirement_refs = result["validation"]["new_stage_requirement_refs"]
    assert requirement_refs[0]["stage_id"] == "spec-guard-stage"
    assert requirement_refs[0]["spec_refs"] == refs
    assert requirement_refs[0]["tasks"][0]["spec_refs"] == refs
    assert requirement_refs[0]["tasks"][0]["command_spec_refs"] == refs
    report = Path(project, result["report"]).read_text(encoding="utf-8")
    assert "## Requirement Advancement" in report
    assert "- `spec-guard-stage` advances: `EH-SPEC-001`, `EH-SPEC-013`" in report

    latest = Harness(project).self_iteration_summary()["latest_assessment"]
    assert latest["validation"]["new_stage_requirement_refs"][0]["spec_refs"] == refs


def test_self_iteration_rejects_missing_spec_refs_when_traceability_is_configured(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs/spec.md").write_text(
        "# Project Spec\n\n### EH-SPEC-001: Local planning\n\n### EH-SPEC-013: Self iteration\n",
        encoding="utf-8",
    )
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["spec"] = {"schema_version": 1, "path": "docs/spec.md"}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    write_self_iteration_guard_planner(project, [valid_self_iteration_stage("missing-spec-stage", "missing-spec-task")])

    result = Harness(project).run_self_iteration(reason="missing-spec-refs")

    assert result["status"] == "rejected"
    assert result["validation"]["status"] == "failed"
    assert result["validation"]["spec_traceability"]["required"] is True
    errors = "\n".join(result["validation"]["errors"])
    assert "new continuation stage `missing-spec-stage` must define non-empty spec_refs" in errors
    assert "new continuation task `missing-spec-task` must define non-empty spec_refs" in errors
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before

    context = json.loads((project / result["context_pack"]["path"]).read_text(encoding="utf-8"))
    assert context["summary"]["spec_traceability_required"] is True
    assert context["spec"]["known_requirement_count"] == 2
    report = Path(project, result["report"]).read_text(encoding="utf-8")
    assert "## Requirement Advancement" in report
    assert "- `missing-spec-stage` advances: `none declared`" in report


def test_duplicate_plan_guard_rejects_exact_duplicate_stage_under_new_ids(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    existing_stage = valid_self_iteration_stage("existing-guard-stage", "existing-guard-task")
    existing_stage["tasks"][0]["e2e"] = [
        {
            "name": "local guard e2e",
            "command": "python3 -c \"print('guard e2e ok')\"",
            "timeout_seconds": 30,
        }
    ]
    before = append_existing_continuation_stage(roadmap_path, existing_stage)
    duplicate_stage = deepcopy(existing_stage)
    duplicate_stage["id"] = "new-guard-stage"
    duplicate_stage["tasks"][0]["id"] = "new-guard-task"
    write_self_iteration_guard_planner(project, [duplicate_stage])

    result = Harness(project).run_self_iteration(reason="duplicate-plan-test")

    assert result["status"] == "rejected"
    errors = "\n".join(result["validation"]["errors"])
    assert "duplicates existing continuation stage plan `existing-guard-stage`" in errors
    assert "fingerprint" in errors
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before
    report = Path(project, result["report"]).read_text(encoding="utf-8")
    assert "duplicates existing continuation stage plan `existing-guard-stage`" in report


def test_duplicate_plan_guard_rejects_duplicate_task_ids_across_roadmap_tasks(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    existing_task_id = before["milestones"][0]["tasks"][0]["id"]
    stage = valid_self_iteration_stage("unique-guard-stage", existing_task_id)
    stage["title"] = "Unique Guard Stage"
    stage["objective"] = "Add a distinct local guard task with a reused task id."
    stage["tasks"][0]["title"] = "Unique Guard Task"
    stage["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('unique guard ok')\""
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="duplicate-task-id-test")

    assert result["status"] == "rejected"
    assert any(
        f"new continuation task id duplicates an existing roadmap task id: {existing_task_id}" in error
        for error in result["validation"]["errors"]
    )
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


def test_duplicate_plan_guard_allows_distinct_stage_with_existing_continuation(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before_stage = valid_self_iteration_stage("existing-guard-stage", "existing-guard-task")
    append_existing_continuation_stage(roadmap_path, before_stage)
    distinct_stage = valid_self_iteration_stage("distinct-guard-stage", "distinct-guard-task")
    distinct_stage["title"] = "Distinct Guard Stage"
    distinct_stage["objective"] = "Add a different locally verifiable generated task."
    distinct_stage["tasks"][0]["title"] = "Distinct Guard Task"
    distinct_stage["tasks"][0]["file_scope"] = ["src/distinct/**", "tests/distinct/**", "docs/distinct/**"]
    distinct_stage["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('distinct guard ok')\""
    write_self_iteration_guard_planner(project, [distinct_stage])

    result = Harness(project).run_self_iteration(reason="distinct-plan-test")

    assert result["status"] == "planned"
    assert result["validation"]["status"] == "passed"
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == [
        "existing-guard-stage",
        "distinct-guard-stage",
    ]


def test_self_iteration_output_guard_rejects_too_many_new_stages_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path, max_stages_per_iteration=1)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    write_self_iteration_guard_planner(
        project,
        [
            valid_self_iteration_stage("guard-stage-a", "guard-task-a"),
            valid_self_iteration_stage("guard-stage-b", "guard-task-b"),
        ],
    )

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    assert result["validation"]["status"] == "failed"
    assert any("expected exactly 1 new continuation stage(s), found 2" in error for error in result["validation"]["errors"])
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before
    assert "found 2" in Path(project, result["report"]).read_text(encoding="utf-8")


def test_self_iteration_output_guard_rejects_malformed_task_gates_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    stage = valid_self_iteration_stage()
    task = stage["tasks"][0]
    task.pop("file_scope")
    task["acceptance"] = [{"name": "missing command"}]
    task["repair"] = []
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    errors = "\n".join(result["validation"]["errors"])
    assert "file_scope" in errors
    assert "acceptance command" in errors
    assert "codex repair" in errors
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


def test_self_iteration_output_guard_rejects_unsafe_requirements_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    stage = valid_self_iteration_stage()
    stage["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('live')\" --live"
    stage["tasks"][0]["implementation"][0]["prompt"] = "Deploy to production with a paid service."
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    errors = "\n".join(result["validation"]["errors"])
    assert "unsafe command" in errors
    assert "production deployment" in errors
    assert "paid service" in errors
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


def test_self_iteration_output_guard_allows_negated_safety_requirements(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    stage = valid_self_iteration_stage()
    stage["tasks"][0]["implementation"][0]["prompt"] = (
        "Implement a local-only guard task free of external services, private keys, "
        "production deployments, mainnet writes, paid services, or live trading."
    )
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "planned"
    assert result["validation"]["status"] == "passed"
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == ["guard-stage"]


def test_self_iteration_output_guard_rejects_milestone_mutation_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    write_self_iteration_guard_planner(
        project,
        [valid_self_iteration_stage()],
        mutation="roadmap['milestones'][0]['tasks'][0]['status'] = 'done'",
    )

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    assert any("mutated existing milestones" in error for error in result["validation"]["errors"])
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


def test_self_iteration_checkpoint_gate_clean_git_appends_stage(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    write_self_iteration_guard_planner(project, [valid_self_iteration_stage()])
    init_git_repo(project)

    result = Harness(project).run_self_iteration(reason="checkpoint-clean")

    assert result["status"] == "planned"
    assert result["validation"]["status"] == "passed"
    assert result["checkpoint_gates"]["preflight"]["status"] == "passed"
    assert result["checkpoint_gates"]["preflight"]["reason"] == "clean"
    assert result["checkpoint_gates"]["acceptance"]["status"] == "passed"
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == ["guard-stage"]


def test_self_iteration_checkpoint_gate_blocks_dirty_git_without_mutation(tmp_path, capsys):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    write_self_iteration_guard_planner(
        project,
        [valid_self_iteration_stage()],
        mutation="Path('planner-ran.txt').write_text('ran', encoding='utf-8')",
    )
    init_git_repo(project)
    before_text = roadmap_path.read_text(encoding="utf-8")
    (project / "operator-notes.txt").write_text("draft operator work", encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--self-iterate",
            "--max-self-iterations",
            "1",
            "--max-tasks",
            "1",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert roadmap_path.read_text(encoding="utf-8") == before_text
    assert not (project / "planner-ran.txt").exists()
    iteration = payload["self_iterations"][0]
    assert iteration["status"] == "blocked"
    assert iteration["checkpoint_gate"]["phase"] == "preflight"
    assert iteration["checkpoint_readiness"]["blocking"] is True
    assert iteration["blocking_paths"] == ["operator-notes.txt"]
    assert iteration["dirty_paths"] == ["operator-notes.txt"]
    assert "will not commit or clean" in iteration["recommended_action"]

    report_text = (project / payload["drive_report"]).read_text(encoding="utf-8")
    assert "Checkpoint readiness" in report_text
    assert "operator-notes.txt" in report_text
    assessment = json.loads((project / iteration["report_json"]).read_text(encoding="utf-8"))
    assert assessment["status"] == "blocked"
    assert assessment["checkpoint_readiness"]["blocking_paths"] == ["operator-notes.txt"]

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    latest = status_payload["self_iteration"]["latest_assessment"]
    assert latest["status"] == "blocked"
    assert latest["blocking_paths"] == ["operator-notes.txt"]
    dashboard_latest = status_payload["runtime_dashboard"]["self_iteration"]["latest_assessment"]
    assert dashboard_latest["checkpoint_readiness"]["blocking_paths"] == ["operator-notes.txt"]


def test_self_iteration_checkpoint_gate_allows_roadmap_materialization_dirtiness(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    write_self_iteration_guard_planner(project, [valid_self_iteration_stage()])
    init_git_repo(project)
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["operator_checkpoint_note"] = "safe roadmap materialization draft"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    result = Harness(project).run_self_iteration(reason="checkpoint-roadmap-dirty")

    assert result["status"] == "planned"
    assert result["checkpoint_gates"]["preflight"]["reason"] == "harness_materialization_dirty"
    assert result["checkpoint_gates"]["preflight"]["blocking"] is False
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == ["guard-stage"]
    assert updated["operator_checkpoint_note"] == "safe roadmap materialization draft"


def test_self_iteration_checkpoint_gate_blocks_planner_unrelated_dirty_before_accepting_diff(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    write_self_iteration_guard_planner(
        project,
        [valid_self_iteration_stage()],
        mutation="Path('operator-after.txt').write_text('dirty', encoding='utf-8')",
    )
    init_git_repo(project)

    result = Harness(project).run_self_iteration(reason="checkpoint-acceptance-dirty")

    assert result["status"] == "blocked"
    assert result["checkpoint_gate"]["phase"] == "acceptance"
    assert "operator-after.txt" in result["blocking_paths"]
    assert (project / "operator-after.txt").read_text(encoding="utf-8") == "dirty"
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before
    report = (project / result["report"]).read_text(encoding="utf-8")
    assert "roadmap diff acceptance" in report
    assert "operator-after.txt" in report


def test_workspace_dispatch_self_iteration_checkpoint_readiness_propagates_dirty_git(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dirty = init_workspace_project(workspace, "aa-self-iteration-dirty")
    clean = init_workspace_project(workspace, "bb-self-iteration-clean")
    configure_workspace_self_iteration_project(
        dirty,
        workspace_backoff_planner_source(workspace_backoff_stage("dirty-stage", "dirty-task")),
    )
    configure_workspace_self_iteration_project(
        clean,
        workspace_backoff_planner_source(workspace_backoff_stage("clean-stage", "clean-task")),
    )
    init_git_repo(dirty)
    dirty_before = (dirty / ".engineering/roadmap.yaml").read_text(encoding="utf-8")
    (dirty / "operator-notes.txt").write_text("operator draft", encoding="utf-8")

    assert cli_main(["workspace-drive", "--workspace", str(workspace), "--self-iterate", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"]["project"] == "bb-self-iteration-clean"
    dirty_item = next(item for item in payload["queue"] if item["project"] == "aa-self-iteration-dirty")
    assert dirty_item["eligible"] is False
    assert dirty_item["checkpoint_readiness"]["blocking"] is True
    assert dirty_item["checkpoint_readiness"]["blocking_paths"] == ["operator-notes.txt"]
    assert "checkpoint_not_ready" in {reason["code"] for reason in dirty_item["skip_reasons"]}
    assert (dirty / ".engineering/roadmap.yaml").read_text(encoding="utf-8") == dirty_before

    sidecar = json.loads((workspace / payload["dispatch_report_json"]).read_text(encoding="utf-8"))
    sidecar_dirty = next(item for item in sidecar["queue"] if item["project"] == "aa-self-iteration-dirty")
    assert sidecar_dirty["checkpoint_readiness"]["blocking_paths"] == ["operator-notes.txt"]

    assert cli_main(["status", "--project-root", str(clean), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    dashboard_dirty = next(
        item
        for item in status_payload["runtime_dashboard"]["workspace_dispatch"]["queue"]
        if item["project"] == "aa-self-iteration-dirty"
    )
    assert dashboard_dirty["checkpoint_readiness"]["blocking_paths"] == ["operator-notes.txt"]
    assert "checkpoint_not_ready" in dashboard_dirty["skip_codes"]


def test_self_iteration_shell_planner_adds_continuation_stage(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next generated test stage.",
        "planner": {"name": "test planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "planner.py").write_text(
        """
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
continuation = roadmap.setdefault("continuation", {"enabled": True, "stages": []})
continuation["enabled"] = True
continuation.setdefault("stages", []).append(
    {
        "id": "self-stage",
        "title": "Self Stage",
        "objective": "Create a marker through a generated task.",
        "tasks": [
            {
                "id": "self-generated-test",
                "title": "Self Generated Test",
                "file_scope": ["tests/**"],
                "acceptance": [
                    {
                        "name": "write self marker",
                        "command": "python3 -c \\"from pathlib import Path; Path('self-generated.txt').write_text('ok')\\"",
                    }
                ],
            }
        ],
    }
)
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    harness = Harness(project)
    result = harness.run_self_iteration(reason="test")

    assert result["status"] == "planned"
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert updated["continuation"]["stages"][0]["id"] == "self-stage"
    assert list((project / ".engineering/reports/tasks/assessments").glob("*-self-iteration.md"))


def test_drive_self_iterates_then_runs_generated_stage(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next generated test stage.",
        "planner": {"name": "test planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "planner.py").write_text(
        """
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
continuation = roadmap.setdefault("continuation", {"enabled": True, "stages": []})
continuation["enabled"] = True
continuation.setdefault("stages", []).append(
    {
        "id": "drive-self-stage",
        "title": "Drive Self Stage",
        "objective": "Create a marker through a generated drive task.",
        "tasks": [
            {
                "id": "drive-self-generated-test",
                "title": "Drive Self Generated Test",
                "file_scope": ["tests/**"],
                "acceptance": [
                    {
                        "name": "write drive self marker",
                        "command": "python3 -c \\"from pathlib import Path; Path('drive-self-generated.txt').write_text('ok')\\"",
                    }
                ],
            }
        ],
    }
)
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--self-iterate",
            "--max-self-iterations",
            "1",
            "--max-continuations",
            "2",
            "--max-tasks",
            "1",
        ]
    )

    assert exit_code == 0
    assert (project / "drive-self-generated.txt").read_text(encoding="utf-8") == "ok"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["drive-self-generated-test"]["status"] == "passed"
    report = next((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))
    text = report.read_text(encoding="utf-8")
    assert "Self Iterations" in text
    assert "drive-self-stage" in roadmap_path.read_text(encoding="utf-8")

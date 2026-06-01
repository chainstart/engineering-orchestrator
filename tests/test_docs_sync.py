from __future__ import annotations

import json
from pathlib import Path

from engineering_orchestrator.cli import main as cli_main
from engineering_orchestrator.docs_sync import (
    MANAGED_BLOCK_START,
    audit_documentation_system,
    record_documentation_update,
)
from engineering_orchestrator.io import load_mapping


def seed_docs_project(tmp_path: Path) -> Path:
    project = tmp_path / "target-docs"
    (project / ".engineering").mkdir(parents=True)
    (project / "docs" / "decisions").mkdir(parents=True)
    (project / "docs" / "decisions" / "0001-record.md").write_text("# Decision\n", encoding="utf-8")
    docs = {
        "architecture.md": "# Architecture\n\nHigh-level goals remain stable.\n",
        "roadmap.md": "# Roadmap\n\n- target-task pending\n",
        "roadmap-status.md": "# Roadmap Status\n\n| Task | Status |\n| --- | --- |\n| target-task | pending |\n",
        "implementation-status.md": "# Implementation Status\n\n| Task | Status |\n| --- | --- |\n| target-task | pending |\n",
        "actual-system-state.md": "# Actual System State\n\n| Task | Status |\n| --- | --- |\n| target-task | pending |\n",
        "spec.md": "# Spec\n\n## REQ-TARGET-001\n",
        "traceability.md": "# Traceability\n\n| Task | Status |\n| --- | --- |\n| target-task | pending |\n",
        "task-packages.md": "# Task Packages\n\n| Task | Status |\n| --- | --- |\n| target-task | pending |\n",
        "deployment-status.md": "# Deployment Status\n\nLocal only. Not deployed.\n",
    }
    for name, text in docs.items():
        (project / "docs" / name).write_text(text, encoding="utf-8")
    (project / ".engineering" / "spec_tasks.yaml").write_text(
        json.dumps(
            {
                "kind": "target.spec_tasks.v1",
                "project": "target-docs",
                "source_spec": "docs/spec.md",
                "status_doc": "docs/implementation-status.md",
                "roadmap_doc": "docs/roadmap.md",
                "roadmap_status_doc": "docs/roadmap-status.md",
                "decision_log_dir": "docs/decisions",
                "documentation": {
                    "architecture_blueprint": "docs/architecture.md",
                    "roadmap": "docs/roadmap.md",
                    "roadmap_status": "docs/roadmap-status.md",
                    "development_progress": "docs/implementation-status.md",
                    "actual_system_state": "docs/actual-system-state.md",
                    "canonical_specs": ["docs/spec.md"],
                    "traceability": ["docs/traceability.md"],
                    "task_packages": ["docs/task-packages.md"],
                    "deployment_status": "docs/deployment-status.md",
                },
                "requirements": [{"id": "REQ-TARGET-001", "title": "Target requirement", "status": "pending"}],
                "tasks": [
                    {
                        "id": "target-task",
                        "roadmap_task_id": "roadmap-task",
                        "title": "Target task",
                        "status": "pending",
                        "requirement_ids": ["REQ-TARGET-001"],
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return project


def add_run_roadmap(project: Path) -> None:
    (project / ".engineering" / "roadmap.yaml").write_text(
        json.dumps(
            {
                "version": 1,
                "project": "target-docs",
                "profile": "python-agent",
                "milestones": [
                    {
                        "id": "docs",
                        "title": "Docs",
                        "status": "active",
                        "tasks": [
                            {
                                "id": "roadmap-task",
                                "title": "Roadmap task",
                                "status": "pending",
                                "spec_refs": ["REQ-TARGET-001"],
                                "file_scope": ["**"],
                                "acceptance": [
                                    {
                                        "name": "local evidence",
                                        "command": "python3 -c \"print('docs sync evidence')\"",
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_docs_sync_audit_reports_documentation_role_coverage(tmp_path: Path) -> None:
    project = seed_docs_project(tmp_path)

    result = audit_documentation_system(project)

    assert result["status"] == "passed"
    assert result["documentation_roles"]["architecture_blueprint"] == ["docs/architecture.md"]
    assert any(check["name"] == "local_deployment_status_separation" for check in result["checks"])


def test_docs_sync_audit_reports_missing_docs_and_stale_status(tmp_path: Path) -> None:
    project = seed_docs_project(tmp_path)
    (project / "docs" / "architecture.md").unlink()
    (project / "docs" / "task-packages.md").write_text("| Task | Status |\n| target-task | completed |\n", encoding="utf-8")

    result = audit_documentation_system(project)

    assert result["status"] == "failed"
    assert any(check["name"] == "architecture_blueprint" and check["status"] == "error" for check in result["checks"])
    assert any(check["name"] == "task_package_status_consistency" and check["status"] == "warning" for check in result["checks"])


def test_docs_sync_record_blocks_completion_without_evidence_and_applies_safe_blocks(tmp_path: Path) -> None:
    project = seed_docs_project(tmp_path)

    blocked = record_documentation_update(
        project,
        task_id="roadmap-task",
        status="completed",
        apply=True,
    )
    assert blocked["status"] == "blocked"
    assert not (project / "docs" / "docs_update_log.jsonl").exists()

    proposed = record_documentation_update(
        project,
        task_id="roadmap-task",
        status="completed",
        evidence=["python3 -m pytest -q"],
        apply=False,
    )
    assert proposed["status"] == "proposed"
    assert proposed["task_id"] == "target-task"
    assert any(action["role"] == "architecture_blueprint" and not action["applyable"] for action in proposed["actions"])

    applied = record_documentation_update(
        project,
        task_id="roadmap-task",
        status="completed",
        evidence=["python3 -m pytest -q"],
        requirement_ids=["REQ-TARGET-001"],
        phase="acceptance",
        apply=True,
    )

    assert applied["status"] == "applied"
    assert (project / "docs" / "docs_update_log.jsonl").exists()
    assert MANAGED_BLOCK_START in (project / "docs" / "implementation-status.md").read_text(encoding="utf-8")
    assert MANAGED_BLOCK_START not in (project / "docs" / "architecture.md").read_text(encoding="utf-8")
    task = load_mapping(project / ".engineering" / "spec_tasks.yaml")["tasks"][0]
    assert task["documentation_sync"]["status"] == "applied"
    assert "docs/implementation-status.md" in task["documentation_sync"]["updated_paths"]


def test_docs_sync_preserves_multiple_managed_rows_in_same_document(tmp_path: Path) -> None:
    project = seed_docs_project(tmp_path)
    spec_tasks_path = project / ".engineering" / "spec_tasks.yaml"
    payload = load_mapping(spec_tasks_path)
    payload["documentation"]["roadmap_status"] = "docs/implementation-status.md"
    payload["documentation"]["actual_system_state"] = "docs/implementation-status.md"
    spec_tasks_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    applied = record_documentation_update(
        project,
        task_id="roadmap-task",
        status="completed",
        evidence=["rg -n \"docs-sync|documentation\" docs src tests"],
        apply=True,
    )

    assert applied["status"] == "applied"
    text = (project / "docs" / "implementation-status.md").read_text(encoding="utf-8")
    assert text.count("| target-task | roadmap-task | actual_system_state | completed |") == 1
    assert text.count("| target-task | roadmap-task | development_progress | completed |") == 1
    assert text.count("| target-task | roadmap-task | roadmap_status | completed |") == 1


def test_docs_sync_cli_audit_propose_and_record(tmp_path: Path, capsys) -> None:
    project = seed_docs_project(tmp_path)

    assert cli_main(["docs-sync", "audit", "--project-root", str(project), "--json"]) == 0
    audit_output = json.loads(capsys.readouterr().out)
    assert audit_output["status"] == "passed"

    assert (
        cli_main(
            [
                "docs-sync",
                "propose",
                "--project-root",
                str(project),
                "--task-id",
                "roadmap-task",
                "--evidence",
                "pytest target",
                "--json",
            ]
        )
        == 0
    )
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["status"] == "proposed"
    assert proposal["applyable_action_count"] > 0

    assert (
        cli_main(
            [
                "docs-sync",
                "record",
                "--project-root",
                str(project),
                "--task-id",
                "roadmap-task",
                "--evidence",
                "pytest target",
                "--apply",
                "--json",
            ]
        )
        == 0
    )
    record_output = json.loads(capsys.readouterr().out)
    assert record_output["status"] == "applied"


def test_completed_run_records_docs_sync_in_manifest_and_report(tmp_path: Path, capsys) -> None:
    project = seed_docs_project(tmp_path)
    add_run_roadmap(project)

    assert cli_main(["run", "--project-root", str(project), "--task", "roadmap-task", "--json"]) == 0
    run_results = json.loads(capsys.readouterr().out)
    result = run_results[0]

    assert result["docs_sync"]["status"] == "applied"
    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["docs_sync"]["status"] == "applied"
    assert manifest["target_sync"]["docs_sync"]["docs_update_log"] == "docs/docs_update_log.jsonl"
    report = (project / result["report"]).read_text(encoding="utf-8")
    assert "## Target Synchronization" in report
    assert "Docs sync" in report


def test_completed_run_skips_docs_sync_without_declared_documentation_roles(tmp_path: Path, capsys) -> None:
    project = seed_docs_project(tmp_path)
    payload = load_mapping(project / ".engineering" / "spec_tasks.yaml")
    payload.pop("documentation")
    payload.pop("roadmap_doc")
    payload.pop("roadmap_status_doc")
    (project / ".engineering" / "spec_tasks.yaml").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    add_run_roadmap(project)

    assert cli_main(["run", "--project-root", str(project), "--task", "roadmap-task", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)[0]

    assert result["docs_sync"]["status"] == "skipped"
    assert result["docs_sync"]["reason"] == "documentation_roles_not_configured"
    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["docs_sync"]["status"] == "skipped"

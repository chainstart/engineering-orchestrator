from __future__ import annotations

import json
from pathlib import Path

from engineering_orchestrator.cli import main as cli_main
from engineering_orchestrator.io import load_mapping
from engineering_orchestrator.spec_sync import audit_spec_system, record_spec_task_update


def seed_spec_project(tmp_path: Path) -> Path:
    project = tmp_path / "target"
    (project / ".engineering").mkdir(parents=True)
    (project / "docs" / "decisions").mkdir(parents=True)
    (project / "docs" / "spec.md").write_text("# Spec\n\n## REQ-TARGET-001\n", encoding="utf-8")
    (project / "docs" / "implementation_status.md").write_text("# Status\n", encoding="utf-8")
    (project / ".engineering" / "spec_tasks.yaml").write_text(
        json.dumps(
            {
                "kind": "target.spec_tasks.v1",
                "project": "target",
                "source_spec": "docs/spec.md",
                "status_doc": "docs/implementation_status.md",
                "decision_log_dir": "docs/decisions",
                "requirements": [{"id": "REQ-TARGET-001", "title": "Target requirement", "status": "pending"}],
                "tasks": [
                    {
                        "id": "target-task",
                        "title": "Target task",
                        "status": "pending",
                        "requirement_ids": ["REQ-TARGET-001"],
                        "evidence": [],
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return project


def test_audit_spec_system_passes_for_complete_dynamic_spec_files(tmp_path: Path) -> None:
    project = seed_spec_project(tmp_path)

    result = audit_spec_system(project)

    assert result["status"] == "passed"
    assert result["requirement_count"] == 1
    assert result["task_count"] == 1


def test_record_spec_task_update_supports_preview_and_apply(tmp_path: Path) -> None:
    project = seed_spec_project(tmp_path)

    preview = record_spec_task_update(
        project,
        task_id="target-task",
        status="completed",
        evidence=["pytest target"],
        note="done",
        apply=False,
    )
    assert preview["status"] == "proposed"
    assert load_mapping(project / ".engineering" / "spec_tasks.yaml")["tasks"][0]["status"] == "pending"

    applied = record_spec_task_update(
        project,
        task_id="target-task",
        status="completed",
        evidence=["pytest target"],
        note="done",
        phase="acceptance",
        apply=True,
    )

    payload = load_mapping(project / ".engineering" / "spec_tasks.yaml")
    task = payload["tasks"][0]
    assert applied["status"] == "updated"
    assert task["status"] == "completed"
    assert task["last_phase"] == "acceptance"
    assert task["evidence"][-1]["value"] == "pytest target"
    assert (project / "docs" / "spec_update_log.jsonl").exists()


def test_spec_sync_cli_audit_and_record(tmp_path: Path, capsys) -> None:
    project = seed_spec_project(tmp_path)

    assert cli_main(["spec-sync", "audit", "--project-root", str(project), "--json"]) == 0
    audit_output = json.loads(capsys.readouterr().out)
    assert audit_output["status"] == "passed"

    assert (
        cli_main(
            [
                "spec-sync",
                "record",
                "--project-root",
                str(project),
                "--task-id",
                "target-task",
                "--status",
                "completed",
                "--evidence",
                "pytest target",
                "--apply",
                "--json",
            ]
        )
        == 0
    )
    record_output = json.loads(capsys.readouterr().out)
    assert record_output["status"] == "updated"

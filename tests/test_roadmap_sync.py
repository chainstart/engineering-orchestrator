import json

from engineering_orchestrator.roadmap_sync import record_roadmap_task_completion


def write_roadmap(tmp_path, roadmap):
    engineering = tmp_path / ".engineering"
    engineering.mkdir()
    roadmap_path = engineering / "roadmap.yaml"
    roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
    return roadmap_path


def test_record_roadmap_task_completion_updates_task_and_rolls_up_milestone(tmp_path):
    roadmap_path = write_roadmap(
        tmp_path,
        {
            "milestones": [
                {
                    "id": "m1",
                    "status": "planned",
                    "tasks": [
                        {"id": "task-a", "status": "completed"},
                        {"id": "task-b", "status": "pending"},
                    ],
                }
            ]
        },
    )

    result = record_roadmap_task_completion(tmp_path, task_id="task-b")

    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert result["status"] == "applied"
    assert result["changed_fields"] == ["task.status", "milestone.status"]
    assert updated["milestones"][0]["status"] == "completed"
    assert updated["milestones"][0]["tasks"][1]["status"] == "completed"


def test_record_roadmap_task_completion_keeps_parent_open_until_siblings_complete(tmp_path):
    roadmap_path = write_roadmap(
        tmp_path,
        {
            "milestones": [
                {
                    "id": "m1",
                    "status": "planned",
                    "tasks": [
                        {"id": "task-a", "status": "pending"},
                        {"id": "task-b", "status": "pending"},
                    ],
                }
            ]
        },
    )

    result = record_roadmap_task_completion(tmp_path, task_id="task-b")

    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert result["status"] == "applied"
    assert result["changed_fields"] == ["task.status"]
    assert updated["milestones"][0]["status"] == "planned"
    assert updated["milestones"][0]["tasks"][1]["status"] == "completed"


def test_record_roadmap_task_completion_supports_continuation_stage_rollup(tmp_path):
    roadmap_path = write_roadmap(
        tmp_path,
        {
            "milestones": [],
            "continuation": {
                "stages": [
                    {
                        "id": "stage-a",
                        "status": "pending",
                        "tasks": [
                            {"id": "task-a", "status": "done"},
                            {"id": "task-b", "status": "pending"},
                        ],
                    }
                ]
            },
        },
    )

    result = record_roadmap_task_completion(tmp_path, task_id="task-b")

    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    stage = updated["continuation"]["stages"][0]
    assert result["status"] == "applied"
    assert result["changed_fields"] == ["task.status", "continuation_stage.status"]
    assert stage["status"] == "completed"
    assert stage["tasks"][1]["status"] == "completed"

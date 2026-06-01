from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .core import utc_now
from .io import write_json


MERGE_PLANNER_SCHEMA_VERSION = 1


def plan_parallel_merges(
    project_root: Path,
    *,
    roadmap: dict[str, Any] | None = None,
    base_ref: str | None = None,
    branches: list[str] | tuple[str, ...] | None = None,
    worktrees: list[str] | tuple[str, ...] | None = None,
    task_ids: list[str] | tuple[str, ...] | None = None,
    post_merge_acceptance: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    root = project_root.resolve()
    repo_result = _git(root, ["rev-parse", "--show-toplevel"])
    if repo_result["returncode"] != 0:
        return {
            "schema_version": MERGE_PLANNER_SCHEMA_VERSION,
            "kind": "engineering-harness.merge-planner",
            "status": "failed",
            "planned_at": utc_now(),
            "project_root": str(root),
            "message": "project root is not inside a git repository",
            "stderr": repo_result["stderr"],
            "safety": _safety_payload(),
        }

    repo_root = Path(repo_result["stdout"].strip()).resolve()
    current_branch = _current_branch(repo_root)
    base = str(base_ref or current_branch or "HEAD")
    base_head = _rev_parse(repo_root, base)
    worktree_index = _git_worktrees(repo_root)
    candidate_inputs = _candidate_inputs(
        repo_root,
        base_ref=base,
        branches=branches or (),
        worktrees=worktrees or (),
        worktree_index=worktree_index,
    )

    candidates = [
        _candidate_plan(repo_root, base_ref=base, base_head=base_head, candidate=item, worktree_index=worktree_index)
        for item in candidate_inputs
    ]
    conflicts = _likely_conflicts(candidates)
    candidate_conflict_counts = _candidate_conflict_counts(candidates, conflicts)
    merge_order = _recommended_merge_order(candidates, candidate_conflict_counts)
    acceptance = _post_merge_acceptance(
        roadmap or {},
        candidates,
        task_ids=task_ids or (),
        manual_commands=post_merge_acceptance or (),
    )
    dirty_paths = sorted(
        dict.fromkeys(
            path
            for candidate in candidates
            for path in candidate.get("dirty_paths", [])
            if str(path).strip()
        )
    )
    changed_files = sorted(
        dict.fromkeys(
            path
            for candidate in candidates
            for path in candidate.get("changed_paths", [])
            if str(path).strip()
        )
    )

    status = "planned"
    if base_head is None:
        status = "needs_attention"
    elif any(candidate.get("status") == "invalid" for candidate in candidates):
        status = "needs_attention"
    elif conflicts["count"] > 0 or dirty_paths:
        status = "needs_attention"

    return {
        "schema_version": MERGE_PLANNER_SCHEMA_VERSION,
        "kind": "engineering-harness.merge-planner",
        "status": status,
        "planned_at": utc_now(),
        "project_root": str(root),
        "repository_root": str(repo_root),
        "base": {
            "ref": base,
            "head": base_head,
            "status": "ready" if base_head else "invalid",
            "current_branch": current_branch,
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "dirty_paths": dirty_paths,
        "changed_files": changed_files,
        "likely_conflicts": conflicts,
        "recommended_merge_order": merge_order,
        "post_merge_acceptance": acceptance,
        "safety": _safety_payload(),
    }


def write_merge_plan_report(project_root: Path, payload: dict[str, Any]) -> dict[str, str]:
    report_dir = project_root.resolve() / ".engineering/reports/tasks/merge-plans"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug_from_timestamp()}-merge-plan"
    report_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    payload["merge_plan_report"] = _project_relative(project_root, report_path)
    payload["merge_plan_report_json"] = _project_relative(project_root, json_path)
    report_path.write_text(_merge_plan_markdown(payload), encoding="utf-8")
    write_json(json_path, payload)
    return {
        "report": payload["merge_plan_report"],
        "report_json": payload["merge_plan_report_json"],
    }


def _candidate_inputs(
    repo_root: Path,
    *,
    base_ref: str,
    branches: list[str] | tuple[str, ...],
    worktrees: list[str] | tuple[str, ...],
    worktree_index: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for branch in branches:
        branch_name = str(branch).strip()
        if not branch_name:
            continue
        key = ("branch", branch_name)
        if key in seen:
            continue
        seen.add(key)
        inputs.append({"source": "branch", "branch": branch_name, "ref": branch_name})

    for worktree in worktrees:
        path = Path(str(worktree)).expanduser().resolve()
        key = ("worktree", str(path))
        if key in seen:
            continue
        seen.add(key)
        inputs.append({"source": "worktree", "path": str(path)})

    if inputs:
        return inputs

    for worktree in worktree_index:
        path = Path(str(worktree.get("path") or "")).resolve()
        if path == repo_root:
            continue
        ref = str(worktree.get("branch") or worktree.get("head") or "").strip()
        if not ref or ref == base_ref:
            continue
        key = ("worktree", str(path))
        if key in seen:
            continue
        seen.add(key)
        inputs.append({"source": "worktree", "path": str(path), "ref": ref})
    return inputs


def _candidate_plan(
    repo_root: Path,
    *,
    base_ref: str,
    base_head: str | None,
    candidate: dict[str, Any],
    worktree_index: list[dict[str, Any]],
) -> dict[str, Any]:
    source = str(candidate.get("source") or "branch")
    worktree_path = candidate.get("path")
    worktree_info: dict[str, Any] | None = None
    if worktree_path:
        worktree_info = _worktree_info_for_path(worktree_index, Path(str(worktree_path)))
    branch = str(candidate.get("branch") or (worktree_info or {}).get("branch") or "").strip() or None
    ref = str(candidate.get("ref") or branch or (worktree_info or {}).get("head") or "").strip()
    if source == "branch":
        worktree_info = _worktree_info_for_branch(worktree_index, ref) or worktree_info
        if worktree_info and not worktree_path:
            worktree_path = worktree_info.get("path")
    if not ref:
        ref = "HEAD"

    candidate_id = _candidate_id(source, ref, worktree_path)
    worktree_missing = bool(worktree_path and not Path(str(worktree_path)).exists())
    head = _rev_parse(repo_root, ref)
    base = _merge_base(repo_root, base_ref, ref)
    diff_base = base or base_head or base_ref
    committed_files = _diff_name_status(repo_root, diff_base, ref)
    base_changed_files = _diff_name_status(repo_root, diff_base, base_ref)
    dirty_entries = _dirty_entries(Path(str(worktree_path))) if worktree_path and not worktree_missing else []
    dirty_paths = [str(entry["path"]) for entry in dirty_entries]
    changed_paths = sorted(
        dict.fromkeys(
            [
                *(str(item["path"]) for item in committed_files if item.get("path")),
                *dirty_paths,
            ]
        )
    )
    base_conflict_paths = sorted(
        set(str(item["path"]) for item in committed_files)
        & set(str(item["path"]) for item in base_changed_files)
    )
    status = "ready" if head and not worktree_missing else "invalid"
    if worktree_missing:
        message = f"worktree path does not exist: {worktree_path}"
    elif head:
        message = "candidate inspected without merge actions"
    else:
        message = f"could not resolve candidate ref `{ref}`"
    return {
        "id": candidate_id,
        "source": source,
        "status": status,
        "message": message,
        "branch": branch,
        "ref": ref,
        "head": head,
        "merge_base": base,
        "worktree_path": str(worktree_path) if worktree_path else None,
        "dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
        "dirty_path_states": dirty_entries,
        "changed_files": committed_files,
        "changed_paths": changed_paths,
        "base_changed_paths": [str(item["path"]) for item in base_changed_files],
        "base_conflict_paths": base_conflict_paths,
        "risk": {
            "dirty_path_count": len(dirty_paths),
            "changed_file_count": len(changed_paths),
            "base_conflict_path_count": len(base_conflict_paths),
        },
    }


def _likely_conflicts(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []

    for candidate in candidates:
        paths = candidate.get("base_conflict_paths", [])
        if paths:
            conflicts.append(
                {
                    "kind": "base_overlap",
                    "candidate_ids": [candidate["id"]],
                    "paths": paths,
                    "message": "base and candidate changed the same path since their merge base",
                }
            )

    for index, left in enumerate(candidates):
        left_paths = set(str(path) for path in left.get("changed_paths", []))
        left_dirty = set(str(path) for path in left.get("dirty_paths", []))
        for right in candidates[index + 1 :]:
            right_paths = set(str(path) for path in right.get("changed_paths", []))
            right_dirty = set(str(path) for path in right.get("dirty_paths", []))
            overlap = sorted(left_paths & right_paths)
            if overlap:
                conflicts.append(
                    {
                        "kind": "candidate_overlap",
                        "candidate_ids": [left["id"], right["id"]],
                        "paths": overlap,
                        "message": "multiple candidates change the same path",
                    }
                )
            dirty_overlap = sorted((left_dirty & right_paths) | (right_dirty & left_paths))
            if dirty_overlap:
                conflicts.append(
                    {
                        "kind": "dirty_path_overlap",
                        "candidate_ids": [left["id"], right["id"]],
                        "paths": dirty_overlap,
                        "message": "dirty worktree paths overlap another candidate change",
                    }
                )

    return {
        "status": "conflicts_likely" if conflicts else "clear",
        "count": len(conflicts),
        "items": conflicts,
    }


def _candidate_conflict_counts(candidates: list[dict[str, Any]], conflicts: dict[str, Any]) -> dict[str, int]:
    counts = {str(candidate["id"]): 0 for candidate in candidates}
    for conflict in conflicts.get("items", []):
        path_count = len(conflict.get("paths", [])) or 1
        for candidate_id in conflict.get("candidate_ids", []):
            counts[str(candidate_id)] = counts.get(str(candidate_id), 0) + path_count
    return counts


def _recommended_merge_order(candidates: list[dict[str, Any]], conflict_counts: dict[str, int]) -> list[dict[str, Any]]:
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        dirty_count = len(candidate.get("dirty_paths", []))
        changed_count = len(candidate.get("changed_paths", []))
        conflict_count = int(conflict_counts.get(str(candidate["id"]), 0))
        score = dirty_count * 1000 + conflict_count * 100 + changed_count
        reasons: list[str] = []
        if dirty_count:
            reasons.append("dirty worktree paths should be reviewed or checkpointed before merge")
        if conflict_count:
            reasons.append("likely conflict paths detected")
        if not reasons:
            reasons.append(f"{changed_count} changed file(s), no likely conflicts")
        scored.append((score, index, {"candidate_id": candidate["id"], "score": score, "reasons": reasons}))

    ordered = [item for _, _, item in sorted(scored, key=lambda item: (item[0], item[1]))]
    for position, item in enumerate(ordered, start=1):
        item["position"] = position
    return ordered


def _post_merge_acceptance(
    roadmap: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    task_ids: list[str] | tuple[str, ...],
    manual_commands: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    explicit_task_ids = {str(task_id).strip() for task_id in task_ids if str(task_id).strip()}
    candidate_labels = [
        str(value)
        for candidate in candidates
        for value in (
            candidate.get("id"),
            candidate.get("branch"),
            candidate.get("ref"),
            Path(str(candidate.get("worktree_path"))).name if candidate.get("worktree_path") else None,
        )
        if value
    ]
    commands: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for task in _roadmap_tasks(roadmap):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        matched = task_id in explicit_task_ids or any(_label_matches_task(label, task_id) for label in candidate_labels)
        if not matched:
            continue
        for phase in ("acceptance", "e2e"):
            for command in task.get(phase, []) if isinstance(task.get(phase), list) else []:
                if not isinstance(command, dict) or not str(command.get("command") or "").strip():
                    continue
                payload = {
                    "source": "roadmap_task",
                    "task_id": task_id,
                    "phase": phase,
                    "name": str(command.get("name") or command.get("command")),
                    "command": str(command.get("command")),
                }
                key = (payload["source"], payload["task_id"], payload["phase"], payload["command"])
                if key not in seen:
                    seen.add(key)
                    commands.append(payload)

    for command in manual_commands:
        command_text = str(command).strip()
        if not command_text:
            continue
        payload = {
            "source": "manual",
            "task_id": None,
            "phase": "post-merge acceptance",
            "name": "manual post-merge acceptance",
            "command": command_text,
        }
        key = (payload["source"], "", payload["phase"], payload["command"])
        if key not in seen:
            seen.add(key)
            commands.append(payload)

    return {
        "command_count": len(commands),
        "commands": commands,
        "message": (
            "Run these post-merge acceptance commands after the planned merges."
            if commands
            else "No task-specific post-merge acceptance commands matched; add --task or --post-merge-acceptance."
        ),
    }


def _roadmap_tasks(roadmap: dict[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for milestone in roadmap.get("milestones", []) if isinstance(roadmap.get("milestones"), list) else []:
        if isinstance(milestone, dict) and isinstance(milestone.get("tasks"), list):
            tasks.extend(task for task in milestone["tasks"] if isinstance(task, dict))
    continuation = roadmap.get("continuation")
    stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
    for stage in stages if isinstance(stages, list) else []:
        if isinstance(stage, dict) and isinstance(stage.get("tasks"), list):
            tasks.extend(task for task in stage["tasks"] if isinstance(task, dict))
    return tasks


def _label_matches_task(label: str, task_id: str) -> bool:
    normalized_label = label.lower()
    normalized_task = task_id.lower()
    return normalized_label == normalized_task or normalized_task in normalized_label


def _git_worktrees(repo_root: Path) -> list[dict[str, Any]]:
    result = _git(repo_root, ["worktree", "list", "--porcelain"])
    if result["returncode"] != 0:
        return []
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in result["stdout"].splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                worktrees.append(current)
            current = {"path": value.strip()}
        elif key == "HEAD":
            current["head"] = value.strip()
        elif key == "branch":
            branch = value.strip()
            current["branch"] = branch.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
    if current:
        worktrees.append(current)
    return worktrees


def _worktree_info_for_path(worktrees: list[dict[str, Any]], path: Path) -> dict[str, Any] | None:
    resolved = path.resolve()
    for item in worktrees:
        try:
            if Path(str(item.get("path") or "")).resolve() == resolved:
                return item
        except OSError:
            continue
    return None


def _worktree_info_for_branch(worktrees: list[dict[str, Any]], branch: str) -> dict[str, Any] | None:
    for item in worktrees:
        if str(item.get("branch") or "") == branch:
            return item
    return None


def _diff_name_status(repo_root: Path, left_ref: str, right_ref: str) -> list[dict[str, str]]:
    result = _git(repo_root, ["diff", "--name-status", "--find-renames", left_ref, right_ref])
    if result["returncode"] != 0:
        return []
    files: list[dict[str, str]] = []
    for line in result["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            files.append({"status": status, "path": _normalize_repo_path(parts[2]), "old_path": _normalize_repo_path(parts[1])})
        else:
            files.append({"status": status, "path": _normalize_repo_path(parts[1])})
    return sorted(files, key=lambda item: str(item.get("path", "")))


def _dirty_entries(worktree_path: Path) -> list[dict[str, Any]]:
    result = _git(worktree_path, ["status", "--porcelain", "--untracked-files=all"])
    if result["returncode"] != 0:
        return []
    entries: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            _, _, path = path.partition(" -> ")
        index_status = status[0]
        worktree_status = status[1]
        states: list[str] = []
        if status == "??":
            states.append("untracked")
        if index_status not in {" ", "?"}:
            states.append("staged")
        if index_status in {"M", "A", "R", "C", "T"} or worktree_status in {"M", "T"}:
            states.append("modified")
        if index_status == "D" or worktree_status == "D":
            states.append("deleted")
        entries.append({"path": _normalize_repo_path(path), "status": status, "states": states})
    return sorted(entries, key=lambda item: str(item["path"]))


def _merge_base(repo_root: Path, left_ref: str, right_ref: str) -> str | None:
    result = _git(repo_root, ["merge-base", left_ref, right_ref])
    return result["stdout"].strip() if result["returncode"] == 0 and result["stdout"].strip() else None


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    result = _git(repo_root, ["rev-parse", "--verify", ref])
    return result["stdout"].strip() if result["returncode"] == 0 and result["stdout"].strip() else None


def _current_branch(repo_root: Path) -> str | None:
    result = _git(repo_root, ["branch", "--show-current"])
    branch = result["stdout"].strip()
    return branch or None


def _git(cwd: Path, args: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc)[-8000:],
        }
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
    }


def _candidate_id(source: str, ref: str, worktree_path: Any) -> str:
    if source == "worktree":
        suffix = Path(str(worktree_path)).name if worktree_path else ref
        return f"worktree:{suffix}"
    return f"branch:{ref}"


def _normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _safety_payload() -> dict[str, Any]:
    return {
        "destructive_git_actions_performed": False,
        "non_destructive_git_commands": [
            "git rev-parse",
            "git branch --show-current",
            "git worktree list --porcelain",
            "git merge-base",
            "git diff --name-status",
            "git status --porcelain",
        ],
        "excluded_git_actions": ["merge", "rebase", "reset", "checkout", "commit", "push"],
    }


def _merge_plan_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Merge Planning Report",
        "",
        "This merge planner report is non-destructive and intended for parallel worktree or task branch coordination.",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Base: `{payload.get('base', {}).get('ref')}`",
        f"- Candidates: `{payload.get('candidate_count', 0)}`",
        f"- Dirty paths: `{len(payload.get('dirty_paths', []))}`",
        f"- Changed files: `{len(payload.get('changed_files', []))}`",
        f"- Likely conflict groups: `{payload.get('likely_conflicts', {}).get('count', 0)}`",
        f"- Destructive git actions performed: `{str(payload.get('safety', {}).get('destructive_git_actions_performed')).lower()}`",
        "",
        "## Worktree And Branch Inputs",
        "",
    ]
    candidates = payload.get("candidates", [])
    if not candidates:
        lines.append("No worktree or branch candidates were selected.")
    for candidate in candidates:
        lines.extend(
            [
                f"- `{candidate.get('id')}`",
                f"  - Source: `{candidate.get('source')}`",
                f"  - Branch: `{candidate.get('branch') or 'none'}`",
                f"  - Worktree: `{candidate.get('worktree_path') or 'none'}`",
                f"  - Dirty paths: `{len(candidate.get('dirty_paths', []))}`",
                f"  - Changed files: `{len(candidate.get('changed_paths', []))}`",
            ]
        )
    lines.extend(["", "## Changed Files", ""])
    changed = payload.get("changed_files", [])
    if changed:
        lines.extend(f"- `{path}`" for path in changed[:80])
    else:
        lines.append("No changed files were detected.")
    lines.extend(["", "## Likely Conflicts", ""])
    conflicts = payload.get("likely_conflicts", {}).get("items", [])
    if conflicts:
        for conflict in conflicts:
            lines.append(
                f"- `{conflict.get('kind')}` for `{', '.join(conflict.get('candidate_ids', []))}`: "
                f"{', '.join(f'`{path}`' for path in conflict.get('paths', [])[:20])}"
            )
    else:
        lines.append("No likely conflict paths were detected.")
    lines.extend(["", "## Recommended Merge Order", ""])
    order = payload.get("recommended_merge_order", [])
    if order:
        for item in order:
            lines.append(
                f"{item.get('position')}. `{item.get('candidate_id')}` - {'; '.join(item.get('reasons', []))}"
            )
    else:
        lines.append("No merge order is available because there are no candidates.")
    lines.extend(["", "## Post-Merge Acceptance", ""])
    acceptance = payload.get("post_merge_acceptance", {})
    lines.append(str(acceptance.get("message", "")))
    commands = acceptance.get("commands", [])
    if commands:
        lines.append("")
        for command in commands:
            lines.extend(
                [
                    f"- `{command.get('task_id') or command.get('source')}` `{command.get('phase')}` {command.get('name')}",
                    "",
                    "  ```bash",
                    f"  {command.get('command')}",
                    "  ```",
                    "",
                ]
            )
    lines.extend(
        [
            "## Machine Summary",
            "",
            "```json",
            json.dumps(
                {
                    "status": payload.get("status"),
                    "candidate_count": payload.get("candidate_count"),
                    "dirty_paths": payload.get("dirty_paths", []),
                    "likely_conflict_count": payload.get("likely_conflicts", {}).get("count", 0),
                    "recommended_merge_order": payload.get("recommended_merge_order", []),
                    "post_merge_acceptance_count": payload.get("post_merge_acceptance", {}).get("command_count", 0),
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _slug_from_timestamp() -> str:
    return utc_now().replace("-", "").replace(":", "").replace(".", "").replace("Z", "Z").replace("T", "T")


def _project_relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)

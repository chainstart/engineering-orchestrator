from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .browser_e2e import (
    browser_user_experience_command,
    browser_user_experience_gate,
    is_browser_experience_kind,
)
from .domain_frontend import (
    DOMAIN_FRONTEND_GENERATOR_ID,
    build_domain_frontend_plan,
    derive_domain_frontend_decision,
    keyword_matches as domain_frontend_keyword_matches,
)
from .goal_intake import normalize_goal_intake
from .io import write_mapping


GOAL_ROADMAP_PLAN_SCHEMA_VERSION = 1
GOAL_ROADMAP_PLAN_KIND = "engineering-harness.goal-roadmap-plan.v1"
GOAL_ROADMAP_PLANNER_ID = "engineering-harness-goal-roadmap-planner"
MIN_GOAL_STAGE_COUNT = 1
DEFAULT_GOAL_STAGE_COUNT = 4
MAX_GOAL_STAGE_COUNT = 4

GOAL_IMPLEMENTATION_FILE_SCOPE = [
    "src/**",
    "app/**",
    "web/**",
    "frontend/**",
    "cli/**",
    "api/**",
    "docs/**",
    "examples/**",
    "tests/**",
    "package.json",
    "pyproject.toml",
]
GOAL_HARDENING_FILE_SCOPE = [
    ".engineering/**",
    "docs/**",
    "tests/**",
    "src/**",
    "app/**",
    "web/**",
    "cli/**",
    "api/**",
    "scripts/**",
    "examples/**",
    "package.json",
    "pyproject.toml",
]

_SELF_ITERATION_KEYWORDS = (
    "agent",
    "autonomous",
    "backtest",
    "continuous",
    "iterate",
    "iteration",
    "long-running",
    "planner",
    "proof",
    "research",
    "roadmap",
    "self-iterate",
    "theorem",
    "worker",
)


def plan_goal_roadmap(
    *,
    project_root: Path,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None = None,
    constraints: list[str] | tuple[str, ...] | None = None,
    desired_experience_kind: str | None = None,
    stage_count: int | None = DEFAULT_GOAL_STAGE_COUNT,
) -> dict[str, Any]:
    """Build a deterministic starter roadmap proposal from a local goal-intake contract."""

    project_root = project_root.resolve()
    normalized_stage_count = _normalize_stage_count(stage_count)
    goal_intake = normalize_goal_intake(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        constraints=constraints,
        desired_experience_kind=desired_experience_kind,
    )
    roadmap = _starter_roadmap(
        project_root=project_root,
        goal_intake=goal_intake,
        stage_count=normalized_stage_count,
    )
    roadmap_path = project_root / ".engineering" / "roadmap.yaml"
    return {
        "schema_version": GOAL_ROADMAP_PLAN_SCHEMA_VERSION,
        "kind": GOAL_ROADMAP_PLAN_KIND,
        "status": "proposed",
        "materialized": False,
        "project": goal_intake["project"]["name"],
        "profile": goal_intake["project"]["profile"],
        "project_root": str(project_root),
        "roadmap_path": str(roadmap_path),
        "goal_intake": goal_intake,
        "experience": roadmap["experience"],
        "stage_count": len(roadmap["continuation"]["stages"]),
        "roadmap": roadmap,
    }


def materialize_goal_roadmap(
    *,
    project_root: Path,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None = None,
    constraints: list[str] | tuple[str, ...] | None = None,
    desired_experience_kind: str | None = None,
    stage_count: int | None = DEFAULT_GOAL_STAGE_COUNT,
    force: bool = False,
) -> dict[str, Any]:
    """Write the proposed starter roadmap to `.engineering/roadmap.yaml`."""

    proposal = plan_goal_roadmap(
        project_root=project_root,
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        constraints=constraints,
        desired_experience_kind=desired_experience_kind,
        stage_count=stage_count,
    )
    roadmap_path = Path(proposal["roadmap_path"])
    if roadmap_path.exists() and not force:
        raise FileExistsError(f"{roadmap_path} already exists. Use --force to replace it.")
    write_mapping(roadmap_path, proposal["roadmap"])
    result = deepcopy(proposal)
    result["status"] = "materialized"
    result["materialized"] = True
    return result


def _starter_roadmap(
    *,
    project_root: Path,
    goal_intake: dict[str, Any],
    stage_count: int,
) -> dict[str, Any]:
    project = goal_intake["project"]
    goal = str(goal_intake["goal"]["text"])
    profile = str(project["profile"])
    project_name = str(project["name"])
    project_slug = str(project["slug"])
    blueprint_path = goal_intake["blueprint"]["path"]
    constraints = list(goal_intake.get("constraints", []))
    experience = _experience_plan(
        project_name=project_name,
        profile=profile,
        goal_text=goal,
        blueprint_path=blueprint_path,
        explicit_kind=goal_intake["experience"]["kind"],
    )
    continuation_stages = _continuation_stages(
        project_root=project_root,
        project_name=project_name,
        project_slug=project_slug,
        profile=profile,
        goal_text=goal,
        constraints=constraints,
        experience=experience,
        blueprint_path=blueprint_path,
        stage_count=stage_count,
    )
    return {
        "version": 1,
        "project": project_name,
        "profile": profile,
        "default_timeout_seconds": 1800,
        "state_path": ".engineering/state/harness-state.json",
        "decision_log_path": ".engineering/state/decision-log.jsonl",
        "report_dir": ".engineering/reports/tasks",
        "generated_by": GOAL_ROADMAP_PLANNER_ID,
        "planning": {
            "stage_count": len(continuation_stages),
            "stage_count_requested": stage_count,
            "stage_count_default": DEFAULT_GOAL_STAGE_COUNT,
            "stage_count_max": MAX_GOAL_STAGE_COUNT,
            "domain_frontend": deepcopy(experience.get("decision_contract", {})),
        },
        "generated_from": {
            "kind": goal_intake["kind"],
            "schema_version": goal_intake["schema_version"],
            "project_slug": project_slug,
            "goal": goal,
            "blueprint_path": blueprint_path,
            "constraints": constraints,
            "experience_kind": experience["kind"],
            "experience_provided": bool(goal_intake["experience"]["provided"]),
            "domain_frontend": deepcopy(experience.get("decision_contract", {})),
            "safety_mode": goal_intake["safety"]["mode"],
        },
        "goal": {
            "text": goal,
            "blueprint": blueprint_path,
            "constraints": constraints,
            "safety": {
                "mode": "local-only",
                "allow_live_services": False,
                "rules": list(goal_intake["safety"]["rules"]),
            },
        },
        "experience": experience,
        "milestones": [
            {
                "id": "baseline",
                "title": "Baseline Local Validation",
                "status": "active",
                "objective": (
                    "Establish local, deterministic acceptance before autonomous implementation work expands."
                ),
                "tasks": [_baseline_task(profile=profile)],
            }
        ],
        "continuation": {
            "enabled": True,
            "goal": goal,
            "blueprint": blueprint_path,
            "stages": continuation_stages,
        },
        "self_iteration": _self_iteration_guidance(
            project_root=project_root,
            project_name=project_name,
            profile=profile,
            goal_text=goal,
            blueprint_path=blueprint_path,
        ),
    }


def _baseline_task(*, profile: str) -> dict[str, Any]:
    return {
        "id": "baseline-roadmap",
        "title": "Verify starter roadmap and local safety bounds",
        "status": "pending",
        "max_attempts": 2,
        "max_task_iterations": 1,
        "file_scope": [".engineering/**", "docs/**", "tests/**", "src/**", "package.json", "pyproject.toml"],
        "acceptance": [
            {
                "name": "roadmap exists and has continuation stages",
                "command": (
                    "python3 -c \"import json; from pathlib import Path; "
                    "data=json.loads(Path('.engineering/roadmap.yaml').read_text()); "
                    "assert data.get('milestones'); "
                    "assert data.get('continuation', {}).get('stages'); "
                    "print('starter roadmap ok')\""
                ),
                "timeout_seconds": 30,
            },
            {
                "name": "roadmap remains local-only",
                "command": (
                    "python3 -c \"import json; from pathlib import Path; "
                    "data=json.loads(Path('.engineering/roadmap.yaml').read_text()); "
                    "assert data.get('goal', {}).get('safety', {}).get('allow_live_services') is False; "
                    "assert data.get('profile') == '"
                    + profile
                    + "'; "
                    "print('local safety ok')\""
                ),
                "timeout_seconds": 30,
            },
        ],
    }


def _continuation_stages(
    *,
    project_root: Path,
    project_name: str,
    project_slug: str,
    profile: str,
    goal_text: str,
    constraints: list[str],
    experience: dict[str, Any],
    blueprint_path: str | None,
    stage_count: int,
) -> list[dict[str, Any]]:
    experience_kind = str(experience["kind"])
    journey = _primary_journey(experience)
    journey_id = str(journey["id"])
    blueprint_note = f" Use `{blueprint_path}` as design context." if blueprint_path else ""
    stages = [
        {
            "id": "stage-1-local-slice",
            "title": "Stage 1 Local Slice",
            "objective": f"Deliver the first local, testable slice of {project_name}.{blueprint_note}",
            "tasks": [
                _continuation_task(
                    project_root=project_root,
                    stage_id="stage-1-local-slice",
                    task_id=f"{project_slug}-first-slice",
                    title="Implement the first locally testable goal slice",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_IMPLEMENTATION_FILE_SCOPE,
                    implementation_focus=(
                        "Implement the first small, locally testable slice of this high-level goal. "
                        "Keep the slice narrow enough for one autonomous implementation pass and include focused tests."
                    ),
                    stage_objective=f"Deliver the first local, testable slice of {project_name}.",
                )
            ],
        },
        {
            "id": "stage-2-experience-validation",
            "title": "Stage 2 Experience Validation",
            "objective": (
                f"Turn the {experience_kind} experience plan into concrete local validation for `{journey_id}`."
            ),
            "tasks": [
                _continuation_task(
                    project_root=project_root,
                    stage_id="stage-2-experience-validation",
                    task_id=f"{project_slug}-experience-validation",
                    title="Validate the primary user or operator journey",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_IMPLEMENTATION_FILE_SCOPE,
                    implementation_focus=(
                        "Add or tighten local validation for the primary experience journey. "
                        "Cover the relevant persona, main surfaces, expected output, empty/error states, and documented usage."
                    ),
                    stage_objective=(
                        f"Validate the `{journey_id}` journey for {journey.get('persona')} without external accounts."
                    ),
                )
            ],
        },
        {
            "id": "stage-3-policy-evidence-observability",
            "title": "Stage 3 Policy, Evidence, and Observability",
            "objective": (
                "Harden local policy boundaries, evidence capture, and observable run state before broader automation."
            ),
            "tasks": [
                _continuation_task(
                    project_root=project_root,
                    stage_id="stage-3-policy-evidence-observability",
                    task_id=f"{project_slug}-policy-evidence-observability",
                    title="Harden policy, evidence, and observability paths",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_HARDENING_FILE_SCOPE,
                    implementation_focus=(
                        "Harden local safety policy, evidence artifacts, logs, reports, and status visibility. "
                        "Make failures diagnosable through deterministic files or command output."
                    ),
                    stage_objective=(
                        "Ensure policy decisions, acceptance evidence, and observable state are local, reviewable, and testable."
                    ),
                )
            ],
        },
        {
            "id": "stage-4-unattended-drive-readiness",
            "title": "Stage 4 Unattended Drive Readiness",
            "objective": (
                f"Prepare `{project_name}` for bounded unattended orchestrator drive runs under the `{profile}` profile."
            ),
            "tasks": [
                _continuation_task(
                    project_root=project_root,
                    stage_id="stage-4-unattended-drive-readiness",
                    task_id=f"{project_slug}-unattended-drive-readiness",
                    title="Prepare bounded unattended drive readiness",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_HARDENING_FILE_SCOPE,
                    implementation_focus=(
                        "Make the project ready for bounded unattended local drive runs. "
                        "Review roadmap ordering, idempotent commands, timeouts, repair paths, and docs for resuming after failure."
                    ),
                    stage_objective=(
                        "Verify the backlog can continue safely without live services, credentials, deployments, or manual accounts."
                    ),
                )
            ],
        },
    ]
    return stages[:stage_count]


def _continuation_task(
    *,
    project_root: Path,
    stage_id: str,
    task_id: str,
    title: str,
    goal_text: str,
    profile: str,
    constraints: list[str],
    experience_kind: str,
    journey: dict[str, Any],
    blueprint_path: str | None,
    file_scope: list[str],
    implementation_focus: str,
    stage_objective: str,
) -> dict[str, Any]:
    gates = _quality_gates(
        project_root=project_root,
        stage_id=stage_id,
        task_id=task_id,
        profile=profile,
        experience_kind=experience_kind,
        journey=journey,
    )
    return {
        "id": task_id,
        "title": title,
        "status": "pending",
        "max_attempts": 2,
        "max_task_iterations": 2,
        "manual_approval_required": False,
        "agent_approval_required": True,
        "file_scope": list(file_scope),
        "implementation": [
            {
                "name": "codex implementation",
                "executor": "codex",
                "prompt": _implementation_prompt(
                    title=title,
                    stage_objective=stage_objective,
                    implementation_focus=implementation_focus,
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    gate_guidance=gates["prompt_guidance"],
                ),
                "timeout_seconds": 3600,
            }
        ],
        "repair": [
            {
                "name": "codex repair",
                "executor": "codex",
                "prompt": _repair_prompt(title=title),
                "timeout_seconds": 1800,
            }
        ],
        "acceptance": gates["acceptance"],
        "e2e": gates["e2e"],
        "generated_by": GOAL_ROADMAP_PLANNER_ID,
    }


def _primary_journey(experience: dict[str, Any]) -> dict[str, Any]:
    journeys = experience.get("e2e_journeys")
    if isinstance(journeys, list) and journeys:
        journey = journeys[0]
        if isinstance(journey, dict):
            return journey
    return {
        "id": "primary-local-journey",
        "persona": "local operator",
        "goal": "Run the local workflow and inspect the result.",
    }


def _quality_gates(
    *,
    project_root: Path,
    stage_id: str,
    task_id: str,
    profile: str,
    experience_kind: str,
    journey: dict[str, Any],
) -> dict[str, Any]:
    acceptance = [
        *_profile_acceptance_gates(
            profile=profile,
            experience_kind=experience_kind,
            task_id=task_id,
            journey=journey,
        ),
        *_experience_acceptance_gates(experience_kind=experience_kind, task_id=task_id, journey=journey),
        {
            "name": "roadmap task contract smoke",
            "command": _task_contract_command(stage_id=stage_id, task_id=task_id),
            "timeout_seconds": 30,
        },
    ]
    e2e = _experience_e2e_gates(
        project_root=project_root,
        profile=profile,
        experience_kind=experience_kind,
        task_id=task_id,
        journey=journey,
    )
    return {
        "acceptance": acceptance,
        "e2e": e2e,
        "prompt_guidance": _gate_prompt_guidance(acceptance=acceptance, e2e=e2e),
    }


def _profile_acceptance_gates(
    *,
    profile: str,
    experience_kind: str,
    task_id: str,
    journey: dict[str, Any],
) -> list[dict[str, Any]]:
    if profile in {"python-agent", "agent-monorepo"}:
        return [
            {
                "name": "python behavioral tests pass",
                "command": "python3 -m pytest tests -q",
                "guidance": "Create or update focused pytest coverage under `tests/` for the implemented behavior.",
                "timeout_seconds": 600,
            }
        ]
    if profile == "node-frontend":
        return [
            {
                "name": "frontend unit and integration tests pass",
                "command": "npm test",
                "guidance": "Create or update the local npm test script and focused frontend tests.",
                "timeout_seconds": 900,
            }
        ]
    journey_id = _journey_id(journey)
    return [
        {
            "name": "local documented behavior check exists",
            "command": _candidate_content_check_command(
                _journey_evidence_candidates(
                    task_id=task_id,
                    journey_id=journey_id,
                    experience_kind=experience_kind,
                ),
                [journey_id, _journey_persona(journey), "local"],
                missing_label="missing local documented behavior check",
            ),
            "guidance": "Create a local documented check or evidence file for the selected journey.",
            "timeout_seconds": 120,
        }
    ]


def _experience_acceptance_gates(
    *,
    experience_kind: str,
    task_id: str,
    journey: dict[str, Any],
) -> list[dict[str, Any]]:
    journey_id = _journey_id(journey)
    persona = _journey_persona(journey)
    if experience_kind == "cli-only":
        candidates = _cli_example_candidates(task_id=task_id, journey_id=journey_id)
        return [
            {
                "name": "documented CLI example or local command check exists",
                "command": _candidate_content_check_command(
                    candidates,
                    [journey_id, persona, "command", "output"],
                    missing_label="missing deterministic CLI example or command check",
                ),
                "guidance": "Create one deterministic CLI example or local command check that includes the journey id, "
                "persona, command, and output at one of: "
                + ", ".join(f"`{item}`" for item in candidates),
                "timeout_seconds": 120,
            }
        ]
    if experience_kind == "api-only":
        candidates = _api_example_candidates(task_id=task_id, journey_id=journey_id)
        return [
            {
                "name": "documented API example or local client check exists",
                "command": _candidate_content_check_command(
                    candidates,
                    [journey_id, persona, "request", "response"],
                    missing_label="missing deterministic API example or client check",
                ),
                "guidance": "Create one deterministic API example or local client check that includes the journey id, "
                "persona, request, and response at one of: "
                + ", ".join(f"`{item}`" for item in candidates),
                "timeout_seconds": 120,
            }
        ]
    path = f"docs/experience/{task_id}.md"
    return [
        {
            "name": f"{experience_kind} acceptance criteria are documented",
            "command": _content_check_command(
                path,
                [experience_kind, journey_id, persona, "acceptance"],
                missing_label="missing experience acceptance criteria",
            ),
            "guidance": f"Document the local acceptance criteria in `{path}`.",
            "timeout_seconds": 120,
        }
    ]


def _experience_e2e_gates(
    *,
    project_root: Path,
    profile: str,
    experience_kind: str,
    task_id: str,
    journey: dict[str, Any],
) -> list[dict[str, Any]]:
    journey_id = _journey_id(journey)
    gates: list[dict[str, Any]] = []
    if is_browser_experience_kind(experience_kind):
        gate = browser_user_experience_gate(
            project_root,
            experience={"kind": experience_kind},
            journey=journey,
        )
        gates.append(
            {
                "name": f"{journey_id} browser user-experience gate passes",
                "command": browser_user_experience_command(journey_id),
                "guidance": (
                    "Use local Playwright specs when they are already installed, or add a static HTML journey "
                    "declaration with expected routes, forms, roles, and DOM/screenshot evidence."
                ),
                "timeout_seconds": 1200,
                "user_experience_gate": gate,
            }
        )
    if profile in {"python-agent", "agent-monorepo"}:
        gates.append(
            {
                "name": f"{journey_id} local e2e tests pass",
                "command": "python3 -m pytest tests/e2e -q",
                "guidance": "Create or update deterministic pytest journey coverage under `tests/e2e/`.",
                "timeout_seconds": 900,
            }
        )
    elif profile == "node-frontend":
        gates.append(
            {
                "name": f"{journey_id} local e2e journey passes",
                "command": "npm run e2e",
                "guidance": "Create or update the local npm `e2e` script and deterministic journey tests.",
                "timeout_seconds": 1200,
            }
        )
    evidence_candidates = _journey_evidence_candidates(
        task_id=task_id,
        journey_id=journey_id,
        experience_kind=experience_kind,
    )
    gates.append(
        {
            "name": f"{journey_id} local journey evidence is captured",
            "command": _candidate_content_check_command(
                evidence_candidates,
                [journey_id, _journey_persona(journey), "evidence"],
                missing_label="missing local e2e journey evidence",
            ),
            "guidance": "Capture journey evidence or an executable check at one of: "
            + ", ".join(f"`{item}`" for item in evidence_candidates),
            "timeout_seconds": 120,
        }
    )
    return gates


def _gate_prompt_guidance(*, acceptance: list[dict[str, Any]], e2e: list[dict[str, Any]]) -> str:
    lines = ["Generated local gates to satisfy after implementation:"]
    lines.append("Acceptance:")
    for item in acceptance:
        lines.append(f"- {item['name']}: `{item['command']}`")
        if item.get("guidance"):
            lines.append(f"  {item['guidance']}")
    lines.append("E2E/journey evidence:")
    for item in e2e:
        lines.append(f"- {item['name']}: `{item['command']}`")
        if item.get("guidance"):
            lines.append(f"  {item['guidance']}")
    lines.append("Create or update the referenced tests, examples, docs, or evidence files inside file_scope.")
    return "\n".join(lines)


def _cli_example_candidates(*, task_id: str, journey_id: str) -> list[str]:
    python_slug = _python_test_slug(journey_id)
    return [
        f"examples/{task_id}-cli.md",
        f"docs/examples/{task_id}-cli.md",
        f"tests/cli/test_{python_slug}.py",
        f"tests/e2e/test_{python_slug}.py",
    ]


def _api_example_candidates(*, task_id: str, journey_id: str) -> list[str]:
    python_slug = _python_test_slug(journey_id)
    return [
        f"examples/{task_id}-api.md",
        f"docs/examples/{task_id}-api.md",
        f"tests/api/test_{python_slug}.py",
        f"tests/e2e/test_{python_slug}.py",
    ]


def _journey_evidence_candidates(*, task_id: str, journey_id: str, experience_kind: str) -> list[str]:
    journey_slug = _slugify(journey_id)
    python_slug = _python_test_slug(journey_id)
    candidates = [
        f"docs/e2e/{task_id}-{journey_slug}.md",
        f"docs/evidence/{task_id}-{journey_slug}.md",
        f"tests/e2e/test_{python_slug}.py",
    ]
    if experience_kind in {"dashboard", "submission-review", "multi-role-app"}:
        candidates.extend(
            [
                f"tests/e2e/{journey_slug}.spec.ts",
                f"tests/e2e/{journey_slug}.spec.js",
                f"e2e/{journey_slug}.spec.ts",
            ]
        )
    if experience_kind == "cli-only":
        candidates.append(f"examples/{task_id}-cli.md")
    if experience_kind == "api-only":
        candidates.append(f"examples/{task_id}-api.md")
    return candidates


def _journey_id(journey: dict[str, Any]) -> str:
    return str(journey.get("id") or "primary-local-journey")


def _journey_persona(journey: dict[str, Any]) -> str:
    return str(journey.get("persona") or "local operator")


def _task_contract_command(*, stage_id: str, task_id: str) -> str:
    return _python_command(
        "import json; from pathlib import Path;",
        "data=json.loads(Path('.engineering/roadmap.yaml').read_text());",
        f"stage=next(item for item in data.get('continuation', {{}}).get('stages', []) if item.get('id') == '{stage_id}');",
        f"task=next(item for item in stage.get('tasks', []) if item.get('id') == '{task_id}');",
        "assert task.get('file_scope');",
        "assert task.get('implementation') and task['implementation'][0].get('executor') == 'codex';",
        "assert task.get('repair') and task['repair'][0].get('executor') == 'codex';",
        "assert task.get('acceptance');",
        f"print('{task_id} contract ok')",
    )


def _content_check_command(path: str, required_terms: list[str], *, missing_label: str) -> str:
    encoded_terms = _b64_json([term for term in dict.fromkeys(required_terms) if str(term).strip()])
    return (
        "python3 -c \"import base64,json; from pathlib import Path; "
        f"p=Path('{path}'); assert p.exists(), 'missing {path}'; "
        "text=p.read_text(encoding='utf-8', errors='ignore').lower(); "
        f"terms=json.loads(base64.b64decode('{encoded_terms}')); "
        "missing=[term for term in terms if str(term).lower() not in text]; "
        f"assert not missing, '{missing_label}: ' + ', '.join(missing); "
        f"print('{path} content ok')\""
    )


def _candidate_content_check_command(
    candidates: list[str],
    required_terms: list[str],
    *,
    missing_label: str,
) -> str:
    encoded_candidates = _b64_json(candidates)
    encoded_terms = _b64_json([term for term in dict.fromkeys(required_terms) if str(term).strip()])
    return (
        "python3 -c \"import base64,json; from pathlib import Path; "
        f"candidates=json.loads(base64.b64decode('{encoded_candidates}')); "
        "paths=[Path(item) for item in candidates if Path(item).exists()]; "
        f"assert paths, '{missing_label}; expected one of: ' + ', '.join(candidates); "
        "text='\\n'.join(path.read_text(encoding='utf-8', errors='ignore').lower() for path in paths); "
        f"terms=json.loads(base64.b64decode('{encoded_terms}')); "
        "missing=[term for term in terms if str(term).lower() not in text]; "
        f"assert not missing, '{missing_label} terms: ' + ', '.join(missing); "
        "print('local journey content ok')\""
    )


def _b64_json(value: Any) -> str:
    return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def _python_test_slug(value: str) -> str:
    return _slugify(value).replace("-", "_")


def _python_command(*statements: str) -> str:
    return 'python3 -c "' + " ".join(statements) + '"'


def _implementation_prompt(
    *,
    title: str,
    stage_objective: str,
    implementation_focus: str,
    goal_text: str,
    profile: str,
    constraints: list[str],
    experience_kind: str,
    journey: dict[str, Any],
    blueprint_path: str | None,
    gate_guidance: str,
) -> str:
    blueprint = f"\nBlueprint: `{blueprint_path}`." if blueprint_path else ""
    return (
        f"{implementation_focus}\n\n"
        f"Task: {title}.\n"
        f"Stage objective: {stage_objective}\n"
        f"Goal: {goal_text}.{blueprint}\n"
        f"Profile: {profile}.\n"
        f"Experience kind: {experience_kind}.\n"
        f"Primary E2E journey: {journey.get('id')} for {journey.get('persona')} - {journey.get('goal')}\n"
        f"{_constraints_prompt(constraints)}\n"
        f"{gate_guidance}\n"
        "Keep the work deterministic and local. Add or update focused tests and docs. Do not use private keys, "
        "paid live services, production deployments, mainnet writes, real-fund movement, or live trading."
    )


def _repair_prompt(*, title: str) -> str:
    return (
        f"Repair the generated task `{title}` using the failing implementation, acceptance, or e2e evidence. "
        "Make the smallest local fix inside file_scope, keep checks deterministic, and update tests or docs when behavior changes. "
        "Do not add live deployments, paid services, private keys, mainnet writes, real-fund movement, or live trading."
    )


def _constraints_prompt(constraints: list[str]) -> str:
    if not constraints:
        return "Additional constraints: none."
    rendered = "; ".join(constraints)
    return f"Additional constraints: {rendered}."


def _normalize_stage_count(stage_count: int | None) -> int:
    if stage_count is None:
        return DEFAULT_GOAL_STAGE_COUNT
    try:
        value = int(stage_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("stage_count must be an integer") from exc
    if value < MIN_GOAL_STAGE_COUNT or value > MAX_GOAL_STAGE_COUNT:
        raise ValueError(
            f"stage_count must be between {MIN_GOAL_STAGE_COUNT} and {MAX_GOAL_STAGE_COUNT}"
        )
    return value


def _self_iteration_guidance(
    *,
    project_root: Path,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | None,
) -> dict[str, Any]:
    enabled = _self_iteration_is_appropriate(profile=profile, goal_text=goal_text)
    return {
        "enabled": enabled,
        "objective": f"Assess current state and append the next safe, testable stage for {project_name}.",
        "max_stages_per_iteration": 1,
        "file_scope": [".engineering/roadmap.yaml", "docs/**", "src/**", "app/**", "tests/**", "examples/**"],
        "guidance": [
            "Append new work to continuation.stages; do not mark tasks complete.",
            "Each generated task must include implementation, repair, acceptance, and e2e gates when user-visible behavior is affected.",
            "Keep all checks local and deterministic; do not require private credentials, paid live services, deployments, or live trading.",
        ],
        "planner": {
            "name": "Codex self-iteration planner",
            "executor": "codex",
            "timeout_seconds": 3600,
            "sandbox": "workspace-write",
            "prompt": _self_iteration_prompt(goal_text=goal_text, blueprint_path=blueprint_path, project_root=project_root),
        },
    }


def _self_iteration_prompt(*, goal_text: str, blueprint_path: str | None, project_root: Path) -> str:
    blueprint = f" Use the blueprint at `{blueprint_path}` when it exists." if blueprint_path else ""
    return (
        f"Project root: {project_root}. Goal: {goal_text}.{blueprint} "
        "Append exactly one continuation stage with concrete, local acceptance and e2e gates. "
        "Do not call paid APIs, require accounts, deploy services, move funds, or use private keys."
    )


def _experience_plan(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | None,
    explicit_kind: str | None,
) -> dict[str, Any]:
    plan = build_domain_frontend_plan(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        explicit_kind=explicit_kind,
        source="derived",
        explicit_source="goal-intake",
    )
    if explicit_kind:
        plan["provided_by"] = "goal-intake"
    else:
        plan["derived_by"] = GOAL_ROADMAP_PLANNER_ID
        plan["domain_frontend_generated_by"] = DOMAIN_FRONTEND_GENERATOR_ID
    plan["derivation_rationale"] = list(plan.get("rationale", []))
    return plan


def _derive_experience_kind(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | None,
) -> tuple[str, list[str]]:
    decision = derive_domain_frontend_decision(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
    )
    return str(decision.get("experience_kind", "dashboard")), list(decision.get("rationale", []))


def _self_iteration_is_appropriate(*, profile: str, goal_text: str) -> bool:
    if profile in {"agent-monorepo", "python-agent", "trading-research", "lean-formalization"}:
        return True
    return bool(_keyword_matches(goal_text.lower(), _SELF_ITERATION_KEYWORDS))


def _keyword_matches(text: str, keywords: tuple[str, ...]) -> list[str]:
    return domain_frontend_keyword_matches(text, keywords)


def _rationale(*, profile: str, decision: str, matches: list[str]) -> list[str]:
    rationale = [decision]
    if profile:
        rationale.append(f"profile: {profile}")
    if matches:
        rationale.append("matched hints: " + ", ".join(matches[:6]))
    return rationale

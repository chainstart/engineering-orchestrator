from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


DOMAIN_FRONTEND_PLAN_SCHEMA_VERSION = 1
DOMAIN_FRONTEND_DECISION_KIND = "engineering-harness.domain-frontend-decision.v1"
DOMAIN_FRONTEND_GENERATOR_ID = "engineering-harness-domain-frontend-generator"

EXPERIENCE_KINDS = {
    "app-specific",
    "api-only",
    "cli-only",
    "dashboard",
    "multi-role-app",
    "submission-review",
}

DEFAULT_EXPERIENCE_PLANS: dict[str, dict[str, Any]] = {
    "dashboard": {
        "kind": "dashboard",
        "personas": ["operator"],
        "primary_surfaces": ["operator dashboard", "run queue", "artifact viewer"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "operator-observes-run",
                "persona": "operator",
                "goal": "Inspect queued work, follow run status, and review latest artifacts or errors.",
            }
        ],
    },
    "submission-review": {
        "kind": "submission-review",
        "personas": ["student", "reviewer"],
        "primary_surfaces": [
            "submission portal",
            "review console",
            "returned work view",
            "revision upload",
            "status timeline",
        ],
        "auth": {"required": True, "roles": ["student", "reviewer"]},
        "e2e_journeys": [
            {
                "id": "student-submit-review-return",
                "persona": "student",
                "goal": "Submit work, receive reviewer comments, inspect a returned decision, and upload a revision.",
            }
        ],
    },
    "multi-role-app": {
        "kind": "multi-role-app",
        "personas": ["admin", "operator", "approver"],
        "primary_surfaces": [
            "account setup",
            "login",
            "role assignment",
            "operator queue",
            "approval screen",
            "access denied state",
            "audit log",
        ],
        "auth": {"required": True, "roles": ["admin", "operator", "approver"]},
        "e2e_journeys": [
            {
                "id": "operator-requests-role-approval",
                "persona": "operator",
                "goal": "Sign in with an assigned role, create a work item, request approval, and verify denied access plus audit history.",
            }
        ],
    },
    "app-specific": {
        "kind": "app-specific",
        "personas": ["user"],
        "primary_surfaces": [
            "primary app workspace",
            "create or edit flow",
            "detail view",
            "empty state",
            "error state",
        ],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "user-completes-primary-workflow",
                "persona": "user",
                "goal": "Open the app-specific workspace, complete the primary workflow, and verify the resulting state.",
            }
        ],
    },
    "api-only": {
        "kind": "api-only",
        "personas": ["api client"],
        "primary_surfaces": ["API docs", "OpenAPI schema", "example client journey", "service status"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "client-runs-api-example",
                "persona": "api client",
                "goal": "Run the documented API example and verify the expected response.",
            }
        ],
    },
    "cli-only": {
        "kind": "cli-only",
        "personas": ["developer"],
        "primary_surfaces": ["command line", "documented examples", "generated reports"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "developer-runs-cli",
                "persona": "developer",
                "goal": "Run the documented command and inspect the generated output or report.",
            }
        ],
    },
}

EXPERIENCE_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "submission-review": (
        "submission-review",
        "submission review",
        "student review",
        "student paper review",
        "paper review",
        "review workflow",
        "return workflow",
    ),
    "multi-role-app": (
        "multi-role-app",
        "multi-role",
        "multi role",
        "role-specific",
        "role based",
        "account roles",
    ),
    "app-specific": (
        "app-specific",
        "app specific",
        "ordinary software",
        "product app",
        "application views",
    ),
    "api-only": ("api-only", "api only", "api-first", "api first", "rest api", "openapi", "api"),
    "cli-only": ("cli-only", "cli only", "command line", "command-line", "cli"),
    "dashboard": (
        "dashboard",
        "dashboard-only",
        "operator console",
        "run queue",
    ),
}

EXPERIENCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "submission-review": (
        "submission",
        "submit",
        "student",
        "reviewer",
        "review",
        "revision",
        "return",
        "returned",
        "paper",
        "assignment",
        "rubric",
        "comments",
        "decision",
        "grade",
    ),
    "multi-role-app": (
        "account",
        "accounts",
        "role",
        "roles",
        "rbac",
        "permission",
        "permissions",
        "admin",
        "operator",
        "approver",
        "approval",
        "audit",
        "login",
        "auth",
        "authenticated",
    ),
    "app-specific": (
        "app",
        "application",
        "software",
        "workspace",
        "view",
        "views",
        "editor",
        "tracker",
        "manager",
        "portal",
        "catalog",
        "calendar",
        "kanban",
        "form",
        "forms",
        "profile",
        "settings",
    ),
    "api-only": (
        "api",
        "rest",
        "openapi",
        "swagger",
        "endpoint",
        "endpoints",
        "graphql",
        "client",
        "http",
        "curl",
        "sdk",
    ),
    "cli-only": (
        "cli",
        "command-line",
        "command line",
        "terminal",
        "argparse",
        "typer",
        "click",
        "subcommand",
        "documented command",
    ),
    "dashboard": (
        "dashboard",
        "autonomous",
        "agent",
        "worker",
        "research",
        "run queue",
        "status",
        "artifact",
        "artifacts",
        "theorem",
        "proof",
        "prover",
        "lean",
        "formalization",
        "backtest",
        "monitor",
        "observability",
    ),
}

DOMAIN_FRONTEND_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "autonomous-theorem-prover-dashboard",
        "domain": "autonomous-theorem-prover",
        "experience_kind": "dashboard",
        "surface_policy": "dashboard-only",
        "priority": 100,
        "threshold": 1,
        "profiles": ("lean-formalization",),
        "project_kinds": ("lean", "formalization"),
        "keywords": (
            "theorem",
            "proof",
            "prover",
            "proof search",
            "formalization",
            "lean",
            "coq",
            "isabelle",
            "tactic",
        ),
        "plan_overrides": {
            "primary_surfaces": [
                "operator dashboard",
                "proof attempt queue",
                "proof detail view",
                "artifact viewer",
            ],
            "e2e_journeys": [
                {
                    "id": "operator-reviews-proof-attempt",
                    "persona": "operator",
                    "goal": "Inspect proof attempts, follow theorem proving status, and review generated proof artifacts.",
                }
            ],
        },
    },
    {
        "id": "student-paper-review-return-workflow",
        "domain": "student-paper-review",
        "experience_kind": "submission-review",
        "surface_policy": "submission-review-return",
        "priority": 90,
        "threshold": 2,
        "keywords": EXPERIENCE_KEYWORDS["submission-review"],
    },
    {
        "id": "account-role-boundary-workflow",
        "domain": "multi-role-system",
        "experience_kind": "multi-role-app",
        "surface_policy": "account-role-flows",
        "priority": 80,
        "threshold": 2,
        "keywords": EXPERIENCE_KEYWORDS["multi-role-app"],
    },
    {
        "id": "api-client-contract",
        "domain": "api-service",
        "experience_kind": "api-only",
        "surface_policy": "api-first",
        "priority": 60,
        "threshold": 2,
        "project_kinds": ("api",),
        "keywords": EXPERIENCE_KEYWORDS["api-only"],
    },
    {
        "id": "cli-command-contract",
        "domain": "cli-tool",
        "experience_kind": "cli-only",
        "surface_policy": "cli-first",
        "priority": 60,
        "threshold": 2,
        "project_kinds": ("cli",),
        "keywords": EXPERIENCE_KEYWORDS["cli-only"],
    },
    {
        "id": "autonomous-operator-dashboard",
        "domain": "autonomous-operator-system",
        "experience_kind": "dashboard",
        "surface_policy": "dashboard-only",
        "priority": 50,
        "threshold": 1,
        "profiles": ("trading-research", "evm-security-research"),
        "keywords": (
            "autonomous",
            "agent",
            "worker",
            "research",
            "backtest",
            "monitor",
            "observability",
            "artifact",
            "artifacts",
            "run queue",
        ),
    },
    {
        "id": "operator-dashboard",
        "domain": "operator-dashboard",
        "experience_kind": "dashboard",
        "surface_policy": "dashboard-only",
        "priority": 40,
        "threshold": 1,
        "keywords": (
            "dashboard",
            "operator console",
            "run queue",
            "status",
            "artifact",
            "artifacts",
            "monitor",
            "observability",
        ),
    },
    {
        "id": "ordinary-software-app-views",
        "domain": "ordinary-software",
        "experience_kind": "app-specific",
        "surface_policy": "app-specific-views",
        "priority": 20,
        "threshold": 2,
        "profiles": ("node-frontend",),
        "project_kinds": ("node", "web", "frontend"),
        "keywords": EXPERIENCE_KEYWORDS["app-specific"],
    },
)


def build_domain_frontend_plan(
    *,
    project_name: str,
    profile: str,
    goal_text: str = "",
    project_kind: str = "",
    blueprint_path: str | None = None,
    explicit_kind: str | None = None,
    hint_values: list[str] | tuple[str, ...] | None = None,
    source: str = "derived",
    explicit_source: str = "explicit",
) -> dict[str, Any]:
    if explicit_kind:
        kind = str(explicit_kind).strip()
        decision = _explicit_decision(
            kind=kind,
            project_name=project_name,
            profile=profile,
            project_kind=project_kind,
            goal_text=goal_text,
            blueprint_path=blueprint_path,
            hint_values=hint_values,
        )
        plan = _plan_from_decision(decision)
        _annotate_plan(plan, decision=decision, source=explicit_source, derived=False)
        return plan

    decision = derive_domain_frontend_decision(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        project_kind=project_kind,
        blueprint_path=blueprint_path,
        hint_values=hint_values,
    )
    plan = _plan_from_decision(decision)
    _annotate_plan(plan, decision=decision, source=source, derived=True)
    return plan


def annotate_explicit_domain_frontend_plan(
    experience: dict[str, Any],
    *,
    project_name: str,
    profile: str,
    goal_text: str = "",
    project_kind: str = "",
    blueprint_path: str | None = None,
    hint_values: list[str] | tuple[str, ...] | None = None,
    source: str = "explicit",
) -> dict[str, Any]:
    plan = deepcopy(experience)
    kind = str(plan.get("kind", "")).strip()
    if kind not in EXPERIENCE_KINDS:
        plan["source"] = source
        plan["derived"] = False
        plan["recommendation"] = kind or None
        plan["required"] = True
        plan["frontend_required"] = True
        plan["rationale"] = ["roadmap declares an explicit experience block"]
        return plan

    defaults = deepcopy(DEFAULT_EXPERIENCE_PLANS[kind])
    for key in ("personas", "primary_surfaces", "auth", "e2e_journeys"):
        plan.setdefault(key, defaults[key])
    existing_decision = plan.get("decision_contract")
    if (
        isinstance(existing_decision, dict)
        and str(existing_decision.get("experience_kind", "")).strip() == kind
    ):
        decision = deepcopy(existing_decision)
    else:
        decision = _explicit_decision(
            kind=kind,
            project_name=project_name,
            profile=profile,
            project_kind=project_kind,
            goal_text=goal_text,
            blueprint_path=blueprint_path,
            hint_values=hint_values,
        )
    _annotate_plan(plan, decision=decision, source=source, derived=False)
    return plan


def derive_domain_frontend_decision(
    *,
    project_name: str,
    profile: str,
    goal_text: str = "",
    project_kind: str = "",
    blueprint_path: str | None = None,
    hint_values: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    normalized_profile = _normalize(profile)
    normalized_project_kind = _normalize(project_kind)
    hint_text = _hint_text(
        project_name=project_name,
        profile=normalized_profile,
        project_kind=normalized_project_kind,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        hint_values=hint_values,
    )

    alias_priority = ("submission-review", "multi-role-app", "api-only", "cli-only")
    for kind in alias_priority:
        matches = keyword_matches(hint_text, EXPERIENCE_KIND_ALIASES[kind])
        if matches:
            rule = _default_rule_for_kind(kind)
            return _decision_payload(
                rule=rule,
                project_name=project_name,
                profile=normalized_profile,
                project_kind=normalized_project_kind,
                goal_text=goal_text,
                blueprint_path=blueprint_path,
                matches=matches,
                decision=f"matched {kind} frontend alias",
            )

    scored: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for rule in DOMAIN_FRONTEND_RULES:
        matches = keyword_matches(hint_text, tuple(rule.get("keywords", ())))
        score = len(matches)
        if normalized_profile and normalized_profile in set(rule.get("profiles", ())):
            score += 2
            matches.append(f"profile:{normalized_profile}")
        if normalized_project_kind and normalized_project_kind in set(rule.get("project_kinds", ())):
            score += 2
            matches.append(f"project_kind:{normalized_project_kind}")
        if score >= int(rule.get("threshold", 1)):
            scored.append((score, int(rule.get("priority", 0)), rule, matches))

    if scored:
        score, _priority, rule, matches = max(scored, key=lambda item: (item[0], item[1]))
        return _decision_payload(
            rule=rule,
            project_name=project_name,
            profile=normalized_profile,
            project_kind=normalized_project_kind,
            goal_text=goal_text,
            blueprint_path=blueprint_path,
            matches=matches,
            decision=f"matched {rule['domain']} frontend signals",
            score=score,
        )

    fallback_kind = "dashboard"
    fallback_rule = _first_rule("autonomous-operator-dashboard")
    fallback_decision = "defaulted to dashboard for agent or research profile"
    if normalized_profile == "lean-formalization":
        fallback_rule = _first_rule("autonomous-theorem-prover-dashboard")
        fallback_decision = "defaulted lean formalization work to dashboard-only proof oversight"
    elif normalized_profile == "node-frontend" or normalized_project_kind in {"node", "web", "frontend"}:
        fallback_kind = "app-specific"
        fallback_rule = _first_rule("ordinary-software-app-views")
        fallback_decision = "defaulted ordinary frontend software to app-specific views"
    elif normalized_profile not in {
        "agent-monorepo",
        "python-agent",
        "trading-research",
        "evm-security-research",
    }:
        fallback_kind = "app-specific"
        fallback_rule = _first_rule("ordinary-software-app-views")
        fallback_decision = "defaulted ordinary software to app-specific views"

    if fallback_rule["experience_kind"] != fallback_kind:
        fallback_rule = _default_rule_for_kind(fallback_kind)
    return _decision_payload(
        rule=fallback_rule,
        project_name=project_name,
        profile=normalized_profile,
        project_kind=normalized_project_kind,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        matches=[],
        decision=fallback_decision,
    )


def keyword_matches(text: str, keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for keyword in keywords:
        expression = re.escape(keyword.lower()).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9]){expression}(?![a-z0-9])", text):
            matches.append(keyword)
    return matches


def _explicit_decision(
    *,
    kind: str,
    project_name: str,
    profile: str,
    project_kind: str,
    goal_text: str,
    blueprint_path: str | None,
    hint_values: list[str] | tuple[str, ...] | None,
) -> dict[str, Any]:
    rule = _default_rule_for_kind(kind)
    return _decision_payload(
        rule=rule,
        project_name=project_name,
        profile=_normalize(profile),
        project_kind=_normalize(project_kind),
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        matches=[],
        decision=f"explicit experience kind: {kind}",
        explicit_kind=kind,
        hint_values=hint_values,
    )


def _decision_payload(
    *,
    rule: dict[str, Any],
    project_name: str,
    profile: str,
    project_kind: str,
    goal_text: str,
    blueprint_path: str | None,
    matches: list[str],
    decision: str,
    score: int | None = None,
    explicit_kind: str | None = None,
    hint_values: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    experience_kind = str(explicit_kind or rule["experience_kind"])
    rationale = [decision]
    if profile:
        rationale.append(f"profile: {profile}")
    if project_kind:
        rationale.append(f"project kind: {project_kind}")
    if matches:
        rationale.append("matched hints: " + ", ".join(dict.fromkeys(matches[:8])))
    return {
        "schema_version": DOMAIN_FRONTEND_PLAN_SCHEMA_VERSION,
        "kind": DOMAIN_FRONTEND_DECISION_KIND,
        "status": "required",
        "generated_by": DOMAIN_FRONTEND_GENERATOR_ID,
        "rule_id": rule.get("id"),
        "domain": rule.get("domain"),
        "experience_kind": experience_kind,
        "surface_policy": rule.get("surface_policy", "app-specific-views"),
        "score": score if score is not None else len(matches),
        "rationale": rationale,
        "matched_hints": list(dict.fromkeys(matches)),
        "inputs": {
            "project_name": project_name,
            "profile": profile,
            "project_kind": project_kind,
            "goal_present": bool(str(goal_text).strip()),
            "blueprint_path": blueprint_path,
            "hint_count": len(hint_values or ()),
            "explicit_kind": explicit_kind,
        },
        "local_only": True,
        "constraints": [
            "required frontend experience plan",
            "local deterministic artifacts only",
            "no external accounts or paid services required",
            "use existing project conventions",
        ],
    }


def _plan_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    kind = str(decision["experience_kind"])
    plan = deepcopy(DEFAULT_EXPERIENCE_PLANS[kind])
    rule = _first_rule(str(decision.get("rule_id") or "")) or _default_rule_for_kind(kind)
    overrides = rule.get("plan_overrides") if isinstance(rule.get("plan_overrides"), dict) else {}
    for key, value in overrides.items():
        plan[key] = deepcopy(value)
    return plan


def _annotate_plan(
    plan: dict[str, Any],
    *,
    decision: dict[str, Any],
    source: str,
    derived: bool,
) -> None:
    plan["schema_version"] = DOMAIN_FRONTEND_PLAN_SCHEMA_VERSION
    plan["source"] = source
    plan["derived"] = derived
    plan["required"] = True
    plan["frontend_required"] = True
    plan["recommendation"] = decision.get("experience_kind")
    plan["domain"] = decision.get("domain")
    plan["surface_policy"] = decision.get("surface_policy")
    plan["rationale"] = list(decision.get("rationale", []))
    plan["decision_contract"] = deepcopy(decision)
    if derived:
        plan["derived_by"] = DOMAIN_FRONTEND_GENERATOR_ID


def _hint_text(
    *,
    project_name: str,
    profile: str,
    project_kind: str,
    goal_text: str,
    blueprint_path: str | None,
    hint_values: list[str] | tuple[str, ...] | None,
) -> str:
    values = [project_name, profile, project_kind, goal_text, str(blueprint_path or "")]
    values.extend(str(item) for item in (hint_values or ()) if str(item).strip())
    return " ".join(item for item in values if str(item).strip()).lower()


def _first_rule(rule_id: str) -> dict[str, Any]:
    for rule in DOMAIN_FRONTEND_RULES:
        if rule.get("id") == rule_id:
            return rule
    return DOMAIN_FRONTEND_RULES[-1]


def _default_rule_for_kind(kind: str) -> dict[str, Any]:
    default_rule_ids = {
        "app-specific": "ordinary-software-app-views",
        "api-only": "api-client-contract",
        "cli-only": "cli-command-contract",
        "dashboard": "operator-dashboard",
        "multi-role-app": "account-role-boundary-workflow",
        "submission-review": "student-paper-review-return-workflow",
    }
    return _first_rule(default_rule_ids.get(kind, "ordinary-software-app-views"))


def _normalize(value: str) -> str:
    return str(value or "").strip().lower()

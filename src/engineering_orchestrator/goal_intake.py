from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .profiles import PROFILES


GOAL_INTAKE_SCHEMA_VERSION = 1
GOAL_INTAKE_KIND = "engineering-harness.goal-intake.v1"
SUPPORTED_EXPERIENCE_KINDS = {
    "app-specific",
    "api-only",
    "cli-only",
    "dashboard",
    "multi-role-app",
    "submission-review",
}
LOCAL_ONLY_SAFETY_RULES = [
    "Goals must be implementable without private credentials.",
    "Goals must not require production deployment, mainnet writes, live trading, or real-fund movement.",
    "Blueprints must be local paths, not remote URLs.",
]

_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_PROJECT_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NEGATED_REQUIREMENT_RE = re.compile(
    r"(?:\bno\b|\bnot\b|\bnever\b|\bwithout\b|\bavoid\b|\bdo not\b|\bmust not\b|\bshould not\b)"
    r"\W+(?:\w+\W+){0,3}$"
)

_UNSAFE_LIVE_SERVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("production deployment", re.compile(r"\bdeploy(?:ment)?\s+(?:to\s+)?(?:production|prod|mainnet)\b")),
    ("production deployment", re.compile(r"\b(?:production|prod|mainnet)\s+(?:deploy|deployment|release)\b")),
    ("mainnet write", re.compile(r"\b(?:cast\s+send|deploy:mainnet|--broadcast)\b")),
    ("live trading", re.compile(r"\b(?:live|real[- ]?money|real[- ]?funds?)\s+trading\b")),
    ("live trading", re.compile(r"\b(?:place|submit|send|execute)\s+(?:real|live)\s+(?:orders?|trades?)\b")),
    ("real-fund movement", re.compile(r"\b(?:withdraw|transfer|move)\s+real\s+(?:funds|money)\b")),
    ("real-fund movement", re.compile(r"\breal[- ]?fund\s+(?:transfer|withdrawal|payment)\b")),
    (
        "private credential use",
        re.compile(r"\b(?:use|configure|require|load|import)\s+(?:a\s+)?(?:private key|mnemonic|seed phrase|api key)\b"),
    ),
    ("production service mutation", re.compile(r"\b(?:call|write to|mutate)\s+(?:the\s+)?(?:production|live)\s+(?:api|service|database)\b")),
    ("paid live service", re.compile(r"\bpaid\s+(?:live|production)\s+(?:deployment|service|hosting)\b")),
)


class GoalIntakeValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def normalize_goal_intake(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None = None,
    constraints: list[str] | tuple[str, ...] | None = None,
    desired_experience_kind: str | None = None,
) -> dict[str, Any]:
    """Return the deterministic local goal-intake contract or raise validation errors."""

    contract, errors, _blocked_requirements = _build_goal_intake_contract(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        constraints=constraints,
        desired_experience_kind=desired_experience_kind,
    )
    if errors:
        raise GoalIntakeValidationError(errors)
    if contract is None:
        raise GoalIntakeValidationError(["goal intake contract could not be normalized"])
    return contract


def validate_goal_intake(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None = None,
    constraints: list[str] | tuple[str, ...] | None = None,
    desired_experience_kind: str | None = None,
) -> dict[str, Any]:
    """Validate the local goal-intake inputs without touching external services."""

    contract, errors, blocked_requirements = _build_goal_intake_contract(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        constraints=constraints,
        desired_experience_kind=desired_experience_kind,
    )
    return {
        "schema_version": GOAL_INTAKE_SCHEMA_VERSION,
        "kind": "engineering-harness.goal-intake.validation.v1",
        "status": "passed" if not errors else "failed",
        "error_count": len(errors),
        "errors": errors,
        "blocked_requirements": blocked_requirements,
        "goal_intake": contract if not errors else None,
    }


def _build_goal_intake_contract(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None,
    constraints: list[str] | tuple[str, ...] | None,
    desired_experience_kind: str | None,
) -> tuple[dict[str, Any] | None, list[str], list[dict[str, str]]]:
    errors: list[str] = []

    normalized_project_name = _collapse_whitespace(project_name)
    if not normalized_project_name:
        errors.append("`project_name` is required")
    project_slug = _project_slug(normalized_project_name)
    if normalized_project_name and not project_slug:
        errors.append("`project_name` must contain at least one letter or number")

    profile_id = _collapse_whitespace(profile).lower()
    if not profile_id:
        errors.append("`profile` is required")
    elif profile_id not in PROFILES:
        allowed = ", ".join(sorted(PROFILES))
        errors.append(f"profile `{profile_id}` is not supported; expected one of: {allowed}")

    normalized_goal_text = _collapse_whitespace(goal_text)
    if not normalized_goal_text:
        errors.append("`goal_text` is required")

    normalized_blueprint_path = _normalize_blueprint_path(blueprint_path, errors=errors)
    normalized_constraints = _normalize_constraints(constraints, errors=errors)
    experience_kind = _normalize_experience_kind(desired_experience_kind, errors=errors)

    scan_items = [("goal_text", normalized_goal_text)]
    scan_items.extend((f"constraints[{index}]", constraint) for index, constraint in enumerate(normalized_constraints))
    blocked_requirements = _detect_unsafe_live_service_requirements(scan_items)
    for item in blocked_requirements:
        errors.append(
            f"{item['field']} contains unsafe live-service requirement `{item['match']}` ({item['reason']})"
        )

    if errors:
        return None, errors, blocked_requirements

    contract = {
        "schema_version": GOAL_INTAKE_SCHEMA_VERSION,
        "kind": GOAL_INTAKE_KIND,
        "project": {
            "name": normalized_project_name,
            "slug": project_slug,
            "profile": profile_id,
        },
        "goal": {
            "text": normalized_goal_text,
        },
        "blueprint": {
            "path": normalized_blueprint_path,
            "provided": normalized_blueprint_path is not None,
        },
        "constraints": normalized_constraints,
        "experience": {
            "kind": experience_kind,
            "provided": experience_kind is not None,
        },
        "safety": {
            "mode": "local-only",
            "allow_live_services": False,
            "blocked_requirements": [],
            "rules": list(LOCAL_ONLY_SAFETY_RULES),
        },
        "roadmap_seed": {
            "project": normalized_project_name,
            "profile": profile_id,
            "goal": normalized_goal_text,
            "blueprint_path": normalized_blueprint_path,
            "constraints": normalized_constraints,
            "experience_kind": experience_kind,
        },
    }
    return contract, [], []


def _collapse_whitespace(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_RE.sub(" ", value.strip())


def _project_slug(project_name: str) -> str:
    slug = _PROJECT_SLUG_RE.sub("-", project_name.lower()).strip("-")
    return slug


def _normalize_blueprint_path(value: str | Path | None, *, errors: list[str]) -> str | None:
    if value is None:
        return None
    text = _collapse_whitespace(str(value))
    if not text:
        errors.append("`blueprint_path` must be a non-empty local path when provided")
        return None
    if _URL_RE.match(text):
        errors.append("`blueprint_path` must be a local path, not a URL")
        return None
    return text


def _normalize_constraints(value: list[str] | tuple[str, ...] | None, *, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        errors.append("`constraints` must be a list of strings")
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _collapse_whitespace(item)
        if not text:
            errors.append(f"constraints[{index}] must be a non-empty string")
            continue
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _normalize_experience_kind(value: str | None, *, errors: list[str]) -> str | None:
    if value is None:
        return None
    raw = _collapse_whitespace(value).lower()
    if not raw:
        errors.append("`desired_experience_kind` must be non-empty when provided")
        return None
    normalized = raw.replace("_", "-").replace(" ", "-")
    aliases = {
        "api": "api-only",
        "api-first": "api-only",
        "app": "app-specific",
        "app-specific-views": "app-specific",
        "application": "app-specific",
        "cli": "cli-only",
        "cli-first": "cli-only",
        "multi-role": "multi-role-app",
        "multi-role-application": "multi-role-app",
        "submission-review-workflow": "submission-review",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_EXPERIENCE_KINDS:
        allowed = ", ".join(sorted(SUPPORTED_EXPERIENCE_KINDS))
        errors.append(f"desired_experience_kind `{raw}` is not supported; expected one of: {allowed}")
        return None
    return normalized


def _detect_unsafe_live_service_requirements(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    blocked: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for field, text in items:
        lowered = text.lower()
        for reason, pattern in _UNSAFE_LIVE_SERVICE_PATTERNS:
            for match in pattern.finditer(lowered):
                matched_text = text[match.start() : match.end()]
                if _is_negated_requirement(lowered, match.start()):
                    continue
                key = (field, reason, matched_text.lower())
                if key in seen:
                    continue
                seen.add(key)
                blocked.append({"field": field, "match": matched_text, "reason": reason})
    return blocked


def _is_negated_requirement(text: str, match_start: int) -> bool:
    prefix = text[max(0, match_start - 48) : match_start]
    return bool(_NEGATED_REQUIREMENT_RE.search(prefix))

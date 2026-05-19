from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .executors import (
    EXECUTOR_RESULT_CONTRACT_VERSION,
    EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
    EXECUTOR_WATCHDOG_CONTRACT_VERSION,
    ExecutorInvocation,
    ExecutorRegistry,
    ExecutorTaskCommand,
    ExecutorTaskContext,
    classify_capabilities,
    default_executor_registry,
)
from .domain_frontend import (
    DOMAIN_FRONTEND_DECISION_KIND,
    DOMAIN_FRONTEND_GENERATOR_ID,
    DOMAIN_FRONTEND_PLAN_SCHEMA_VERSION,
    DEFAULT_EXPERIENCE_PLANS as DOMAIN_DEFAULT_EXPERIENCE_PLANS,
    EXPERIENCE_KEYWORDS as DOMAIN_EXPERIENCE_KEYWORDS,
    EXPERIENCE_KIND_ALIASES as DOMAIN_EXPERIENCE_KIND_ALIASES,
    EXPERIENCE_KINDS as DOMAIN_EXPERIENCE_KINDS,
    annotate_explicit_domain_frontend_plan,
    build_domain_frontend_plan,
    derive_domain_frontend_decision,
    keyword_matches as domain_frontend_keyword_matches,
)
from .browser_e2e import (
    BROWSER_E2E_EVIDENCE_DIR,
    BROWSER_USER_EXPERIENCE_FAILURE_MARKER,
    BROWSER_USER_EXPERIENCE_GATE_KIND,
    BROWSER_USER_EXPERIENCE_SCHEMA_VERSION,
    browser_user_experience_command,
    browser_user_experience_gate,
    detect_playwright_support,
    is_browser_experience_kind,
)
from .io import append_jsonl, load_mapping, write_json, write_mapping
from .profiles import command_policy, default_roadmap
from .spec_backlog import (
    SPEC_BACKLOG_GENERATOR_ID,
    build_spec_backlog_plan,
    materialize_spec_backlog_plan,
)


COMPLETED_STATUSES = {"done", "passed", "skipped"}
BLOCKED_STATUSES = {"blocked", "paused"}
CONFIG_CANDIDATES = (".engineering/roadmap.yaml", ".engineering/roadmap.json", "ops/engineering/roadmap.yaml")
PRUNE_DIRS = {".git", "node_modules", ".venv", "venv", ".pytest_cache", "dist", "out", "cache", "artifacts"}
EXPERIENCE_KINDS = DOMAIN_EXPERIENCE_KINDS
POLICY_INPUT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
PHASE_STATE_SCHEMA_VERSION = 1
REPLAY_GUARD_SCHEMA_VERSION = 1
REPLAY_GUARD_SUMMARY_LIMIT = 25
DRIVE_CONTROL_SCHEMA_VERSION = 2
DRIVE_WATCHDOG_SCHEMA_VERSION = 1
STALE_RUNNING_RECOVERY_SCHEMA_VERSION = 1
DEFAULT_DRIVE_HEARTBEAT_STALE_SECONDS = 60 * 60
DRIVE_WATCHDOG_STALE_SECONDS_ENV = "ENGINEERING_HARNESS_DRIVE_STALE_AFTER_SECONDS"
EXECUTOR_NO_PROGRESS_SECONDS_ENV = "ENGINEERING_HARNESS_EXECUTOR_NO_PROGRESS_SECONDS"
EXECUTOR_NO_PROGRESS_PHASE_ENV_PREFIX = "ENGINEERING_HARNESS_EXECUTOR_NO_PROGRESS_"
EXECUTOR_WATCHDOG_ENABLED_ENV = "ENGINEERING_HARNESS_EXECUTOR_WATCHDOG_ENABLED"
DEFAULT_EXECUTOR_NO_PROGRESS_SECONDS = 0
MATERIALIZATION_CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_READINESS_SCHEMA_VERSION = 1
APPROVAL_QUEUE_SCHEMA_VERSION = 2
APPROVAL_FINGERPRINT_SCHEMA_VERSION = 1
DEFAULT_APPROVAL_LEASE_TTL_SECONDS = 60 * 60
FAILURE_ISOLATION_SCHEMA_VERSION = 1
FAILURE_ISOLATION_SUMMARY_LIMIT = 5
ISOLATED_FAILURE_STATUSES = {"failed", "blocked"}
EXECUTOR_WATCHDOG_FAILURE_STATUSES = {"timeout", "no_progress"}
CAPABILITY_POLICY_SCHEMA_VERSION = 1
COMMAND_SAFETY_CLASSIFICATION_SCHEMA_VERSION = 1
SAFETY_AUDIT_SCHEMA_VERSION = 1
UNSAFE_EXECUTOR_CAPABILITIES = {
    "filesystem_escape",
    "host_filesystem_write",
    "network",
    "network_access",
    "secret_access",
    "secrets",
    "browser_automation",
    "deployment",
    "deploy",
    "live_operations",
    "live",
}
LOCAL_CAPABILITY_VOCABULARY = {
    "agent",
    "containerized_execution",
    "exit_code",
    "filesystem_escape",
    "host_filesystem_write",
    "local_dagger_cli",
    "local_openhands_cli",
    "local_process",
    "requires_explicit_configuration",
    "stderr",
    "stdout",
    "workspace_write",
    *UNSAFE_EXECUTOR_CAPABILITIES,
}
COMMAND_CAPABILITY_REQUEST_FIELDS = ("requested_capabilities", "capabilities")
UNSAFE_CAPABILITY_CLASSES = {
    "filesystem": ("filesystem_escape", "host_filesystem_write"),
    "network": ("network_access",),
    "secret": ("secret_access",),
    "deploy": ("deployment",),
}
SAFE_SANDBOX_MODES = {"workspace-write", "read-only"}
UNSAFE_SANDBOX_MODES = {
    "danger-full-access",
    "full-access",
    "full_auto",
    "none",
    "no-sandbox",
    "off",
    "unconfined",
    "unsandboxed",
}
COMMAND_UNSAFE_OPERATION_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "network": (
        ("network_url", re.compile(r"\b(?:https?|wss?|ftp)://", re.IGNORECASE)),
        ("python_network_module", re.compile(r"\b(?:requests|urllib|httpx|aiohttp|socket)\b")),
        ("network_cli", re.compile(r"\b(?:curl|wget|nc|netcat|telnet|ssh|scp|rsync)\b", re.IGNORECASE)),
        ("git_remote_network", re.compile(r"\bgit\s+(?:clone|fetch|pull|push)\b", re.IGNORECASE)),
    ),
    "secret": (
        (
            "sensitive_env_name",
            re.compile(
                r"\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|PRIVATE_KEY|MNEMONIC|"
                r"[A-Z0-9_]*(?:API_KEY|ACCESS_KEY)|"
                r"[A-Z0-9_]+_(?:TOKEN|SECRET|PASSWORD|CREDENTIALS?)|"
                r"(?:TOKEN|SECRET|PASSWORD|CREDENTIALS?)_[A-Z0-9_]+)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "secret_file_path",
            re.compile(
                r"(?:~|/home/[^\\s\"']+)/(?:\\.ssh|\\.aws|\\.config/gcloud)|"
                r"\b(?:id_rsa|id_ed25519|\\.env)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    "filesystem": (
        (
            "destructive_filesystem",
            re.compile(r"\b(?:rm\s+-rf\s+/|git\s+reset\s+--hard|git\s+checkout\s+--|git\s+clean\s+-fd)\b"),
        ),
        (
            "host_filesystem_path",
            re.compile(
                r"(?:Path|open)\(\s*['\"](?:\.\./|/etc/|/root/|/home/|~/(?:\.ssh|\.aws)|\$HOME)|"
                r"(?:>\s*|>>\s*|touch\s+|mkdir\s+|cp\s+.*\s+|mv\s+.*\s+)(?:\.\./|/etc/|/root/|~/(?:\.ssh|\.aws))",
                re.IGNORECASE,
            ),
        ),
    ),
    "deploy": (
        ("git_push", re.compile(r"\bgit\s+push\b", re.IGNORECASE)),
        ("package_publish", re.compile(r"\b(?:npm\s+publish|pnpm\s+publish|yarn\s+npm\s+publish|twine\s+upload)\b", re.IGNORECASE)),
        ("container_push", re.compile(r"\bdocker\s+push\b", re.IGNORECASE)),
        ("infra_apply", re.compile(r"\b(?:kubectl\s+apply|helm\s+upgrade|terraform\s+apply|pulumi\s+up)\b", re.IGNORECASE)),
        (
            "production_deploy",
            re.compile(
                r"\b(?:vercel\s+--prod|netlify\s+deploy\s+--prod|firebase\s+deploy|"
                r"deploy:mainnet|verify:mainnet|cast\s+send|--broadcast)\b",
                re.IGNORECASE,
            ),
        ),
    ),
}
SENSITIVE_ENV_NAME_PATTERN = (
    r"[A-Z0-9_]*(?:API[-_]?KEY|ACCESS[-_]?KEY|TOKEN|SECRET|PASSWORD|PASS|"
    r"PRIVATE[-_]?KEY|MNEMONIC|SEED(?:[-_]?PHRASE)?|CREDENTIALS?)[A-Z0-9_]*"
)
SENSITIVE_ENV_NAME_RE = re.compile(rf"(?i)^{SENSITIVE_ENV_NAME_PATTERN}$")
SENSITIVE_QUOTED_VALUE_RE = re.compile(
    rf"(?i)\b({SENSITIVE_ENV_NAME_PATTERN})\b(\s*[:=]\s*)(['\"])(.*?)(\3)"
)
SENSITIVE_UNQUOTED_VALUE_RE = re.compile(
    rf"(?i)\b({SENSITIVE_ENV_NAME_PATTERN})\b(\s*=\s*)([^\s\"'`]+)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
OPENAI_STYLE_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{8,})\b")
APPROVAL_DECISION_KINDS = {
    "manual_approval": "manual",
    "agent_approval": "agent",
    "executor_approval": "agent",
    "live_approval": "live",
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
        "primary_surfaces": ["submission portal", "review console", "revision upload", "status timeline"],
        "auth": {"required": True, "roles": ["student", "reviewer"]},
        "e2e_journeys": [
            {
                "id": "student-submit-review-revise",
                "persona": "student",
                "goal": "Submit work, receive reviewer comments, upload a revision, and view the decision.",
            }
        ],
    },
    "multi-role-app": {
        "kind": "multi-role-app",
        "personas": ["admin", "operator", "approver"],
        "primary_surfaces": ["login", "admin console", "operator queue", "approval screen", "audit log"],
        "auth": {"required": True, "roles": ["admin", "operator", "approver"]},
        "e2e_journeys": [
            {
                "id": "operator-requests-approval",
                "persona": "operator",
                "goal": "Create a work item, request approval, and verify role boundaries and audit history.",
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
        "paper review",
        "review workflow",
    ),
    "multi-role-app": ("multi-role-app", "multi-role", "multi role", "role-specific", "role based"),
    "api-only": ("api-only", "api only", "api-first", "api first", "rest api", "openapi", "api"),
    "cli-only": ("cli-only", "cli only", "command line", "command-line", "cli"),
    "dashboard": ("dashboard", "operator console", "run queue"),
}
EXPERIENCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "submission-review": (
        "submission",
        "submit",
        "student",
        "reviewer",
        "review",
        "revision",
        "paper",
        "assignment",
        "comments",
        "decision",
        "grade",
    ),
    "multi-role-app": (
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
        "backtest",
        "monitor",
        "observability",
    ),
}
DEFAULT_EXPERIENCE_PLANS = DOMAIN_DEFAULT_EXPERIENCE_PLANS
EXPERIENCE_KIND_ALIASES = DOMAIN_EXPERIENCE_KIND_ALIASES
EXPERIENCE_KEYWORDS = DOMAIN_EXPERIENCE_KEYWORDS
FRONTEND_TASK_MILESTONE_ID = "frontend-visualization"
FRONTEND_TASK_GENERATOR = "engineering-harness-frontend-task-generator"
SELF_ITERATION_CONTEXT_SCHEMA_VERSION = 1
SELF_ITERATION_ASSESSMENT_SCHEMA_VERSION = 1
GOAL_GAP_RETROSPECTIVE_SCHEMA_VERSION = 1
GOAL_GAP_SCORECARD_SCHEMA_VERSION = 1
RUNTIME_DASHBOARD_SCHEMA_VERSION = 1
SPEC_COVERAGE_SCHEMA_VERSION = 1
OPERATOR_CONSOLE_SCHEMA_VERSION = 1
WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION = 1
WORKSPACE_DISPATCH_LEASE_DIRNAME = "workspace-dispatch-lease"
WORKSPACE_DISPATCH_REPORT_LIMIT = 5
DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION = 1
DAEMON_SUPERVISOR_RUNTIME_STATE_FILENAME = "daemon-supervisor-runtime.json"
DAEMON_SUPERVISOR_RUNTIME_REPORT_DIRNAME = "daemon-supervisor-runtime"
DAEMON_SUPERVISOR_RUNTIME_REPORT_LIMIT = 5
UNATTENDED_RELIABILITY_GOAL = (
    "Run unattended engineering drives that drain or safely extend the roadmap, preserve local audit "
    "evidence, surface blockers deterministically, and avoid unsafe external dependencies."
)
SELF_ITERATION_CONTEXT_LIMITS = {
    "recent_manifest_count": 5,
    "recent_report_count": 8,
    "doc_count": 8,
    "doc_excerpt_chars": 1200,
    "test_file_count": 60,
    "test_name_count": 20,
    "source_file_count": 120,
    "continuation_stage_count": 12,
    "duplicate_plan_stage_count": 12,
    "duplicate_plan_task_count": 8,
    "duplicate_plan_group_count": 8,
    "manifest_run_count": 8,
    "message_chars": 500,
    "git_commit_count": 8,
    "goal_gap_scorecard_evidence_paths": 8,
    "goal_gap_scorecard_themes": 4,
}
AGENT_CONTEXT_PACK_SCHEMA_VERSION = 1
AGENT_CONTEXT_PACK_DIRNAME = "agent-context-packs"
AGENT_CONTEXT_PACK_LIMITS = {
    "requirement_count": 12,
    "requirement_excerpt_chars": 1200,
    "prompt_chars": 1200,
    "verification_command_count": 20,
    "roadmap_task_count": 12,
    "roadmap_command_count": 4,
    "continuation_stage_count": 8,
    "recent_manifest_count": 5,
    "manifest_run_count": 6,
    "test_file_count": 40,
    "test_name_count": 12,
    "git_commit_count": 6,
    "message_chars": 500,
}
OPERATOR_CONSOLE_LIMITS = {
    "recent_task_runs": 8,
    "recent_drive_runs": 5,
    "timeline_tasks": 8,
    "timeline_events_per_task": 10,
    "pending_tasks": 12,
    "approval_items": 12,
    "failure_items": 8,
    "replay_guard_items": 8,
    "e2e_files": 12,
    "e2e_runs": 8,
    "recommended_actions": 8,
    "message_chars": 220,
    "max_json_bytes": 120_000,
}
GOAL_GAP_SCORECARD_CATEGORY_ORDER = (
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
)
GOAL_GAP_SCORECARD_STATUS_ORDER = ("complete", "partial", "missing", "blocked")
SELF_ITERATION_DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt"}
SELF_ITERATION_TEST_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".sh"}
SELF_ITERATION_SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".sh",
}
SELF_ITERATION_UNSAFE_REQUIREMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private credential use",
        re.compile(
            r"\b(?:use|configure|require|load|import|set|provide|read)\s+"
            r"(?:a\s+)?(?:private\s+keys?|mnemonics?|seed\s+phrases?|api\s+keys?)\b"
        ),
    ),
    (
        "private credential assignment",
        re.compile(r"\b(?:PRIVATE_KEY|MNEMONIC|OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=", re.IGNORECASE),
    ),
    (
        "mainnet write",
        re.compile(
            r"\b(?:cast\s+send|deploy:mainnet|--broadcast|--rpc-url\s+mainnet|"
            r"mainnet\s+(?:write|transaction|deploy|deployment|release))\b"
        ),
    ),
    ("production deployment", re.compile(r"\bdeploy(?:ment)?\s+(?:to\s+)?(?:production|prod|mainnet)\b")),
    ("production deployment", re.compile(r"\b(?:production|prod|mainnet)\s+(?:deploy|deployment|release)\b")),
    (
        "production service mutation",
        re.compile(
            r"\b(?:call|write\s+to|mutate|modify)\s+(?:the\s+)?(?:production|prod|live)\s+"
            r"(?:api|service|database|system)\b"
        ),
    ),
    ("live operation", re.compile(r"\b--live\b|\blive\s+(?:deployment|service|trading|orders?|trades?)\b")),
    ("live operation", re.compile(r"\b(?:place|submit|send|execute)\s+(?:real|live)\s+(?:orders?|trades?)\b")),
    (
        "real-fund movement",
        re.compile(
            r"\b(?:withdraw|transfer|move)\s+real\s+(?:funds|money)\b|"
            r"\breal[- ]?fund\s+(?:transfer|withdrawal|payment)\b"
        ),
    ),
    ("paid service", re.compile(r"\bpaid[- ]?(?:service|services|account|accounts|hosting|deployment|api|subscription)\b")),
)
SPEC_REQUIREMENT_ID_PATTERN = r"[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+-\d+"
SPEC_REQUIREMENT_ID_RE = re.compile(rf"\b{SPEC_REQUIREMENT_ID_PATTERN}\b")
SPEC_MARKDOWN_ANY_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+")
SPEC_MARKDOWN_REQUIREMENT_HEADING_RE = re.compile(
    rf"^(?P<marks>#{{1,6}})\s+(?P<id>{SPEC_REQUIREMENT_ID_PATTERN})(?P<title>(?:\b|:).*)?$"
)
SPEC_MARKDOWN_HEADING_RE = re.compile(
    rf"^#{{1,6}}\s+(?P<id>{SPEC_REQUIREMENT_ID_PATTERN})(?:\b|:)",
    re.MULTILINE,
)
FRONTEND_KIND_LABELS = {
    "app-specific": "app-specific frontend",
    "dashboard": "operator dashboard",
    "submission-review": "submission review workflow",
    "multi-role-app": "multi-role application",
    "api-only": "API-first experience",
    "cli-only": "CLI-first experience",
}
FRONTEND_KIND_TASK_GUIDANCE: dict[str, dict[str, Any]] = {
    "app-specific": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["app-specific", "workspace", "create", "detail", "empty", "error"],
        "implementation_focus": (
            "Build or document the app-specific primary workflow using the project's existing UI conventions. "
            "Cover the main workspace, create or edit flow, detail state, empty state, and error state."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "dashboard": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["dashboard", "status", "queue", "artifact", "loading", "empty", "error"],
        "implementation_focus": (
            "Build or document the operator dashboard using the project's existing UI conventions. "
            "Cover status, queue/detail state, artifacts, loading, empty, and error states."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "submission-review": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["submission", "reviewer", "revision", "comments", "decision", "status timeline"],
        "implementation_focus": (
            "Build or document the student and reviewer surfaces using the project's existing stack. "
            "Cover submission upload, reviewer comments, revision upload, decision state, and timeline states."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "multi-role-app": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["role", "login", "permission", "approval", "audit", "denied"],
        "implementation_focus": (
            "Build or document authenticated role-specific surfaces using the project's existing stack. "
            "Cover login, role routes, permission denial, approval handoff, and audit history."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "api-only": {
        "file_scope": [
            "src/**",
            "api/**",
            "openapi/**",
            "docs/**",
            "examples/**",
            "tests/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["api", "openapi", "example", "request", "response", "status"],
        "implementation_focus": (
            "Build or document the API-first user path without requiring a browser UI. "
            "Cover API reference, example client flow, request/response expectations, auth if required, and service status."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.py",
            "tests/api/{slug}.py",
            "tests/e2e/{slug}.sh",
            "examples/{slug}.md",
            "docs/e2e/{slug}.md",
        ),
    },
    "cli-only": {
        "file_scope": [
            "src/**",
            "cli/**",
            "docs/**",
            "examples/**",
            "tests/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["cli", "command", "example", "output", "report"],
        "implementation_focus": (
            "Build or document the CLI-first user path without requiring a browser UI. "
            "Cover documented commands, inputs, output/report inspection, failure messages, and repeatable examples."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.py",
            "tests/cli/{slug}.py",
            "tests/e2e/{slug}.sh",
            "examples/{slug}.md",
            "docs/e2e/{slug}.md",
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def redact(text: str) -> str:
    redacted = str(text)
    redacted = SENSITIVE_QUOTED_VALUE_RE.sub(r"\1\2\3[REDACTED]\5", redacted)
    redacted = SENSITIVE_UNQUOTED_VALUE_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = BEARER_TOKEN_RE.sub(r"\1[REDACTED]", redacted)
    redacted = OPENAI_STYLE_TOKEN_RE.sub("[REDACTED]", redacted)
    return redacted


def sensitive_evidence_key(key: object) -> bool:
    text = str(key).strip()
    if not text:
        return False
    if text.lower() in {
        "blocked",
        "completed",
        "done",
        "error",
        "failed",
        "passed",
        "pending",
        "skipped",
        "status",
        "warning",
    }:
        return False
    upper = text.upper()
    if upper.endswith(("_CONFIGURED", "_PRESENT", "_SET", "_AVAILABLE", "_ENABLED")):
        return False
    return bool(SENSITIVE_ENV_NAME_RE.fullmatch(text))


def redact_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if (
                sensitive_evidence_key(text_key)
                and item is not None
                and not isinstance(item, bool)
                and not isinstance(item, (dict, list, tuple))
            ):
                redacted[text_key] = "[REDACTED]"
            else:
                redacted[text_key] = redact_evidence(item)
        return redacted
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [redact_evidence(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def capability_core_classes(capabilities: tuple[str, ...] | list[str]) -> list[str]:
    classifications = classify_capabilities(capabilities)
    classes = classifications.get("classes", {}) if isinstance(classifications, dict) else {}
    return sorted(
        class_name
        for class_name in UNSAFE_CAPABILITY_CLASSES
        if isinstance(classes.get(class_name), list) and classes.get(class_name)
    )


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    roadmap_path: Path | None
    profile: str | None
    configured: bool
    kind: str


@dataclass(frozen=True)
class AcceptanceCommand:
    name: str
    command: str | None
    timeout_seconds: int
    required: bool = True
    executor: str = "shell"
    prompt: str | None = None
    model: str | None = None
    sandbox: str = "workspace-write"
    no_progress_timeout_seconds: int | None = None
    requested_capabilities: tuple[str, ...] = ()
    user_experience_gate: dict[str, Any] = field(default_factory=dict)
    spec_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessTask:
    id: str
    title: str
    milestone_id: str
    milestone_title: str
    status: str
    max_attempts: int
    file_scope: tuple[str, ...]
    manual_approval_required: bool
    agent_approval_required: bool
    max_task_iterations: int
    spec_refs: tuple[str, ...]
    implementation: tuple[AcceptanceCommand, ...]
    repair: tuple[AcceptanceCommand, ...]
    acceptance: tuple[AcceptanceCommand, ...]
    e2e: tuple[AcceptanceCommand, ...]


@dataclass(frozen=True)
class CommandRun:
    phase: str
    name: str
    command: str
    status: str
    returncode: int | None
    started_at: str
    finished_at: str
    stdout: str
    stderr: str
    executor: str = "shell"
    executor_metadata: dict[str, Any] = field(default_factory=dict)
    result_metadata: dict[str, Any] = field(default_factory=dict)
    context_pack: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyInput:
    project: dict[str, Any]
    task: dict[str, Any]
    phase: str
    command: dict[str, Any] | None
    executor: dict[str, Any] | None
    git: dict[str, Any]
    worktree: dict[str, Any]
    file_scope: dict[str, Any]
    approvals: dict[str, Any]
    live: dict[str, Any]
    context: dict[str, Any]

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_INPUT_SCHEMA_VERSION,
            "project": self.project,
            "task": self.task,
            "phase": self.phase,
            "command": self.command,
            "executor": self.executor,
            "git": self.git,
            "worktree": self.worktree,
            "file_scope": self.file_scope,
            "approvals": self.approvals,
            "live": self.live,
            "context": self.context,
        }


@dataclass(frozen=True)
class PolicyDecision:
    kind: str
    scope: str
    outcome: str
    reason: str
    policy_input: PolicyInput
    severity: str = "info"
    effect: str = "allow"
    requires_approval: bool = False
    approval_flag: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def blocks_execution(self) -> bool:
        return self.effect in {"deny", "requires_approval"}

    def as_contract(self) -> dict[str, Any]:
        policy_input = self.policy_input.as_contract()
        command = policy_input.get("command") or {}
        executor = policy_input.get("executor") or {}
        payload: dict[str, Any] = {
            "schema_version": POLICY_DECISION_SCHEMA_VERSION,
            "kind": self.kind,
            "scope": self.scope,
            "outcome": self.outcome,
            "effect": self.effect,
            "severity": self.severity,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "input": policy_input,
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.approval_flag is not None:
            payload["approval_flag"] = self.approval_flag
        if self.metadata:
            payload["metadata"] = self.metadata
        phase = policy_input.get("phase")
        if phase:
            payload["phase"] = phase
        if command.get("name"):
            payload["name"] = command["name"]
        if executor.get("id"):
            payload["executor"] = executor["id"]
        return payload


def find_project_config(root: Path) -> Path | None:
    for relative in CONFIG_CANDIDATES:
        path = root / relative
        if path.exists():
            return path
    return None


def guess_profile(root: Path) -> tuple[str | None, str]:
    if (root / "foundry.toml").exists() or any(root.glob("*/foundry.toml")):
        return "evm-protocol", "evm"
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        return "python-agent", "python"
    if (root / "package.json").exists():
        return "node-frontend", "node"
    if any((root / child).exists() for child in ("ara", "agents", "runtime")):
        return "python-agent", "agent"
    return None, "unknown"


def project_from_root(root: Path) -> Project:
    root = root.resolve()
    config = find_project_config(root)
    profile, kind = guess_profile(root)
    configured = config is not None
    name = root.name
    if config:
        config_in_scope = False
        try:
            config_in_scope = config.resolve().is_relative_to(root)
        except OSError:
            config_in_scope = False
        if config_in_scope:
            try:
                roadmap = load_mapping(config)
                name = str(roadmap.get("project", name))
                profile = str(roadmap.get("profile", profile or "")) or profile
            except Exception:
                pass
    return Project(name=name, root=root, roadmap_path=config, profile=profile, configured=configured, kind=kind)


def discover_projects(workspace: Path, max_depth: int = 3) -> list[Project]:
    workspace = workspace.resolve()
    projects: dict[Path, Project] = {}
    for current, dirs, files in os.walk(workspace):
        current_path = Path(current)
        depth = len(current_path.relative_to(workspace).parts)
        dirs[:] = [name for name in dirs if name not in PRUNE_DIRS and not name.startswith(".cache")]
        if depth >= max_depth:
            dirs[:] = []
        has_git = ".git" in dirs or ".git" in files or (current_path / ".git").exists()
        has_config = find_project_config(current_path) is not None
        has_known = any((current_path / name).exists() for name in ("package.json", "pyproject.toml", "foundry.toml"))
        if current_path != workspace and (has_git or has_config or has_known):
            project = project_from_root(current_path)
            projects[current_path] = project
            if has_git or has_config:
                dirs[:] = [name for name in dirs if name.startswith(".engineering")]
    return sorted(projects.values(), key=lambda item: str(item.root))


def init_project(project_root: Path, profile_id: str, name: str | None = None, force: bool = False) -> dict[str, Any]:
    project_root = project_root.resolve()
    engineering = project_root / ".engineering"
    roadmap_path = engineering / "roadmap.yaml"
    policy_dir = engineering / "policies"
    state_dir = engineering / "state"
    report_dir = engineering / "reports"
    if roadmap_path.exists() and not force:
        raise FileExistsError(f"{roadmap_path} already exists. Use --force to replace it.")
    project_name = name or project_root.name
    write_json(roadmap_path, default_roadmap(project_name, profile_id))
    write_json(policy_dir / "command-allowlist.yaml", command_policy(profile_id))
    write_json(
        policy_dir / "deployment-policy.yaml",
        {
            "version": 1,
            "profile": profile_id,
            "requires_human_approval": [
                "mainnet deployment",
                "contract upgrade or migration",
                "private key or API key configuration",
                "real-fund transfer",
                "live trading",
            ],
        },
    )
    write_json(
        policy_dir / "secret-policy.yaml",
        {
            "version": 1,
            "rules": [
                "Do not write secrets into tracked files.",
                "Use environment variables for private keys and API keys.",
                "Reports must redact secret-looking values before becoming versioned artifacts.",
            ],
        },
    )
    for directory in (state_dir, report_dir):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitignore").write_text("*\n!.gitignore\n!.gitkeep\n", encoding="utf-8")
        (directory / ".gitkeep").write_text("", encoding="utf-8")
    return {
        "project": project_name,
        "profile": profile_id,
        "roadmap": str(roadmap_path),
        "policy_dir": str(policy_dir),
    }


class Harness:
    def __init__(
        self,
        project_root: Path,
        roadmap_path: Path | None = None,
        executor_registry: ExecutorRegistry | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.roadmap_path = roadmap_path or find_project_config(self.project_root)
        if self.roadmap_path is None:
            raise FileNotFoundError(f"No engineering roadmap found in {self.project_root}")
        self.roadmap = load_mapping(self.roadmap_path)
        self.default_timeout = int(self.roadmap.get("default_timeout_seconds", 300))
        self.state_path = self.project_root / str(self.roadmap.get("state_path", ".engineering/state/harness-state.json"))
        self.decision_log_path = self.project_root / str(
            self.roadmap.get("decision_log_path", ".engineering/state/decision-log.jsonl")
        )
        self.report_dir = self.project_root / str(self.roadmap.get("report_dir", ".engineering/reports/tasks"))
        manifest_index_path = self.roadmap.get("manifest_index_path")
        self.manifest_index_path = (
            self.project_root / str(manifest_index_path)
            if manifest_index_path
            else self.report_dir / "manifest-index.json"
        )
        self.command_policy = self._load_command_policy()
        self.executor_registry = executor_registry or default_executor_registry()

    def _load_command_policy(self) -> dict[str, Any]:
        policy_candidates = [
            self.project_root / ".engineering/policies/command-allowlist.yaml",
            self.project_root / "ops/engineering/policies/command-allowlist.yaml",
        ]
        for path in policy_candidates:
            if path.exists():
                return load_mapping(path)
        profile = str(self.roadmap.get("profile", "")) or "python-agent"
        return command_policy(profile)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "updated_at": None, "tasks": {}}
        payload = load_mapping(self.state_path)
        if "tasks" not in payload or not isinstance(payload["tasks"], dict):
            payload["tasks"] = {}
        return payload

    def save_state(self, state: dict[str, Any]) -> None:
        state["version"] = 1
        state["updated_at"] = utc_now()
        write_json(self.state_path, state)

    def _drive_watchdog_stale_after_seconds(self) -> int:
        candidates: list[Any] = []
        env_value = os.environ.get(DRIVE_WATCHDOG_STALE_SECONDS_ENV)
        if env_value is not None:
            candidates.append(env_value)
        config = self.roadmap.get("drive_watchdog")
        if isinstance(config, dict):
            candidates.extend(
                [
                    config.get("stale_after_seconds"),
                    config.get("heartbeat_stale_after_seconds"),
                ]
            )
        candidates.extend(
            [
                self.roadmap.get("drive_stale_after_seconds"),
                self.roadmap.get("drive_heartbeat_stale_after_seconds"),
            ]
        )
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                seconds = int(candidate)
            except (TypeError, ValueError):
                continue
            if seconds >= 0:
                return seconds
        return DEFAULT_DRIVE_HEARTBEAT_STALE_SECONDS

    def _executor_watchdog_enabled(self) -> bool:
        env_value = os.environ.get(EXECUTOR_WATCHDOG_ENABLED_ENV)
        if env_value is not None:
            return str(env_value).strip().lower() not in {"0", "false", "no", "off"}
        config = self.roadmap.get("executor_watchdog")
        if isinstance(config, dict) and "enabled" in config:
            return bool(config.get("enabled"))
        return True

    def _executor_watchdog_phase_key(self, phase: str | None) -> str:
        normalized = str(phase or "").strip().lower().replace("_", "-")
        if normalized.startswith("acceptance"):
            return "acceptance"
        if normalized.startswith("repair"):
            return "repair"
        if normalized.startswith("implementation"):
            return "implementation"
        if normalized.startswith("e2e"):
            return "e2e"
        if normalized in {"self-iteration", "self-iteration-planner", "planner"}:
            return "planner"
        return normalized or "task"

    def _coerce_optional_nonnegative_seconds(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds >= 0 else None

    def _executor_no_progress_timeout_seconds(
        self,
        phase: str | None,
        command: AcceptanceCommand | None = None,
    ) -> int | None:
        if not self._executor_watchdog_enabled():
            return None
        if command is not None and command.no_progress_timeout_seconds is not None:
            seconds = self._coerce_optional_nonnegative_seconds(command.no_progress_timeout_seconds)
            return seconds if seconds and seconds > 0 else None

        phase_key = self._executor_watchdog_phase_key(phase)
        env_phase = os.environ.get(f"{EXECUTOR_NO_PROGRESS_PHASE_ENV_PREFIX}{phase_key.upper()}_SECONDS")
        env_global = os.environ.get(EXECUTOR_NO_PROGRESS_SECONDS_ENV)
        config = self.roadmap.get("executor_watchdog")
        phase_config: dict[str, Any] = {}
        if isinstance(config, dict):
            configured_phases = config.get("phase_no_progress_seconds") or config.get("phases") or {}
            if isinstance(configured_phases, dict):
                phase_config = configured_phases
        candidates: list[Any] = [
            env_phase,
            env_global,
            phase_config.get(phase_key),
        ]
        if isinstance(config, dict):
            candidates.extend(
                [
                    config.get(f"{phase_key}_no_progress_seconds"),
                    config.get("no_progress_timeout_seconds"),
                    config.get("no_progress_seconds"),
                ]
            )
        candidates.extend(
            [
                self.roadmap.get("executor_no_progress_timeout_seconds"),
                self.roadmap.get("executor_no_progress_seconds"),
                DEFAULT_EXECUTOR_NO_PROGRESS_SECONDS,
            ]
        )
        for candidate in candidates:
            seconds = self._coerce_optional_nonnegative_seconds(candidate)
            if seconds is None:
                continue
            return seconds if seconds > 0 else None
        return None

    def executor_watchdog_summary(self) -> dict[str, Any]:
        phases = ("implementation", "repair", "acceptance", "e2e", "planner")
        return {
            "schema_version": EXECUTOR_WATCHDOG_CONTRACT_VERSION,
            "enabled": self._executor_watchdog_enabled(),
            "default_no_progress_seconds": self._executor_no_progress_timeout_seconds(None),
            "phase_no_progress_seconds": {
                phase: self._executor_no_progress_timeout_seconds(phase)
                for phase in phases
            },
            "timeout_source": "command.timeout_seconds",
            "no_progress_env": EXECUTOR_NO_PROGRESS_SECONDS_ENV,
            "phase_no_progress_env_prefix": EXECUTOR_NO_PROGRESS_PHASE_ENV_PREFIX,
        }

    def _coerce_pid(self, value: Any) -> int | None:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _process_is_running(self, pid: int | None) -> bool | None:
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

    def _drive_control(self, state: dict[str, Any]) -> dict[str, Any]:
        control = state.setdefault("drive_control", {})
        if not isinstance(control, dict):
            control = {}
            state["drive_control"] = control
        control["schema_version"] = DRIVE_CONTROL_SCHEMA_VERSION
        control.setdefault("status", "idle")
        control.setdefault("active", False)
        control.setdefault("pause_requested", False)
        control.setdefault("cancel_requested", False)
        control.setdefault("reason", None)
        control.setdefault("updated_at", state.get("updated_at"))
        control.setdefault("pid", None)
        control.setdefault("started_at", None)
        control.setdefault("last_heartbeat_at", None)
        control.setdefault("heartbeat_count", 0)
        control["stale_after_seconds"] = self._drive_watchdog_stale_after_seconds()
        control.setdefault("current_activity", None)
        control.setdefault("current_task", None)
        control.setdefault("executor_watchdog", None)
        control.setdefault("latest_executor_event", None)
        control.setdefault("executor_event_count", 0)
        executor_event_history = control.setdefault("executor_event_history", [])
        if not isinstance(executor_event_history, list):
            control["executor_event_history"] = []
        control.setdefault("last_progress_message", None)
        control.setdefault("stale_reason", None)
        control.setdefault("stale_detected_at", None)
        control.setdefault("stale_running_recovery", None)
        control.setdefault("stale_running_preflight", None)
        control.setdefault("stale_running_block", None)
        history = control.setdefault("history", [])
        if not isinstance(history, list):
            control["history"] = []
        return control

    def _drive_watchdog_status(self, control: dict[str, Any]) -> dict[str, Any]:
        checked_at = utc_now()
        checked_dt = parse_utc_timestamp(checked_at) or datetime.now(timezone.utc)
        threshold = int(control.get("stale_after_seconds", self._drive_watchdog_stale_after_seconds()) or 0)
        status = str(control.get("status", "idle"))
        pid = self._coerce_pid(control.get("pid"))
        heartbeat_at = control.get("last_heartbeat_at")
        heartbeat_dt = parse_utc_timestamp(heartbeat_at)
        heartbeat_age_seconds = None
        if heartbeat_dt is not None:
            heartbeat_age_seconds = max(0, int((checked_dt - heartbeat_dt).total_seconds()))
        pid_alive = self._process_is_running(pid)
        protected_control = (
            status in {"paused", "cancelled"}
            or bool(control.get("pause_requested"))
            or bool(control.get("cancel_requested"))
        )
        watching = (
            status == "running"
            or (bool(control.get("active")) and not protected_control)
            or status == "stale"
        )
        stale = False
        reason = None
        message = "drive is not running"
        if watching:
            message = "drive heartbeat is fresh"
            if status == "stale":
                stale = True
                reason = str(control.get("stale_reason") or "stale")
                message = f"drive is stale: {reason}"
            elif heartbeat_dt is not None and heartbeat_age_seconds is not None and heartbeat_age_seconds <= threshold:
                message = "drive heartbeat is fresh"
            elif heartbeat_dt is None:
                stale = True
                reason = "missing_heartbeat"
                message = "running drive has no recorded heartbeat"
            elif heartbeat_age_seconds is not None and heartbeat_age_seconds > threshold:
                stale = True
                reason = "heartbeat_stale"
                message = f"drive heartbeat is stale after {heartbeat_age_seconds}s"
        return {
            "schema_version": DRIVE_WATCHDOG_SCHEMA_VERSION,
            "status": "stale" if stale else ("running" if watching else "idle"),
            "stale": stale,
            "reason": reason,
            "message": message,
            "checked_at": checked_at,
            "threshold_seconds": threshold,
            "pid": pid,
            "pid_alive": pid_alive,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": heartbeat_age_seconds,
        }

    def _stale_running_recovery_follow_up(self) -> str:
        return (
            "Inspect the previous drive task state, latest report, and local worktree for partial "
            "changes before treating recovered work as complete."
        )

    def _stale_running_heartbeat_is_stale(self, watchdog: dict[str, Any]) -> bool:
        heartbeat_age_seconds = watchdog.get("heartbeat_age_seconds")
        threshold = int(watchdog.get("threshold_seconds", self._drive_watchdog_stale_after_seconds()) or 0)
        if heartbeat_age_seconds is None:
            return watchdog.get("heartbeat_at") is None
        try:
            return int(heartbeat_age_seconds) > threshold
        except (TypeError, ValueError):
            return False

    def _stale_running_recovery_reason(self, watchdog: dict[str, Any]) -> str:
        pid = self._coerce_pid(watchdog.get("pid"))
        heartbeat_missing = watchdog.get("heartbeat_at") is None
        pid_reason = "missing_pid" if pid is None else "dead_pid"
        heartbeat_reason = "missing_heartbeat" if heartbeat_missing else "stale_heartbeat"
        return f"{pid_reason}_and_{heartbeat_reason}"

    def _stale_running_preflight_from_control(
        self,
        control: dict[str, Any],
        *,
        watchdog: dict[str, Any] | None = None,
        reason: str = "drive_preflight",
    ) -> dict[str, Any]:
        watchdog_payload = watchdog or self._drive_watchdog_status(control)
        status = str(control.get("status", "idle"))
        active = bool(control.get("active", False))
        checked_at = str(watchdog_payload.get("checked_at") or utc_now())
        pid = self._coerce_pid(watchdog_payload.get("pid"))
        pid_alive = watchdog_payload.get("pid_alive")
        heartbeat_stale = self._stale_running_heartbeat_is_stale(watchdog_payload)
        heartbeat_fresh = watchdog_payload.get("heartbeat_at") is not None and not heartbeat_stale
        payload: dict[str, Any] = {
            "schema_version": STALE_RUNNING_RECOVERY_SCHEMA_VERSION,
            "kind": "engineering-harness.stale-running-recovery-preflight",
            "status": "not_needed",
            "reason": "not_running",
            "message": "drive control is not in a running state",
            "checked_at": checked_at,
            "previous_status": status,
            "previous_active": active,
            "previous_pid": pid,
            "pid_alive": pid_alive,
            "heartbeat_at": watchdog_payload.get("heartbeat_at"),
            "heartbeat_age_seconds": watchdog_payload.get("heartbeat_age_seconds"),
            "threshold_seconds": watchdog_payload.get("threshold_seconds"),
            "watchdog_status": watchdog_payload.get("status"),
            "watchdog_reason": watchdog_payload.get("reason"),
            "preflight_reason": reason,
            "recoverable": False,
            "blocking": False,
            "recommended_follow_up": self._stale_running_recovery_follow_up(),
        }
        if status != "running" and not active:
            return payload

        if (
            status in {"paused", "cancelled"}
            or bool(control.get("pause_requested"))
            or bool(control.get("cancel_requested"))
        ):
            if status == "cancelled" or bool(control.get("cancel_requested")):
                protected_reason = "cancelled"
            else:
                protected_reason = "paused"
            payload.update(
                {
                    "status": "not_needed",
                    "reason": protected_reason,
                    "message": (
                        f"drive control is {protected_reason}; stale-running recovery is not applied"
                    ),
                    "recoverable": False,
                    "blocking": False,
                }
            )
            return payload

        if heartbeat_stale and (pid is None or pid_alive is False):
            recovery_reason = self._stale_running_recovery_reason(watchdog_payload)
            payload.update(
                {
                    "status": "recoverable",
                    "reason": recovery_reason,
                    "message": (
                        "running drive control can be recovered because the heartbeat is stale "
                        "and the recorded process is absent or dead"
                    ),
                    "recoverable": True,
                    "blocking": False,
                }
            )
            return payload

        if heartbeat_fresh:
            payload.update(
                {
                    "status": "in_progress",
                    "reason": "heartbeat_fresh",
                    "message": "running drive control is protected by a fresh heartbeat",
                    "recoverable": False,
                    "blocking": False,
                }
            )
            return payload

        if pid_alive is True:
            block_reason = "pid_alive"
            message = "running drive control is still owned by a live process"
        else:
            block_reason = "running_state_not_recoverable"
            message = "running drive control does not meet stale recovery requirements"
        payload.update(
            {
                "status": "blocked",
                "reason": block_reason,
                "message": message,
                "recoverable": False,
                "blocking": True,
            }
        )
        return payload

    def _recover_stale_running_in_state(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        preflight: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        from_status = str(control.get("status", "running"))
        recovered_at = utc_now()
        evidence = {
            **deepcopy(preflight),
            "kind": "engineering-harness.stale-running-recovery",
            "status": "recovered",
            "reason": preflight.get("reason"),
            "message": "stale running drive state recovered to idle before selecting new work",
            "recovered_at": recovered_at,
            "preflight_reason": reason,
            "recoverable": True,
            "blocking": False,
        }
        control.update(
            {
                "status": "idle",
                "active": False,
                "pause_requested": False,
                "cancel_requested": False,
                "pid": None,
                "current_activity": "stale-running-recovery",
                "current_task": None,
                "executor_watchdog": None,
                "last_progress_message": evidence["message"],
                "stale_reason": None,
                "stale_detected_at": None,
                "stale_running_recovery": evidence,
                "stale_running_preflight": evidence,
                "stale_running_block": None,
                "last_drive_status": "recovered",
                "last_drive_message": evidence["message"],
                "recovered_at": recovered_at,
                "reason": reason,
                "updated_at": recovered_at,
            }
        )
        self._record_drive_control_event(
            state,
            command="stale-running-recovery",
            from_status=from_status,
            to_status="idle",
            reason=str(evidence.get("reason") or "stale_running_recovery"),
        )
        self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": recovered_at,
                "event": "stale_running_recovery",
                "status": "recovered",
                "reason": evidence.get("reason"),
                "previous_pid": evidence.get("previous_pid"),
                "pid_alive": evidence.get("pid_alive"),
                "heartbeat_at": evidence.get("heartbeat_at"),
                "heartbeat_age_seconds": evidence.get("heartbeat_age_seconds"),
                "threshold_seconds": evidence.get("threshold_seconds"),
                "recommended_follow_up": evidence.get("recommended_follow_up"),
            },
        )
        return deepcopy(evidence)

    def recover_stale_running_preflight(self, *, reason: str = "drive_preflight") -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        preflight = self._stale_running_preflight_from_control(control, reason=reason)
        if preflight.get("status") != "recoverable":
            return preflight
        return self._recover_stale_running_in_state(state, control, preflight, reason=reason)

    def _drive_control_summary_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        control = deepcopy(self._drive_control(state))
        watchdog = self._drive_watchdog_status(control)
        preflight = self._stale_running_preflight_from_control(
            control,
            watchdog=watchdog,
            reason="status_summary",
        )
        control["watchdog"] = watchdog
        control["stale_running_preflight"] = preflight
        control["stale_running_block"] = preflight if preflight.get("status") == "blocked" else None
        control["stale"] = bool(watchdog.get("stale"))
        if watchdog.get("stale"):
            control["status"] = "stale"
            control["active"] = False
            control["stale_reason"] = watchdog.get("reason")
        return control

    def _mark_drive_stale(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        *,
        watchdog: dict[str, Any],
    ) -> str:
        reason = str(watchdog.get("reason") or "stale")
        message = str(watchdog.get("message") or f"drive is stale: {reason}")
        from_status = str(control.get("status", "running"))
        now = utc_now()
        control.update(
            {
                "status": "stale",
                "active": False,
                "pause_requested": False,
                "cancel_requested": False,
                "stale_reason": reason,
                "stale_detected_at": now,
                "last_drive_status": "stale",
                "last_drive_message": message,
                "last_progress_message": message,
                "updated_at": now,
            }
        )
        self._record_drive_control_event(
            state,
            command="watchdog-stale",
            from_status=from_status,
            to_status="stale",
            reason=message,
        )
        append_jsonl(
            self.decision_log_path,
            {
                "at": now,
                "event": "drive_watchdog",
                "status": "stale",
                "reason": reason,
                "message": message,
                "pid": watchdog.get("pid"),
                "heartbeat_at": watchdog.get("heartbeat_at"),
                "heartbeat_age_seconds": watchdog.get("heartbeat_age_seconds"),
            },
        )
        return message

    def _drive_task_control_payload(self, task: HarnessTask, *, phase: str | None = None) -> dict[str, Any]:
        payload = {
            "id": task.id,
            "title": task.title,
            "milestone_id": task.milestone_id,
            "milestone_title": task.milestone_title,
        }
        if phase:
            payload["phase"] = phase
        return payload

    def _heartbeat_drive_control_in_state(
        self,
        state: dict[str, Any],
        *,
        activity: str,
        message: str | None = None,
        task: HarnessTask | None = None,
        phase: str | None = None,
        clear_task: bool = False,
        executor_watchdog: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        control = self._drive_control(state)
        if str(control.get("status", "idle")) != "running" or not bool(control.get("active")):
            return None
        current_pid = os.getpid()
        owner_pid = self._coerce_pid(control.get("pid"))
        if owner_pid is not None and owner_pid != current_pid:
            return None
        now = utc_now()
        control["pid"] = current_pid
        control["last_heartbeat_at"] = now
        control["heartbeat_count"] = int(control.get("heartbeat_count", 0) or 0) + 1
        control["current_activity"] = activity
        if task is not None:
            control["current_task"] = self._drive_task_control_payload(task, phase=phase)
        elif clear_task:
            control["current_task"] = None
        if message is not None:
            control["last_progress_message"] = message
        if executor_watchdog is not None:
            control["executor_watchdog"] = deepcopy(executor_watchdog)
        control["stale_reason"] = None
        control["stale_detected_at"] = None
        control["updated_at"] = now
        return control

    def drive_heartbeat(
        self,
        *,
        activity: str,
        message: str | None = None,
        task: HarnessTask | None = None,
        phase: str | None = None,
        clear_task: bool = False,
        executor_watchdog: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        state = self.load_state()
        control = self._heartbeat_drive_control_in_state(
            state,
            activity=activity,
            message=message,
            task=task,
            phase=phase,
            clear_task=clear_task,
            executor_watchdog=executor_watchdog,
        )
        if control is None:
            return None
        self.save_state(state)
        return deepcopy(control)

    def _record_drive_control_event(
        self,
        state: dict[str, Any],
        *,
        command: str,
        from_status: str,
        to_status: str,
        reason: str,
    ) -> dict[str, Any]:
        control = self._drive_control(state)
        event = {
            "at": utc_now(),
            "command": command,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
        }
        history = control.setdefault("history", [])
        if isinstance(history, list):
            history.append(event)
            control["history"] = history[-100:]
        return event

    def set_drive_control(self, command: str, *, reason: str = "manual") -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        from_status = str(control.get("status", "idle"))
        now = utc_now()
        if command == "pause":
            message = "drive pause requested"
            control.update(
                {
                    "status": "paused",
                    "active": False,
                    "pause_requested": True,
                    "cancel_requested": False,
                    "paused_at": now,
                    "reason": reason,
                    "last_progress_message": message,
                    "updated_at": now,
                }
            )
        elif command == "resume":
            watchdog = self._drive_watchdog_status(control)
            preflight = self._stale_running_preflight_from_control(
                control,
                watchdog=watchdog,
                reason="manual_resume",
            )
            if from_status == "running" and bool(control.get("active")) and preflight.get("status") == "recoverable":
                recovery = self._recover_stale_running_in_state(
                    state,
                    control,
                    preflight,
                    reason="manual_resume",
                )
                return {
                    "status": "idle",
                    "message": "stale running drive controls recovered; run `drive` to continue",
                    "drive_control": self._drive_control_summary_from_state(self.load_state()),
                    "stale_running_recovery": recovery,
                    "stale_running_preflight": recovery,
                }
            if from_status == "running" and bool(control.get("active")):
                summary = deepcopy(control)
                summary["watchdog"] = watchdog
                summary["stale"] = bool(watchdog.get("stale"))
                summary["stale_running_preflight"] = preflight
                summary["stale_running_block"] = preflight if preflight.get("status") == "blocked" else None
                return {
                    "status": "running",
                    "message": f"drive is already running; resume did not clear active state: {preflight.get('reason')}",
                    "drive_control": summary,
                    "stale_running_preflight": preflight,
                }
            message = "drive controls cleared; run `drive` to continue"
            control.update(
                {
                    "status": "idle",
                    "active": False,
                    "pause_requested": False,
                    "cancel_requested": False,
                    "pid": None,
                    "current_activity": "resume",
                    "current_task": None,
                    "executor_watchdog": None,
                    "last_progress_message": message,
                    "stale_reason": None,
                    "stale_detected_at": None,
                    "resumed_at": now,
                    "reason": reason,
                    "updated_at": now,
                }
            )
        elif command == "cancel":
            message = "drive cancel requested"
            control.update(
                {
                    "status": "cancelled",
                    "active": False,
                    "pause_requested": False,
                    "cancel_requested": True,
                    "cancelled_at": now,
                    "reason": reason,
                    "last_progress_message": message,
                    "updated_at": now,
                }
            )
        else:
            raise ValueError(f"unknown drive control command: {command}")
        self._record_drive_control_event(
            state,
            command=command,
            from_status=from_status,
            to_status=str(control["status"]),
            reason=reason,
        )
        self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": now,
                "event": "drive_control",
                "command": command,
                "from_status": from_status,
                "to_status": control["status"],
                "reason": reason,
            },
        )
        return {"status": control["status"], "message": message, "drive_control": deepcopy(control)}

    def start_drive(self, *, reason: str = "drive_command") -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        from_status = str(control.get("status", "idle"))
        watchdog = self._drive_watchdog_status(control)
        preflight = self._stale_running_preflight_from_control(control, watchdog=watchdog, reason=reason)
        recovery: dict[str, Any] | None = None
        if bool(control.get("pause_requested")) or from_status == "paused":
            return {
                "started": False,
                "status": "paused",
                "message": "drive is paused; run `resume` before starting another drive",
                "drive_control": self._drive_control_summary_from_state(state),
                "stale_running_preflight": preflight,
            }
        if bool(control.get("cancel_requested")) or from_status == "cancelled":
            return {
                "started": False,
                "status": "cancelled",
                "message": "drive is cancelled; run `resume` to clear the cancellation before driving again",
                "drive_control": self._drive_control_summary_from_state(state),
                "stale_running_preflight": preflight,
            }
        if preflight.get("status") == "recoverable":
            recovery = self._recover_stale_running_in_state(state, control, preflight, reason=reason)
            from_status = str(control.get("status", "idle"))
            watchdog = self._drive_watchdog_status(control)
            preflight = recovery
        elif preflight.get("status") == "blocked":
            summary = self._drive_control_summary_from_state(state)
            summary["stale_running_preflight"] = preflight
            summary["stale_running_block"] = preflight
            return {
                "started": False,
                "status": str(summary.get("status") or from_status or "running"),
                "message": f"drive is already running; stale recovery blocked: {preflight.get('reason')}",
                "drive_control": summary,
                "stale_running_preflight": preflight,
            }
        if (from_status == "running" or bool(control.get("active"))) and not watchdog.get("stale"):
            summary = deepcopy(control)
            summary["watchdog"] = watchdog
            summary["stale"] = False
            return {
                "started": False,
                "status": "running",
                "message": "drive is already running; inspect status or wait for the active drive to finish",
                "drive_control": summary,
                "stale_running_preflight": preflight,
            }
        if from_status == "stale" or watchdog.get("stale"):
            summary = self._drive_control_summary_from_state(state)
            return {
                "started": False,
                "status": "stale",
                "message": "stale drive state must be reviewed before starting another drive",
                "drive_control": summary,
                "stale_running_preflight": summary.get("stale_running_preflight"),
            }
        now = utc_now()
        control.update(
            {
                "schema_version": DRIVE_CONTROL_SCHEMA_VERSION,
                "status": "running",
                "active": True,
                "pause_requested": False,
                "cancel_requested": False,
                "pid": os.getpid(),
                "started_at": now,
                "last_heartbeat_at": now,
                "heartbeat_count": 1,
                "current_activity": "drive-starting",
                "current_task": None,
                "executor_watchdog": None,
                "last_progress_message": "drive started",
                "stale_reason": None,
                "stale_detected_at": None,
                "reason": reason,
                "updated_at": now,
            }
        )
        self._record_drive_control_event(
            state,
            command="start",
            from_status=from_status,
            to_status="running",
            reason=reason,
        )
        self.save_state(state)
        return {
            "started": True,
            "status": "running",
            "message": "drive started",
            "drive_control": self._drive_control_summary_from_state(state),
            "stale_running_recovery": recovery,
            "stale_running_preflight": preflight,
        }

    def finish_drive(self, *, status: str, message: str) -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        from_status = str(control.get("status", "idle"))
        now = utc_now()
        if bool(control.get("cancel_requested")) or status == "cancelled":
            to_status = "cancelled"
            pause_requested = False
            cancel_requested = True
        elif bool(control.get("pause_requested")) or status == "paused":
            to_status = "paused"
            pause_requested = True
            cancel_requested = False
        else:
            to_status = "idle"
            pause_requested = False
            cancel_requested = False
        control.update(
            {
                "schema_version": DRIVE_CONTROL_SCHEMA_VERSION,
                "status": to_status,
                "active": False,
                "pause_requested": pause_requested,
                "cancel_requested": cancel_requested,
                "pid": None,
                "last_heartbeat_at": now,
                "current_activity": "drive-finished",
                "current_task": None,
                "executor_watchdog": None,
                "last_progress_message": message,
                "stale_reason": None,
                "stale_detected_at": None,
                "last_drive_status": status,
                "last_drive_message": message,
                "finished_at": now,
                "updated_at": now,
            }
        )
        self._record_drive_control_event(
            state,
            command="finish",
            from_status=from_status,
            to_status=to_status,
            reason=message,
        )
        self.save_state(state)
        return deepcopy(control)

    def drive_control_summary(self) -> dict[str, Any]:
        state = self.load_state()
        return self._drive_control_summary_from_state(state)

    def _approval_queue(self, state: dict[str, Any]) -> dict[str, Any]:
        queue = state.setdefault("approval_queue", {})
        if not isinstance(queue, dict):
            queue = {}
            state["approval_queue"] = queue
        queue["schema_version"] = APPROVAL_QUEUE_SCHEMA_VERSION
        items = queue.setdefault("items", {})
        if not isinstance(items, dict):
            queue["items"] = {}
        queue.setdefault("updated_at", state.get("updated_at"))
        return queue

    def _approval_lease_ttl_seconds(self) -> int:
        candidates: list[Any] = []
        config = self.roadmap.get("approval_leases")
        if isinstance(config, dict):
            candidates.extend(
                [
                    config.get("ttl_seconds"),
                    config.get("lease_ttl_seconds"),
                    config.get("approval_lease_ttl_seconds"),
                ]
            )
        candidates.extend(
            [
                self.roadmap.get("approval_lease_ttl_seconds"),
                self.roadmap.get("approval_lease_ttl"),
            ]
        )
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                seconds = int(candidate)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                return seconds
        return DEFAULT_APPROVAL_LEASE_TTL_SECONDS

    def _approval_queue_summary_from_state(
        self,
        state: dict[str, Any],
        *,
        status_filter: str | None = "pending",
    ) -> dict[str, Any]:
        queue = self._approval_queue(state)
        items = [
            deepcopy(item)
            for item in queue.get("items", {}).values()
            if isinstance(item, dict) and (status_filter is None or str(item.get("status")) == status_filter)
        ]
        items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        all_items = [item for item in queue.get("items", {}).values() if isinstance(item, dict)]
        counts: dict[str, int] = {}
        stale_reasons: dict[str, int] = {}
        for item in all_items:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
            if status == "stale":
                reason = str(item.get("stale_reason") or "unknown")
                stale_reasons[reason] = stale_reasons.get(reason, 0) + 1
        return {
            "schema_version": APPROVAL_QUEUE_SCHEMA_VERSION,
            "path": self._project_relative_path(self.state_path),
            "status_filter": status_filter,
            "lease_ttl_seconds": self._approval_lease_ttl_seconds(),
            "counts": dict(sorted(counts.items())),
            "pending_count": counts.get("pending", 0),
            "approved_count": counts.get("approved", 0),
            "consumed_count": counts.get("consumed", 0),
            "stale_count": counts.get("stale", 0),
            "stale_reasons": dict(sorted(stale_reasons.items())),
            "items": items,
        }

    def approval_queue_summary(self, *, status_filter: str | None = "pending") -> dict[str, Any]:
        state = self.load_state()
        if self._refresh_approval_queue_staleness(state):
            self.save_state(state)
        return self._approval_queue_summary_from_state(state, status_filter=status_filter)

    def approve_approval(
        self,
        approval_id: str,
        *,
        approved_by: str = "local",
        reason: str = "manual approval",
    ) -> dict[str, Any]:
        state = self.load_state()
        queue = self._approval_queue(state)
        stale_changed = self._refresh_approval_queue_staleness(state)
        items = queue.setdefault("items", {})
        record = items.get(approval_id)
        if not isinstance(record, dict):
            if stale_changed:
                self.save_state(state)
            return {"status": "not_found", "message": f"approval not found: {approval_id}", "approval_id": approval_id}
        previous_status = str(record.get("status", "pending"))
        now = utc_now()
        if previous_status == "consumed":
            if stale_changed:
                self.save_state(state)
            return {
                "status": "consumed",
                "message": f"approval was already consumed: {approval_id}",
                "approval": deepcopy(record),
            }
        if previous_status == "stale":
            if stale_changed:
                self.save_state(state)
            reason_text = str(record.get("stale_reason") or "approval is stale")
            return {
                "status": "stale",
                "message": f"approval is stale and cannot be approved: {approval_id} ({reason_text})",
                "approval": deepcopy(record),
            }
        lease_ttl_seconds = int(record.get("lease_ttl_seconds") or self._approval_lease_ttl_seconds())
        record.update(
            {
                "status": "approved",
                "approved_at": now,
                "approved_by": approved_by,
                "approval_reason": reason,
                "lease_started_at": now,
                "lease_expires_at": self._approval_lease_expires_at(now, lease_ttl_seconds),
                "lease_ttl_seconds": lease_ttl_seconds,
                "updated_at": now,
            }
        )
        queue["updated_at"] = now
        task_id = str(record.get("task_id", ""))
        task_state = state.setdefault("tasks", {}).get(task_id)
        if isinstance(task_state, dict) and str(task_state.get("status")) == "blocked":
            task_state["status"] = "pending"
            task_state["approval_unblocked_at"] = now
            task_state["approval_unblocked_by"] = approval_id
            task_state["blocked_on_approval"] = False
            task_state.pop("failure_isolation", None)
        self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": now,
                "event": "approval",
                "approval_id": approval_id,
                "task_id": task_id,
                "status": "approved",
                "previous_status": previous_status,
                "approved_by": approved_by,
                "reason": reason,
            },
        )
        return {"status": "approved", "message": f"approval recorded: {approval_id}", "approval": deepcopy(record)}

    def approve_all_pending(
        self,
        *,
        approved_by: str = "local",
        reason: str = "manual approval",
    ) -> dict[str, Any]:
        pending = self.approval_queue_summary(status_filter="pending")["items"]
        results = [
            self.approve_approval(str(item["id"]), approved_by=approved_by, reason=reason)
            for item in pending
            if item.get("id")
        ]
        return {
            "status": "approved",
            "message": f"approved {len(results)} pending approval(s)",
            "approved_count": len(results),
            "approvals": results,
        }

    def _approval_phase_key(self, phase: str | None) -> str:
        value = str(phase or "task")
        if value.startswith("acceptance-"):
            return "acceptance"
        if value.startswith("repair-"):
            return "repair"
        return value

    def _approval_flag_for_decision_kind(self, decision_kind: str) -> str:
        return {
            "manual_approval": "--allow-manual",
            "agent_approval": "--allow-agent",
            "executor_approval": "--allow-agent",
            "live_approval": "--allow-live",
        }.get(decision_kind, "")

    def _approval_json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._approval_json_safe(value[key]) for key in sorted(value)}
        if isinstance(value, (list, tuple)):
            return [self._approval_json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return redact(value) if isinstance(value, str) else value
        return redact(str(value))

    def _approval_digest(self, value: Any) -> str:
        serialized = json.dumps(
            self._approval_json_safe(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _approval_text_digest(self, value: Any) -> str | None:
        if value is None:
            return None
        return hashlib.sha256(redact(str(value)).encode("utf-8")).hexdigest()

    def _approval_command_fingerprint_payload(self, command: AcceptanceCommand | dict[str, Any]) -> dict[str, Any]:
        if isinstance(command, AcceptanceCommand):
            name = command.name
            command_text = command.command
            prompt = command.prompt
            required = command.required
            timeout_seconds = command.timeout_seconds
            no_progress_timeout_seconds = command.no_progress_timeout_seconds
            model = command.model
            sandbox = command.sandbox
            executor = command.executor
            requested_capabilities = list(command.requested_capabilities)
            user_experience_gate = deepcopy(command.user_experience_gate)
            spec_refs = list(command.spec_refs)
        else:
            name = str(command.get("name") or "")
            command_text = command.get("command")
            prompt = command.get("prompt")
            required = bool(command.get("required", True))
            timeout_seconds = command.get("timeout_seconds")
            no_progress_timeout_seconds = command.get("no_progress_timeout_seconds", command.get("no_progress_seconds"))
            model = command.get("model")
            sandbox = command.get("sandbox")
            executor = str(command.get("executor") or "")
            requested_capabilities = self._normalize_requested_capabilities(command.get("requested_capabilities"))
            user_experience_gate = (
                deepcopy(command.get("user_experience_gate"))
                if isinstance(command.get("user_experience_gate"), dict)
                else {}
            )
            spec_refs = list(self._normalize_spec_refs(command.get("spec_refs")))
        return {
            "name": name,
            "executor": executor,
            "required": required,
            "timeout_seconds": timeout_seconds,
            "no_progress_timeout_seconds": no_progress_timeout_seconds,
            "model": model,
            "sandbox": sandbox,
            "requested_capabilities": list(requested_capabilities),
            "user_experience_gate": user_experience_gate,
            "spec_refs": spec_refs,
            "command_sha256": self._approval_text_digest(command_text),
            "prompt_sha256": self._approval_text_digest(prompt),
            "has_command": command_text is not None,
            "has_prompt": prompt is not None,
        }

    def _approval_task_command_inventory(self, task: HarnessTask) -> dict[str, Any]:
        groups = {
            "implementation": task.implementation,
            "repair": task.repair,
            "acceptance": task.acceptance,
            "e2e": task.e2e,
        }
        return {
            phase: [self._approval_command_fingerprint_payload(command) for command in commands]
            for phase, commands in groups.items()
        }

    def _approval_policy_metadata(
        self,
        *,
        decision_kind: str,
        decision_metadata: dict[str, Any] | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command_policy_payload = {
            "profile": self.command_policy.get("profile") or self.roadmap.get("profile"),
            "version": self.command_policy.get("version"),
            "allowed_prefixes": self.command_policy.get("allowed_prefixes", []),
            "blocked_patterns": self.command_policy.get("blocked_patterns", []),
            "requires_live_flag_patterns": self.command_policy.get("requires_live_flag_patterns", []),
        }
        executor_summary: dict[str, Any] = {}
        if isinstance(executor_metadata, dict):
            executor_summary = {
                key: executor_metadata.get(key)
                for key in (
                    "id",
                    "kind",
                    "input_mode",
                    "uses_command_policy",
                    "requires_agent_approval",
                )
                if key in executor_metadata
            }
        return {
            "decision_kind": decision_kind,
            "command_policy": {
                "profile": command_policy_payload["profile"],
                "version": command_policy_payload["version"],
                "sha256": self._approval_digest(command_policy_payload),
            },
            "decision_metadata": self._approval_json_safe(decision_metadata or {}),
            "executor": self._approval_json_safe(executor_summary),
        }

    def _approval_fingerprint_payload(
        self,
        task: HarnessTask,
        *,
        decision_kind: str,
        phase: str,
        approval_flag: str,
        command: AcceptanceCommand | dict[str, Any] | None = None,
        executor_metadata: dict[str, Any] | None = None,
        decision_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        phase_key = self._approval_phase_key(phase)
        approval_kind = APPROVAL_DECISION_KINDS.get(decision_kind, "unknown")
        inventory = self._approval_task_command_inventory(task)
        payload: dict[str, Any] = {
            "schema_version": APPROVAL_FINGERPRINT_SCHEMA_VERSION,
            "project": {
                "root": str(self.project_root),
                "roadmap_path": self._project_relative_path(self.roadmap_path) if self.roadmap_path else None,
            },
            "task": {
                "id": task.id,
                "milestone_id": task.milestone_id,
                "manual_approval_required": task.manual_approval_required,
                "agent_approval_required": task.agent_approval_required,
            },
            "phase": phase_key,
            "approval": {
                "decision_kind": decision_kind,
                "approval_kind": approval_kind,
                "approval_flag": approval_flag,
            },
            "file_scope": sorted({str(item) for item in task.file_scope}),
            "policy": self._approval_policy_metadata(
                decision_kind=decision_kind,
                decision_metadata=decision_metadata,
                executor_metadata=executor_metadata,
            ),
        }
        if command is None:
            payload["task_command_inventory_sha256"] = self._approval_digest(inventory)
            payload["task_command_counts"] = {
                phase_name: len(commands)
                for phase_name, commands in (
                    ("implementation", task.implementation),
                    ("repair", task.repair),
                    ("acceptance", task.acceptance),
                    ("e2e", task.e2e),
                )
            }
        else:
            payload["command"] = self._approval_command_fingerprint_payload(command)
        return payload

    def _approval_fingerprint(self, payload: dict[str, Any]) -> str:
        return self._approval_digest(payload)

    def _approval_record_id(
        self,
        task: HarnessTask,
        *,
        decision_kind: str,
        approval_kind: str,
        phase: str,
        name: str,
        executor: str,
        approval_flag: str,
        approval_fingerprint: str,
    ) -> str:
        identity = {
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "approval_kind": approval_kind,
            "decision_kind": decision_kind,
            "phase": self._approval_phase_key(phase),
            "name": name,
            "executor": executor,
            "approval_flag": approval_flag,
            "approval_fingerprint": approval_fingerprint,
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        label_parts = [task.id, approval_kind, self._approval_phase_key(phase)]
        if name:
            label_parts.append(name)
        elif executor:
            label_parts.append(executor)
        return f"{self._slugify('-'.join(label_parts))}-{digest}"

    def _approval_current_decision_metadata(
        self,
        *,
        decision_kind: str,
        command: AcceptanceCommand | None = None,
    ) -> dict[str, Any]:
        if decision_kind == "live_approval" and command is not None:
            return {"matched_live_patterns": self._live_policy_matches(command.command)}
        return {}

    def _approval_current_identity(
        self,
        task: HarnessTask,
        *,
        decision_kind: str,
        phase: str = "task",
        command: AcceptanceCommand | None = None,
        name: str | None = None,
        executor: str | None = None,
        approval_flag: str | None = None,
        decision_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        approval_kind = APPROVAL_DECISION_KINDS.get(decision_kind)
        if approval_kind is None:
            return None
        phase_key = self._approval_phase_key(phase)
        command_name = str(name if name is not None else (command.name if command is not None else ""))
        executor_id = str(executor if executor is not None else (command.executor if command is not None else ""))
        flag = str(approval_flag or self._approval_flag_for_decision_kind(decision_kind))
        executor_metadata = self.executor_registry.metadata_for(command.executor) if command is not None else None
        metadata = decision_metadata
        if metadata is None:
            metadata = self._approval_current_decision_metadata(decision_kind=decision_kind, command=command)
        fingerprint_payload = self._approval_fingerprint_payload(
            task,
            decision_kind=decision_kind,
            phase=phase_key,
            approval_flag=flag,
            command=command,
            executor_metadata=executor_metadata,
            decision_metadata=metadata,
        )
        approval_fingerprint = self._approval_fingerprint(fingerprint_payload)
        return {
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "approval_kind": approval_kind,
            "decision_kind": decision_kind,
            "phase": phase_key,
            "name": command_name,
            "executor": executor_id,
            "approval_flag": flag,
            "approval_fingerprint": approval_fingerprint,
            "approval_fingerprint_version": APPROVAL_FINGERPRINT_SCHEMA_VERSION,
            "approval_fingerprint_payload": fingerprint_payload,
            "id": self._approval_record_id(
                task,
                decision_kind=decision_kind,
                approval_kind=approval_kind,
                phase=phase_key,
                name=command_name,
                executor=executor_id,
                approval_flag=flag,
                approval_fingerprint=approval_fingerprint,
            ),
        }

    def _approval_find_command(
        self,
        task: HarnessTask,
        *,
        phase: str,
        name: str | None = None,
        executor: str | None = None,
    ) -> AcceptanceCommand | None:
        phase_key = self._approval_phase_key(phase)
        for group_phase, commands in (
            ("implementation", task.implementation),
            ("repair", task.repair),
            ("acceptance", task.acceptance),
            ("e2e", task.e2e),
        ):
            if self._approval_phase_key(group_phase) != phase_key:
                continue
            for command in commands:
                if name is not None and str(command.name) != str(name):
                    continue
                if executor is not None and str(command.executor) != str(executor):
                    continue
                return command
        return None

    def _approval_current_identity_from_record(
        self,
        record: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        task_id = str(record.get("task_id") or "")
        task = self.task_by_id(task_id)
        if task is None:
            return None, "approval task no longer exists"
        decision_kind = str(record.get("decision_kind") or "")
        phase = str(record.get("phase") or "task")
        name = str(record.get("name") or "")
        executor = str(record.get("executor") or "")
        command: AcceptanceCommand | None = None
        if name or decision_kind in {"executor_approval", "live_approval"}:
            command = self._approval_find_command(
                task,
                phase=phase,
                name=name or None,
                executor=executor or None,
            )
            if command is None and name:
                command = self._approval_find_command(
                    task,
                    phase=phase,
                    name=name,
                    executor=None,
                )
            if command is None:
                return None, "approval policy target no longer exists"
        current = self._approval_current_identity(
            task,
            decision_kind=decision_kind,
            phase=phase,
            command=command,
            name=name or None,
            executor=executor or None,
            approval_flag=str(record.get("approval_flag") or self._approval_flag_for_decision_kind(decision_kind)),
        )
        if current is None:
            return None, "approval decision kind is no longer recognized"
        return current, None

    def _approval_lease_expires_at(self, started_at: str, ttl_seconds: int) -> str:
        started_dt = parse_utc_timestamp(started_at) or datetime.now(timezone.utc)
        return format_utc_timestamp(started_dt + timedelta(seconds=ttl_seconds))

    def _approval_expired_reason(self, record: dict[str, Any], *, now: str) -> str | None:
        if str(record.get("status")) != "approved":
            return None
        expires_at = str(record.get("lease_expires_at") or "")
        expires_dt = parse_utc_timestamp(expires_at)
        if expires_dt is None:
            return "approval lease missing expiration timestamp"
        now_dt = parse_utc_timestamp(now) or datetime.now(timezone.utc)
        if now_dt >= expires_dt:
            return f"approval lease expired at {expires_at}"
        return None

    def _mark_approval_stale(
        self,
        record: dict[str, Any],
        *,
        reason: str,
        now: str,
        current_fingerprint: str | None = None,
    ) -> bool:
        previous_status = str(record.get("status", "unknown"))
        if previous_status == "stale":
            return False
        record.update(
            {
                "status": "stale",
                "previous_status": previous_status,
                "stale_at": now,
                "stale_reason": reason,
                "updated_at": now,
            }
        )
        if current_fingerprint is not None:
            record["current_approval_fingerprint"] = current_fingerprint
        append_jsonl(
            self.decision_log_path,
            {
                "at": now,
                "event": "approval",
                "approval_id": record.get("id"),
                "task_id": record.get("task_id"),
                "status": "stale",
                "previous_status": previous_status,
                "reason": reason,
            },
        )
        return True

    def _refresh_approval_queue_staleness(self, state: dict[str, Any]) -> bool:
        queue = self._approval_queue(state)
        items = queue.setdefault("items", {})
        now = utc_now()
        changed = False
        for record in list(items.values()):
            if not isinstance(record, dict) or str(record.get("status")) not in {"pending", "approved"}:
                continue
            expired_reason = self._approval_expired_reason(record, now=now)
            if expired_reason is not None:
                changed = self._mark_approval_stale(record, reason=expired_reason, now=now) or changed
                continue
            current, missing_reason = self._approval_current_identity_from_record(record)
            if current is None:
                changed = self._mark_approval_stale(
                    record,
                    reason=str(missing_reason or "approval policy target is no longer current"),
                    now=now,
                ) or changed
                continue
            current_fingerprint = str(current.get("approval_fingerprint") or "")
            record_fingerprint = str(record.get("approval_fingerprint") or "")
            if not record_fingerprint:
                changed = self._mark_approval_stale(
                    record,
                    reason="approval missing fingerprint metadata",
                    now=now,
                    current_fingerprint=current_fingerprint,
                ) or changed
                continue
            if record_fingerprint != current_fingerprint:
                changed = self._mark_approval_stale(
                    record,
                    reason="approval fingerprint mismatch: current policy decision changed",
                    now=now,
                    current_fingerprint=current_fingerprint,
                ) or changed
        if changed:
            queue["updated_at"] = now
        return changed

    def _approval_identity_from_decision(self, task: HarnessTask, decision: dict[str, Any]) -> dict[str, Any] | None:
        decision_kind = str(decision.get("kind", ""))
        approval_kind = APPROVAL_DECISION_KINDS.get(decision_kind)
        if approval_kind is None:
            return None
        policy_input = decision.get("input") if isinstance(decision.get("input"), dict) else {}
        command = policy_input.get("command") if isinstance(policy_input.get("command"), dict) else {}
        executor_metadata = policy_input.get("executor") if isinstance(policy_input.get("executor"), dict) else None
        phase = self._approval_phase_key(str(decision.get("phase") or policy_input.get("phase") or "task"))
        name = str(decision.get("name") or command.get("name") or "")
        executor = str(decision.get("executor") or command.get("executor") or "")
        approval_flag = str(decision.get("approval_flag") or "")
        command_payload = command if command else None
        fingerprint_payload = self._approval_fingerprint_payload(
            task,
            decision_kind=decision_kind,
            phase=phase,
            approval_flag=approval_flag,
            command=command_payload,
            executor_metadata=executor_metadata,
            decision_metadata=decision.get("metadata") if isinstance(decision.get("metadata"), dict) else {},
        )
        approval_fingerprint = self._approval_fingerprint(fingerprint_payload)
        return {
            "id": self._approval_record_id(
                task,
                decision_kind=decision_kind,
                approval_kind=approval_kind,
                phase=phase,
                name=name,
                executor=executor,
                approval_flag=approval_flag,
                approval_fingerprint=approval_fingerprint,
            ),
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "approval_kind": approval_kind,
            "decision_kind": decision_kind,
            "phase": phase,
            "name": name,
            "executor": executor,
            "approval_flag": approval_flag,
            "approval_fingerprint": approval_fingerprint,
            "approval_fingerprint_version": APPROVAL_FINGERPRINT_SCHEMA_VERSION,
            "approval_fingerprint_payload": fingerprint_payload,
        }

    def _approval_is_approved(
        self,
        task: HarnessTask,
        *,
        decision_kind: str,
        phase: str = "task",
        name: str | None = None,
        executor: str | None = None,
        command: AcceptanceCommand | None = None,
        state: dict[str, Any] | None = None,
        mutate_stale: bool = True,
    ) -> bool:
        state_payload = state if state is not None else self.load_state()
        queue = self._approval_queue(state_payload)
        items = queue.get("items", {})
        phase_key = self._approval_phase_key(phase)
        current = self._approval_current_identity(
            task,
            decision_kind=decision_kind,
            phase=phase_key,
            command=command,
            name=name,
            executor=executor,
        )
        if current is None:
            return False
        current_fingerprint = str(current.get("approval_fingerprint") or "")
        changed = False
        now = utc_now()
        for item in items.values():
            if not isinstance(item, dict) or str(item.get("status")) not in {"approved", "pending"}:
                continue
            if str(item.get("task_id")) != task.id:
                continue
            if str(item.get("decision_kind")) != decision_kind:
                continue
            if str(item.get("phase", "task")) != phase_key:
                continue
            item_name = str(item.get("name", ""))
            if name is not None and item_name != name:
                continue
            if name is None and item_name:
                continue
            if executor is not None and str(item.get("executor", "")) != executor:
                if mutate_stale:
                    changed = self._mark_approval_stale(
                        item,
                        reason="approval fingerprint mismatch: current policy decision changed",
                        now=now,
                        current_fingerprint=current_fingerprint,
                    ) or changed
                continue
            if str(item.get("status")) != "approved":
                continue
            expired_reason = self._approval_expired_reason(item, now=now)
            if expired_reason is not None:
                if mutate_stale:
                    changed = self._mark_approval_stale(item, reason=expired_reason, now=now) or changed
                continue
            record_fingerprint = str(item.get("approval_fingerprint") or "")
            if not record_fingerprint:
                if mutate_stale:
                    changed = self._mark_approval_stale(
                        item,
                        reason="approval missing fingerprint metadata",
                        now=now,
                        current_fingerprint=current_fingerprint,
                    ) or changed
                continue
            if record_fingerprint != current_fingerprint:
                if mutate_stale:
                    changed = self._mark_approval_stale(
                        item,
                        reason="approval fingerprint mismatch: current policy decision changed",
                        now=now,
                        current_fingerprint=current_fingerprint,
                    ) or changed
                continue
            if changed:
                queue["updated_at"] = now
                if state is None:
                    self.save_state(state_payload)
            return True
        if changed:
            queue["updated_at"] = now
            if state is None:
                self.save_state(state_payload)
        return False

    def _approval_record_matches_identity(self, record: dict[str, Any], identity: dict[str, Any]) -> bool:
        for key in (
            "task_id",
            "milestone_id",
            "approval_kind",
            "decision_kind",
            "phase",
            "name",
            "executor",
            "approval_flag",
            "approval_fingerprint",
        ):
            if str(record.get(key, "")) != str(identity.get(key, "")):
                return False
        return True

    def _active_approval_id_for_identity(
        self,
        items: dict[str, Any],
        identity: dict[str, Any],
    ) -> str | None:
        for approval_id, record in sorted(items.items()):
            if not isinstance(record, dict):
                continue
            if str(record.get("status")) not in {"pending", "approved"}:
                continue
            if self._approval_record_matches_identity(record, identity):
                return str(approval_id)
        return None

    def _unique_approval_id(self, items: dict[str, Any], approval_id: str) -> str:
        if approval_id not in items:
            return approval_id
        counter = 2
        while f"{approval_id}-{counter}" in items:
            counter += 1
        return f"{approval_id}-{counter}"

    def _queue_required_approvals(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self._refresh_approval_queue_staleness(state)
        queue = self._approval_queue(state)
        items = queue.setdefault("items", {})
        created_or_updated: list[dict[str, Any]] = []
        now = utc_now()
        for decision in decisions:
            if not decision.get("requires_approval") or decision.get("outcome") != "requires_approval":
                continue
            identity = self._approval_identity_from_decision(task, decision)
            if identity is None:
                continue
            approval_id = self._active_approval_id_for_identity(items, identity) or str(identity["id"])
            existing = items.get(approval_id)
            if isinstance(existing, dict) and str(existing.get("status")) in {"pending", "approved"}:
                existing.update(
                    {
                        "last_seen_at": now,
                        "reason": decision.get("reason"),
                        "policy_decision": self._compact_policy_decision(decision),
                        "lease_ttl_seconds": int(existing.get("lease_ttl_seconds") or self._approval_lease_ttl_seconds()),
                        "updated_at": now,
                    }
                )
                created_or_updated.append(deepcopy(existing))
                continue
            approval_id = self._unique_approval_id(items, approval_id)
            identity["id"] = approval_id
            record = {
                "schema_version": APPROVAL_QUEUE_SCHEMA_VERSION,
                **identity,
                "status": "pending",
                "reason": decision.get("reason"),
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "lease_ttl_seconds": self._approval_lease_ttl_seconds(),
                "lease_started_at": None,
                "lease_expires_at": None,
                "source": "policy",
                "policy_decision": self._compact_policy_decision(decision),
            }
            items[approval_id] = record
            created_or_updated.append(deepcopy(record))
            append_jsonl(
                self.decision_log_path,
                {
                    "at": now,
                    "event": "approval",
                    "approval_id": approval_id,
                    "task_id": task.id,
                    "status": "pending",
                    "decision_kind": identity["decision_kind"],
                    "approval_kind": identity["approval_kind"],
                    "reason": decision.get("reason"),
                },
            )
        if created_or_updated:
            queue["updated_at"] = now
        return created_or_updated

    def _consume_task_approvals(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        *,
        status: str,
    ) -> list[dict[str, Any]]:
        queue = self._approval_queue(state)
        items = queue.setdefault("items", {})
        now = utc_now()
        consumed: list[dict[str, Any]] = []
        for item in items.values():
            if not isinstance(item, dict):
                continue
            if str(item.get("task_id")) != task.id or str(item.get("status")) not in {"pending", "approved"}:
                continue
            item.update(
                {
                    "status": "consumed",
                    "consumed_at": now,
                    "consumed_by_status": status,
                    "updated_at": now,
                }
            )
            consumed.append(deepcopy(item))
        if consumed:
            queue["updated_at"] = now
        return consumed

    def _record_phase_state(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        *,
        phase: str,
        event: str,
        status: str,
        persist: bool,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        runs: list[CommandRun] | None = None,
    ) -> dict[str, Any] | None:
        if not persist:
            return None
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        history = task_state.setdefault("phase_history", [])
        if not isinstance(history, list):
            history = []
            task_state["phase_history"] = history
        sequence = self._next_phase_sequence(task_state, history)
        recorded_at = utc_now()
        payload: dict[str, Any] = {
            "schema_version": PHASE_STATE_SCHEMA_VERSION,
            "sequence": sequence,
            "recorded_at": recorded_at,
            "event": event,
            "phase": phase,
            "status": status,
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "task_attempt": int(task_state.get("attempts", 0)),
        }
        if message is not None:
            payload["message"] = message
        if metadata:
            payload["metadata"] = metadata
        if runs is not None:
            payload["runs"] = [self._command_run_state_summary(run) for run in runs]
        history.append(payload)
        task_state["phase_sequence"] = sequence
        task_state["last_phase_event"] = payload
        phase_states = task_state.setdefault("phase_states", {})
        if isinstance(phase_states, dict):
            phase_states[phase] = payload
        if event == "before":
            task_state["current_phase"] = payload
        else:
            current_phase = task_state.get("current_phase")
            if isinstance(current_phase, dict) and current_phase.get("phase") == phase:
                task_state["current_phase"] = None
        progress_message = message or f"{phase} {event} {status}"
        self._heartbeat_drive_control_in_state(
            state,
            activity=f"{phase}:{event}",
            message=progress_message,
            task=task,
            phase=phase,
        )
        self.save_state(state)
        return payload

    def _next_phase_sequence(self, task_state: dict[str, Any], history: list[Any]) -> int:
        current = int(task_state.get("phase_sequence", 0) or 0)
        for item in history:
            if isinstance(item, dict):
                try:
                    current = max(current, int(item.get("sequence", 0) or 0))
                except (TypeError, ValueError):
                    continue
        return current + 1

    def _command_run_state_summary(self, run: CommandRun) -> dict[str, Any]:
        return {
            "phase": run.phase,
            "name": run.name,
            "status": run.status,
            "returncode": run.returncode,
            "executor": run.executor,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "executor_watchdog": run.result_metadata.get("watchdog") if isinstance(run.result_metadata, dict) else None,
        }

    def _replay_guard_is_enabled_for_task_state(self, task_state: dict[str, Any]) -> bool:
        status = str(task_state.get("status", "") or "").strip()
        if status in COMPLETED_STATUSES or status in ISOLATED_FAILURE_STATUSES or status in BLOCKED_STATUSES:
            return False
        if task_state.get("last_finished_at"):
            return False
        return True

    def _phase_replay_guard_decision(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        *,
        phase: str,
        commands: tuple[AcceptanceCommand, ...],
    ) -> dict[str, Any]:
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        fingerprint = self._command_group_fingerprint(commands)
        payload: dict[str, Any] = {
            "schema_version": REPLAY_GUARD_SCHEMA_VERSION,
            "kind": "engineering-harness.phase-replay-guard",
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "phase": phase,
            "status": "not_reused",
            "action": "run",
            "reason": "missing_passed_phase",
            "command_count": len(commands),
            "command_group_fingerprint": fingerprint,
            "checked_at": utc_now(),
        }
        if not commands:
            payload["reason"] = "empty_command_group"
            return payload
        if not self._replay_guard_is_enabled_for_task_state(task_state):
            payload["reason"] = "task_status_requires_fresh_execution"
            payload["task_status"] = task_state.get("status")
            return payload

        latest_for_phase: dict[str, Any] | None = None
        latest_same_fingerprint: dict[str, Any] | None = None
        history = task_state.get("phase_history", [])
        if not isinstance(history, list):
            history = []
        phase_states = task_state.get("phase_states")
        phase_state = (
            phase_states.get(phase)
            if isinstance(phase_states, dict) and isinstance(phase_states.get(phase), dict)
            else None
        )
        if phase_state is not None:
            phase_state_sequence = phase_state.get("sequence")
            has_phase_state = any(
                isinstance(item, dict)
                and item.get("phase") == phase
                and item.get("sequence") == phase_state_sequence
                for item in history
            )
            if not has_phase_state:
                history = [*history, phase_state]
        for item in reversed(history):
            if not isinstance(item, dict) or item.get("phase") != phase:
                continue
            latest_for_phase = latest_for_phase or item
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            item_fingerprint = str(metadata.get("command_group_fingerprint") or "")
            if item_fingerprint == fingerprint:
                latest_same_fingerprint = item
                if item.get("status") == "passed" and item.get("event") in {"after", "reused"}:
                    payload.update(
                        {
                            "status": "reused",
                            "action": "reuse",
                            "reason": "passed_phase_for_current_definition",
                            "source_sequence": item.get("sequence"),
                            "source_event": item.get("event"),
                            "source_status": item.get("status"),
                            "source_recorded_at": item.get("recorded_at"),
                            "source_task_attempt": item.get("task_attempt"),
                        }
                    )
                    replay_metadata = (
                        metadata.get("replay_guard")
                        if isinstance(metadata.get("replay_guard"), dict)
                        else None
                    )
                    if replay_metadata is not None:
                        payload["source_replay_guard"] = deepcopy(replay_metadata)
                    return payload
        if latest_same_fingerprint is not None:
            payload.update(
                {
                    "reason": "latest_matching_phase_not_passed",
                    "latest_matching_sequence": latest_same_fingerprint.get("sequence"),
                    "latest_matching_event": latest_same_fingerprint.get("event"),
                    "latest_matching_status": latest_same_fingerprint.get("status"),
                }
            )
            return payload
        if latest_for_phase is not None:
            latest_metadata = (
                latest_for_phase.get("metadata")
                if isinstance(latest_for_phase.get("metadata"), dict)
                else {}
            )
            payload.update(
                {
                    "reason": "command_group_changed",
                    "latest_sequence": latest_for_phase.get("sequence"),
                    "latest_event": latest_for_phase.get("event"),
                    "latest_status": latest_for_phase.get("status"),
                    "latest_command_group_fingerprint": latest_metadata.get("command_group_fingerprint"),
                }
            )
        return payload

    def _record_replay_guard_decision(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        decision: dict[str, Any],
    ) -> None:
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        history = task_state.setdefault("replay_guard_history", [])
        if not isinstance(history, list):
            history = []
            task_state["replay_guard_history"] = history
        history.append(deepcopy(decision))
        task_state["replay_guard_history"] = history[-REPLAY_GUARD_SUMMARY_LIMIT:]
        task_state["last_replay_guard"] = deepcopy(decision)

    def _reuse_command_group_from_replay_guard(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        *,
        phase: str,
        commands: tuple[AcceptanceCommand, ...],
        runs: list[CommandRun],
        decision: dict[str, Any],
        persist_state: bool,
    ) -> tuple[str, str]:
        started_at = utc_now()
        phase_runs = [
            CommandRun(
                phase,
                command.name,
                self._display_command(command, task),
                "reused",
                0,
                started_at,
                started_at,
                "",
                "",
                executor=command.executor,
                executor_metadata=self.executor_registry.metadata_for(command.executor),
                result_metadata={"replay_guard": deepcopy(decision)},
            )
            for command in commands
        ]
        runs.extend(phase_runs)
        message = f"Reused passed {phase} phase from durable replay guard evidence."
        if persist_state:
            metadata = self._command_group_state_metadata(commands)
            metadata["replay_guard"] = deepcopy(decision)
            self._record_phase_state(
                state,
                task,
                phase=phase,
                event="reused",
                status="passed",
                message=message,
                persist=True,
                metadata=metadata,
                runs=phase_runs,
            )
            self._record_replay_guard_decision(state, task, decision)
            self.save_state(state)
        return "passed", message

    def _new_replay_guard_summary(self, task: HarnessTask) -> dict[str, Any]:
        return {
            "schema_version": REPLAY_GUARD_SCHEMA_VERSION,
            "kind": "engineering-harness.task-replay-guard",
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "status": "not_used",
            "checked_at": utc_now(),
            "decisions": [],
            "reused_phases": [],
        }

    def _append_replay_guard_decision(
        self,
        summary: dict[str, Any],
        decision: dict[str, Any],
    ) -> None:
        decisions = summary.setdefault("decisions", [])
        if isinstance(decisions, list):
            decisions.append(deepcopy(decision))
        if decision.get("status") == "reused":
            reused = summary.setdefault("reused_phases", [])
            if isinstance(reused, list):
                reused.append(deepcopy(decision))
            summary["status"] = "reused"

    def _finalize_replay_guard_summary(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        decisions = summary.get("decisions")
        reused = summary.get("reused_phases")
        if not isinstance(decisions, list) or not decisions:
            return None
        if not isinstance(reused, list):
            reused = []
            summary["reused_phases"] = reused
        summary["decision_count"] = len(decisions)
        summary["reused_phase_count"] = len(reused)
        if not reused and summary.get("status") != "reused":
            summary["status"] = "evaluated"
        return summary

    def replay_guard_summary(self) -> dict[str, Any]:
        state = self.load_state()
        tasks_payload: dict[str, Any] = {}
        reused_phases: list[dict[str, Any]] = []
        for task_id, task_state in sorted(state.get("tasks", {}).items()):
            if not isinstance(task_state, dict):
                continue
            history = task_state.get("replay_guard_history", [])
            if not isinstance(history, list):
                history = []
            task_reused = [
                deepcopy(item)
                for item in history
                if isinstance(item, dict) and item.get("status") == "reused"
            ]
            last_replay_guard = (
                deepcopy(task_state.get("last_replay_guard"))
                if isinstance(task_state.get("last_replay_guard"), dict)
                else None
            )
            if not task_reused and last_replay_guard is None:
                continue
            task_payload: dict[str, Any] = {
                "reused_phase_count": len(task_reused),
                "reused_phases": task_reused[-REPLAY_GUARD_SUMMARY_LIMIT:],
            }
            if last_replay_guard is not None:
                task_payload["last_replay_guard"] = last_replay_guard
            tasks_payload[str(task_id)] = task_payload
            reused_phases.extend(task_reused)
        reused_phases.sort(
            key=lambda item: (
                str(item.get("checked_at") or ""),
                str(item.get("task_id") or ""),
                str(item.get("phase") or ""),
            ),
            reverse=True,
        )
        return {
            "schema_version": REPLAY_GUARD_SCHEMA_VERSION,
            "kind": "engineering-harness.replay-guard-summary",
            "status": "reused" if reused_phases else "none",
            "reused_phase_count": len(reused_phases),
            "reused_phases": reused_phases[:REPLAY_GUARD_SUMMARY_LIMIT],
            "tasks": tasks_payload,
        }

    def _command_group_state_metadata(self, commands: tuple[AcceptanceCommand, ...]) -> dict[str, Any]:
        command_fingerprints = self._command_group_command_fingerprints(commands)
        return {
            "command_count": len(commands),
            "command_group_fingerprint": self._command_group_fingerprint(commands),
            "fingerprint_algorithm": "sha256",
            "fingerprint_fields": [
                "name",
                "command",
                "prompt",
                "executor",
                "required",
                "timeout_seconds",
                "no_progress_timeout_seconds",
                "model",
                "sandbox",
                "requested_capabilities",
                "user_experience_gate",
                "spec_refs",
            ],
            "commands": [
                {
                    "name": command.name,
                    "executor": command.executor,
                    "required": command.required,
                    "timeout_seconds": command.timeout_seconds,
                    "no_progress_timeout_seconds": command.no_progress_timeout_seconds,
                    "has_command": command.command is not None,
                    "has_prompt": command.prompt is not None,
                    "command_sha256": command_fingerprints[index]["command_sha256"],
                    "prompt_sha256": command_fingerprints[index]["prompt_sha256"],
                    "model": command.model,
                    "sandbox": command.sandbox,
                    "requested_capabilities": list(command.requested_capabilities),
                    "user_experience_gate": deepcopy(command.user_experience_gate),
                    "spec_refs": list(command.spec_refs),
                }
                for index, command in enumerate(commands)
            ],
        }

    def _command_group_definition(self, commands: tuple[AcceptanceCommand, ...]) -> list[dict[str, Any]]:
        return [
            {
                "name": command.name,
                "command": command.command,
                "prompt": command.prompt,
                "executor": command.executor,
                "required": command.required,
                "timeout_seconds": command.timeout_seconds,
                "no_progress_timeout_seconds": command.no_progress_timeout_seconds,
                "model": command.model,
                "sandbox": command.sandbox,
                "requested_capabilities": list(command.requested_capabilities),
                "user_experience_gate": deepcopy(command.user_experience_gate),
                "spec_refs": list(command.spec_refs),
            }
            for command in commands
        ]

    def _command_group_fingerprint(self, commands: tuple[AcceptanceCommand, ...]) -> str:
        encoded = json.dumps(
            self._command_group_definition(commands),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _command_group_command_fingerprints(
        self,
        commands: tuple[AcceptanceCommand, ...],
    ) -> list[dict[str, str | None]]:
        fingerprints: list[dict[str, str | None]] = []
        for command in commands:
            command_text = command.command
            prompt_text = command.prompt
            fingerprints.append(
                {
                    "command_sha256": hashlib.sha256(command_text.encode("utf-8")).hexdigest()
                    if command_text is not None
                    else None,
                    "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
                    if prompt_text is not None
                    else None,
                }
            )
        return fingerprints

    def save_roadmap(self) -> None:
        write_mapping(self.roadmap_path, self.roadmap)

    def continuation_summary(self) -> dict[str, Any]:
        config = self.roadmap.get("continuation") or {}
        if not isinstance(config, dict):
            config = {}
        stages = config.get("stages") or []
        if not isinstance(stages, list):
            stages = []
        existing = {str(item.get("id")) for item in self.roadmap.get("milestones", [])}
        pending = [stage for stage in stages if str(stage.get("id")) not in existing]
        return {
            "enabled": bool(config.get("enabled", False)),
            "goal": config.get("goal"),
            "blueprint": config.get("blueprint"),
            "stage_count": len(stages),
            "pending_stage_count": len(pending),
            "next_stage": self._continuation_stage_payload(pending[0]) if pending else None,
        }

    def self_iteration_summary(self) -> dict[str, Any]:
        config = self.roadmap.get("self_iteration") or {}
        if not isinstance(config, dict):
            config = {}
        planner = config.get("planner") or {}
        if not isinstance(planner, dict):
            planner = {}
        summary = {
            "enabled": bool(config.get("enabled", False)),
            "objective": config.get("objective"),
            "planner_executor": str(planner.get("executor", "shell")) if planner else None,
            "max_stages_per_iteration": int(config.get("max_stages_per_iteration", 1)),
        }
        latest_assessment = self._latest_self_iteration_assessment()
        if latest_assessment is not None:
            summary["latest_assessment"] = latest_assessment
        return summary

    def _compact_checkpoint_readiness(self, readiness: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(readiness, dict):
            return {}
        keys = (
            "schema_version",
            "kind",
            "is_repository",
            "ready",
            "blocking",
            "reason",
            "dirty_paths",
            "blocking_paths",
            "safe_to_checkpoint_paths",
            "recommended_action",
            "materialization_paths",
            "classifications",
        )
        compact = {key: deepcopy(readiness.get(key)) for key in keys if key in readiness}
        compact["dirty_path_count"] = len(readiness.get("dirty_paths", []))
        compact["blocking_path_count"] = len(readiness.get("blocking_paths", []))
        compact["safe_to_checkpoint_path_count"] = len(readiness.get("safe_to_checkpoint_paths", []))
        return compact

    def _self_iteration_checkpoint_gate(
        self,
        readiness: dict[str, Any],
        *,
        phase: str,
    ) -> dict[str, Any]:
        compact = self._compact_checkpoint_readiness(readiness)
        blocking = bool(compact.get("blocking"))
        reason = str(compact.get("reason") or "unknown")
        if blocking:
            action = str(compact.get("recommended_action") or "Resolve checkpoint readiness blockers.")
            message = (
                "self-iteration checkpoint readiness blocked "
                f"{phase}: {reason}. {action}"
            )
            status = "blocked"
        else:
            message = f"self-iteration checkpoint readiness passed {phase}: {reason}"
            status = "passed"
        return {
            "schema_version": SELF_ITERATION_ASSESSMENT_SCHEMA_VERSION,
            "kind": "engineering-harness.self-iteration-checkpoint-gate",
            "phase": phase,
            "status": status,
            "blocking": blocking,
            "reason": reason,
            "message": message,
            "checkpoint_readiness": compact,
            "dirty_paths": deepcopy(compact.get("dirty_paths", [])),
            "blocking_paths": deepcopy(compact.get("blocking_paths", [])),
            "recommended_action": compact.get("recommended_action"),
        }

    def _self_iteration_allowed_harness_paths(self, *paths: Path) -> list[str]:
        candidates = [self.state_path, self.decision_log_path, *paths]
        allowed: list[str] = []
        for candidate in candidates:
            try:
                relative = self._project_relative_path(candidate)
            except ValueError:
                continue
            if relative and not Path(relative).is_absolute():
                allowed.append(self._normalize_repo_path(relative))
        return sorted(dict.fromkeys(allowed))

    def _self_iteration_checkpoint_readiness(
        self,
        *,
        readiness: dict[str, Any] | None = None,
        allowed_harness_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = deepcopy(readiness) if isinstance(readiness, dict) else self.checkpoint_readiness()
        allowed = sorted(
            dict.fromkeys(
                self._normalize_repo_path(path)
                for path in (allowed_harness_paths or [])
                if str(path).strip()
            )
        )
        if not allowed or not isinstance(payload, dict):
            return payload
        dirty_paths = [
            self._normalize_repo_path(str(path))
            for path in payload.get("dirty_paths", [])
            if str(path).strip()
        ]
        blocking_paths = [
            self._normalize_repo_path(str(path))
            for path in payload.get("blocking_paths", [])
            if str(path).strip()
        ]
        allowed_dirty = sorted(path for path in dirty_paths if path in allowed)
        if not allowed_dirty:
            payload["self_iteration_allowed_harness_paths"] = []
            return payload
        remaining_blocking = sorted(path for path in blocking_paths if path not in allowed_dirty)
        payload["self_iteration_allowed_harness_paths"] = allowed_dirty
        payload.setdefault("classifications", {})
        if isinstance(payload["classifications"], dict):
            payload["classifications"]["harness_self_iteration"] = allowed_dirty
        payload["blocking_paths"] = remaining_blocking
        safe_paths = [
            self._normalize_repo_path(str(path))
            for path in payload.get("safe_to_checkpoint_paths", [])
            if str(path).strip()
        ]
        payload["safe_to_checkpoint_paths"] = sorted(dict.fromkeys([*safe_paths, *allowed_dirty]))
        if not remaining_blocking:
            payload["ready"] = True
            payload["blocking"] = False
            original_reason = str(payload.get("reason") or "")
            if original_reason in {"clean", "harness_materialization_dirty"} and not allowed_dirty:
                reason = original_reason
            elif any(path in self._roadmap_materialization_paths() for path in dirty_paths):
                reason = "self_iteration_checkpointable_dirty_paths"
            else:
                reason = "self_iteration_harness_dirty"
            payload["reason"] = reason
            payload["recommended_action"] = (
                "Run self-iteration normally; only harness-owned self-iteration assessment/state "
                "paths and roadmap materialization paths are dirty."
            )
            return payload
        payload["ready"] = False
        payload["blocking"] = True
        payload["reason"] = (
            "mixed_unrelated_user_dirty"
            if payload.get("safe_to_checkpoint_paths")
            else "unrelated_user_dirty"
        )
        payload["recommended_action"] = (
            "Review, commit, stash, or move the blocking user paths yourself, then rerun "
            "self-iteration. The harness will not commit or clean them."
        )
        return payload

    def _self_iteration_assessment_json_path(self, report_path: Path) -> Path:
        return report_path.with_suffix(".json")

    def _write_self_iteration_assessment(self, report_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        json_path = self._self_iteration_assessment_json_path(report_path)
        assessment = {
            "schema_version": SELF_ITERATION_ASSESSMENT_SCHEMA_VERSION,
            "kind": "engineering-harness.self-iteration-assessment",
            **deepcopy(payload),
            "report_json": str(json_path.relative_to(self.project_root)),
        }
        write_json(json_path, self._redact_context_value(assessment))
        return assessment

    def _latest_self_iteration_assessment(self) -> dict[str, Any] | None:
        assessment_dir = self.report_dir / "assessments"
        if not assessment_dir.exists():
            return None
        paths = sorted(
            [path for path in assessment_dir.glob("*-self-iteration.json") if path.is_file()],
            key=self._project_relative_path,
        )
        for path in reversed(paths):
            try:
                payload = load_mapping(path)
            except Exception:
                continue
            compact = self._compact_self_iteration_assessment(payload)
            if compact:
                return compact
        return None

    def _compact_self_iteration_assessment(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        checkpoint_gate = payload.get("checkpoint_gate") if isinstance(payload.get("checkpoint_gate"), dict) else None
        checkpoint_readiness = (
            payload.get("checkpoint_readiness")
            if isinstance(payload.get("checkpoint_readiness"), dict)
            else checkpoint_gate.get("checkpoint_readiness")
            if checkpoint_gate
            else None
        )
        compact: dict[str, Any] = {
            "schema_version": payload.get("schema_version"),
            "kind": payload.get("kind"),
            "status": payload.get("status"),
            "message": payload.get("message"),
            "reason": payload.get("reason"),
            "report": payload.get("report"),
            "report_json": payload.get("report_json"),
            "snapshot": payload.get("snapshot"),
            "context_pack": deepcopy(payload.get("context_pack"))
            if isinstance(payload.get("context_pack"), dict)
            else payload.get("context_pack"),
            "stage_count_before": payload.get("stage_count_before"),
            "stage_count_after": payload.get("stage_count_after"),
            "pending_stage_count_after": payload.get("pending_stage_count_after"),
        }
        if checkpoint_gate:
            compact["checkpoint_gate"] = deepcopy(checkpoint_gate)
        if isinstance(payload.get("checkpoint_gates"), dict):
            compact["checkpoint_gates"] = deepcopy(payload.get("checkpoint_gates"))
        if isinstance(checkpoint_readiness, dict):
            compact["checkpoint_readiness"] = self._compact_checkpoint_readiness(checkpoint_readiness)
            compact["dirty_paths"] = deepcopy(compact["checkpoint_readiness"].get("dirty_paths", []))
            compact["blocking_paths"] = deepcopy(compact["checkpoint_readiness"].get("blocking_paths", []))
            compact["recommended_action"] = compact["checkpoint_readiness"].get("recommended_action")
        if isinstance(payload.get("goal_gap_scorecard"), dict):
            compact["goal_gap_scorecard"] = deepcopy(payload.get("goal_gap_scorecard"))
        validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else None
        if validation:
            compact["validation"] = {
                "status": validation.get("status"),
                "error_count": validation.get("error_count", 0),
                "warning_count": validation.get("warning_count", 0),
                "new_stage_ids": deepcopy(validation.get("new_stage_ids", [])),
                "new_stage_requirement_refs": deepcopy(validation.get("new_stage_requirement_refs", [])),
            }
        return {key: value for key, value in compact.items() if value is not None}

    def advance_roadmap(self, *, max_new_milestones: int = 1, reason: str = "queue_empty") -> dict[str, Any]:
        self.drive_heartbeat(
            activity="continuation-materialization",
            message=f"checking roadmap continuation: {reason}",
            clear_task=True,
        )
        config = self.roadmap.get("continuation") or {}
        if not isinstance(config, dict) or not config.get("enabled", False):
            self.drive_heartbeat(
                activity="continuation-materialization",
                message="roadmap continuation is not enabled",
                clear_task=True,
            )
            return {
                "status": "disabled",
                "message": "roadmap continuation is not enabled",
                "milestones_added": [],
                "tasks_added": 0,
            }
        stages = config.get("stages") or []
        if not isinstance(stages, list):
            self.drive_heartbeat(
                activity="continuation-materialization",
                message="continuation.stages must be a list",
                clear_task=True,
            )
            return {
                "status": "error",
                "message": "continuation.stages must be a list",
                "milestones_added": [],
                "tasks_added": 0,
            }
        existing_milestones = {str(item.get("id")) for item in self.roadmap.get("milestones", [])}
        existing_tasks = {task.id for task in self.iter_tasks()}
        materialized: list[dict[str, Any]] = []
        tasks_added = 0
        for stage in stages:
            if len(materialized) >= max_new_milestones:
                break
            stage_id = str(stage.get("id", ""))
            if not stage_id or stage_id in existing_milestones:
                continue
            milestone = self._materialize_continuation_stage(stage, existing_tasks=existing_tasks)
            self.roadmap.setdefault("milestones", []).append(milestone)
            existing_milestones.add(stage_id)
            for task in milestone.get("tasks", []):
                existing_tasks.add(str(task["id"]))
            task_count = len(milestone.get("tasks", []))
            tasks_added += task_count
            materialized.append({"id": stage_id, "title": milestone.get("title", stage_id), "tasks": task_count})
        if not materialized:
            self.drive_heartbeat(
                activity="continuation-materialization",
                message="no unmaterialized continuation stage remains",
                clear_task=True,
            )
            return {
                "status": "exhausted",
                "message": "no unmaterialized continuation stage remains",
                "milestones_added": [],
                "tasks_added": 0,
            }
        self.save_roadmap()
        event = {
            "at": utc_now(),
            "event": "roadmap_continuation",
            "reason": reason,
            "milestones_added": materialized,
            "tasks_added": tasks_added,
            "goal": config.get("goal"),
        }
        append_jsonl(self.decision_log_path, event)
        self.drive_heartbeat(
            activity="continuation-materialized",
            message=f"materialized {len(materialized)} continuation milestone(s)",
            clear_task=True,
        )
        return {
            "status": "advanced",
            "message": f"materialized {len(materialized)} continuation milestone(s)",
            "milestones_added": materialized,
            "tasks_added": tasks_added,
        }

    def run_self_iteration(
        self,
        *,
        reason: str = "roadmap_exhausted",
        allow_agent: bool = False,
        allow_live: bool = False,
        checkpoint_readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.drive_heartbeat(
            activity="self-iteration",
            message=f"checking self-iteration planner: {reason}",
            clear_task=True,
        )
        config = self.roadmap.get("self_iteration") or {}
        if not isinstance(config, dict) or not config.get("enabled", False):
            self.drive_heartbeat(
                activity="self-iteration",
                message="self_iteration is not enabled",
                clear_task=True,
            )
            return {
                "status": "disabled",
                "message": "self_iteration is not enabled",
                "stage_count_before": self.continuation_summary()["stage_count"],
                "stage_count_after": self.continuation_summary()["stage_count"],
                "pending_stage_count_after": self.continuation_summary()["pending_stage_count"],
                "report": None,
            }
        isolated_failures = self.latest_isolated_failures_summary()
        if int(isolated_failures.get("unresolved_count", 0) or 0) > 0:
            continuation = self.continuation_summary()
            message = "unresolved isolated task failure exists; resolve it before self-iteration adds another stage"
            self.drive_heartbeat(
                activity="self-iteration",
                message=message,
                clear_task=True,
            )
            return {
                "status": "isolated_failure",
                "message": message,
                "stage_count_before": continuation["stage_count"],
                "stage_count_after": continuation["stage_count"],
                "pending_stage_count_after": continuation["pending_stage_count"],
                "report": None,
                "failure_isolation": isolated_failures,
            }
        planner = config.get("planner") or {}
        if not isinstance(planner, dict):
            self.drive_heartbeat(
                activity="self-iteration",
                message="self_iteration.planner must be a mapping",
                clear_task=True,
            )
            return {
                "status": "error",
                "message": "self_iteration.planner must be a mapping",
                "stage_count_before": self.continuation_summary()["stage_count"],
                "stage_count_after": self.continuation_summary()["stage_count"],
                "pending_stage_count_after": self.continuation_summary()["pending_stage_count"],
                "report": None,
            }

        assessment_dir = self.report_dir / "assessments"
        assessment_dir.mkdir(parents=True, exist_ok=True)
        assessment_slug = slug_now()
        snapshot_path = assessment_dir / f"{assessment_slug}-self-iteration-snapshot.json"
        context_path = assessment_dir / f"{assessment_slug}-self-iteration-context.json"
        report_path = assessment_dir / f"{assessment_slug}-self-iteration.md"
        allowed_harness_paths = self._self_iteration_allowed_harness_paths(
            snapshot_path,
            context_path,
            report_path,
            self._self_iteration_assessment_json_path(report_path),
        )
        before_roadmap = deepcopy(self.roadmap)
        before_roadmap_text = self.roadmap_path.read_text(encoding="utf-8")
        before = self.continuation_summary()
        expected_new_stages = max(1, int(config.get("max_stages_per_iteration", 1)))
        preflight_readiness = self._self_iteration_checkpoint_readiness(readiness=checkpoint_readiness)
        preflight_gate = self._self_iteration_checkpoint_gate(preflight_readiness, phase="preflight")
        if preflight_gate["status"] == "blocked":
            message = preflight_gate["message"]
            goal_gap_scorecard = self.goal_gap_scorecard()
            snapshot = {
                "generated_at": utc_now(),
                "reason": reason,
                "status": "blocked",
                "message": message,
                "checkpoint_gate": preflight_gate,
                "checkpoint_readiness": preflight_gate["checkpoint_readiness"],
                "goal_gap_scorecard": goal_gap_scorecard,
                "recent_git": self._git(["log", "--oneline", "-8"]),
                "git_status": self._git(["status", "--short"]),
            }
            write_json(snapshot_path, snapshot)
            run = CommandRun(
                "self-iteration",
                "checkpoint readiness preflight",
                "checkpoint-readiness preflight",
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                message,
                executor="checkpoint-readiness",
                executor_metadata={"id": "checkpoint-readiness", "kind": "local-preflight"},
            )
            self._write_self_iteration_report(
                report_path,
                reason,
                snapshot_path,
                before,
                before,
                run,
                status="blocked",
                message=message,
                checkpoint_gate=preflight_gate,
                checkpoint_readiness=preflight_gate["checkpoint_readiness"],
                goal_gap_scorecard=goal_gap_scorecard,
            )
            assessment = self._write_self_iteration_assessment(
                report_path,
                {
                    "generated_at": utc_now(),
                    "reason": reason,
                    "status": "blocked",
                    "message": message,
                    "stage_count_before": before["stage_count"],
                    "stage_count_after": before["stage_count"],
                    "pending_stage_count_after": before["pending_stage_count"],
                    "report": str(report_path.relative_to(self.project_root)),
                    "snapshot": str(snapshot_path.relative_to(self.project_root)),
                    "context_pack": None,
                    "checkpoint_gate": preflight_gate,
                    "checkpoint_gates": {"preflight": preflight_gate},
                    "checkpoint_readiness": preflight_gate["checkpoint_readiness"],
                    "goal_gap_scorecard": goal_gap_scorecard,
                },
            )
            append_jsonl(
                self.decision_log_path,
                {
                    "at": utc_now(),
                    "event": "self_iteration",
                    "reason": reason,
                    "status": "blocked",
                    "message": message,
                    "stage_count_before": before["stage_count"],
                    "stage_count_after": before["stage_count"],
                    "pending_stage_count_after": before["pending_stage_count"],
                    "report": str(report_path.relative_to(self.project_root)),
                    "report_json": assessment["report_json"],
                    "snapshot": str(snapshot_path.relative_to(self.project_root)),
                    "context_pack": None,
                    "checkpoint_gate": preflight_gate,
                    "checkpoint_readiness": preflight_gate["checkpoint_readiness"],
                    "goal_gap_scorecard": goal_gap_scorecard,
                },
            )
            self.drive_heartbeat(
                activity="self-iteration",
                message=message,
                clear_task=True,
            )
            return {
                "status": "blocked",
                "message": message,
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
                "report_json": assessment["report_json"],
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": None,
                "checkpoint_gate": preflight_gate,
                "checkpoint_gates": {"preflight": preflight_gate},
                "checkpoint_readiness": preflight_gate["checkpoint_readiness"],
                "goal_gap_scorecard": goal_gap_scorecard,
                "dirty_paths": deepcopy(preflight_gate.get("dirty_paths", [])),
                "blocking_paths": deepcopy(preflight_gate.get("blocking_paths", [])),
                "reason": preflight_gate.get("reason"),
                "recommended_action": preflight_gate.get("recommended_action"),
            }
        context_pack = self._self_iteration_context_pack(
            reason=reason,
            snapshot_path=snapshot_path,
            context_path=context_path,
        )
        write_json(context_path, context_pack)
        context_info = {
            "path": str(context_path.relative_to(self.project_root)),
            "summary": context_pack["summary"],
            "goal_gap_scorecard": deepcopy(context_pack.get("goal_gap_scorecard", {})),
        }
        snapshot = {
            "generated_at": utc_now(),
            "reason": reason,
            "status": self.status_summary(),
            "recent_git": self._git(["log", "--oneline", "-8"]),
            "git_status": self._git(["status", "--short"]),
            "context_pack": context_info,
            "checkpoint_gate": preflight_gate,
            "checkpoint_readiness": preflight_gate["checkpoint_readiness"],
        }
        write_json(snapshot_path, snapshot)

        command = self._parse_task_commands([planner], default_name="self-iteration-planner")[0]
        executor = self.executor_registry.get(command.executor)
        if executor is None:
            run = CommandRun(
                "self-iteration",
                command.name,
                self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                f"unknown executor: {command.executor}",
                executor=command.executor,
                executor_metadata=self.executor_registry.metadata_for(command.executor),
            )
            self._write_self_iteration_report(
                report_path,
                reason,
                snapshot_path,
                before,
                before,
                run,
                context_pack=context_info,
                status="blocked",
                message=f"unknown executor: {command.executor}",
                checkpoint_gate=preflight_gate,
                checkpoint_readiness=preflight_gate["checkpoint_readiness"],
            )
            self.drive_heartbeat(
                activity="self-iteration",
                message=f"unknown executor: {command.executor}",
                clear_task=True,
            )
            return {
                "status": "blocked",
                "message": f"unknown executor: {command.executor}",
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
            }
        if executor.metadata.requires_agent_approval and not allow_agent:
            block_reason = f"{command.executor} planner requires --allow-agent"
            run = CommandRun(
                "self-iteration",
                command.name,
                self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                block_reason,
                executor=command.executor,
                executor_metadata=executor.metadata.as_contract(),
            )
            self._write_self_iteration_report(
                report_path,
                reason,
                snapshot_path,
                before,
                before,
                run,
                context_pack=context_info,
                status="blocked",
                message=block_reason,
                checkpoint_gate=preflight_gate,
                checkpoint_readiness=preflight_gate["checkpoint_readiness"],
            )
            self.drive_heartbeat(
                activity="self-iteration",
                message=block_reason,
                clear_task=True,
            )
            return {
                "status": "blocked",
                "message": block_reason,
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
            }
        if executor.metadata.uses_command_policy:
            allowed, block_reason = self.command_allowed(command.command, allow_live=allow_live)
            if not allowed:
                run = CommandRun(
                    "self-iteration",
                    command.name,
                    self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                    "blocked",
                    None,
                    utc_now(),
                    utc_now(),
                    "",
                    block_reason,
                    executor=command.executor,
                    executor_metadata=executor.metadata.as_contract(),
                )
                self._write_self_iteration_report(
                    report_path,
                    reason,
                    snapshot_path,
                    before,
                    before,
                    run,
                    context_pack=context_info,
                    status="blocked",
                    message=block_reason,
                    checkpoint_gate=preflight_gate,
                    checkpoint_readiness=preflight_gate["checkpoint_readiness"],
                )
                self.drive_heartbeat(
                    activity="self-iteration",
                    message=block_reason,
                    clear_task=True,
                )
                return {
                    "status": "blocked",
                    "message": block_reason,
                    "stage_count_before": before["stage_count"],
                    "stage_count_after": before["stage_count"],
                    "pending_stage_count_after": before["pending_stage_count"],
                    "report": str(report_path.relative_to(self.project_root)),
                    "snapshot": str(snapshot_path.relative_to(self.project_root)),
                    "context_pack": context_info,
                }

        planner_prompt = self._self_iteration_prompt(config, snapshot_path, context_path)
        planner_task = self._self_iteration_task(command, snapshot_path, planner_prompt)
        command = replace(command, prompt=planner_prompt)
        self.drive_heartbeat(
            activity="self-iteration-planner",
            message=f"running self-iteration planner: {command.name}",
            clear_task=True,
        )
        run = self._run_command(
            command,
            phase="self-iteration",
            task=planner_task,
            state=self.load_state(),
            persist_state=True,
        )
        self.drive_heartbeat(
            activity="self-iteration-planner",
            message=f"self-iteration planner finished with status {run.status}",
            clear_task=True,
        )

        observed_after = before
        validation: dict[str, Any]
        acceptance_gate: dict[str, Any] | None = None
        if run.returncode != 0:
            status = "failed"
            message = f"self-iteration planner failed: {command.name}"
        else:
            acceptance_readiness = self._self_iteration_checkpoint_readiness(
                allowed_harness_paths=allowed_harness_paths
            )
            acceptance_gate = self._self_iteration_checkpoint_gate(acceptance_readiness, phase="acceptance")
            if acceptance_gate["status"] == "blocked":
                self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
                self.roadmap = deepcopy(before_roadmap)
                validation = self._self_iteration_validation_result(
                    status="skipped",
                    errors=[],
                    warnings=[
                        "planner output was restored and not accepted because checkpoint readiness "
                        "blocked roadmap diff acceptance"
                    ],
                    expected_new_stage_count=expected_new_stages,
                    actual_new_stage_count=0,
                    new_stage_ids=[],
                )
                status = "blocked"
                message = acceptance_gate["message"]
            else:
                try:
                    after_roadmap = load_mapping(self.roadmap_path)
                except Exception as exc:
                    validation = self._self_iteration_validation_result(
                        status="failed",
                        errors=[f"planner output is not a readable roadmap mapping: {exc}"],
                        warnings=[],
                        expected_new_stage_count=expected_new_stages,
                        actual_new_stage_count=0,
                        new_stage_ids=[],
                    )
                    self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
                    self.roadmap = deepcopy(before_roadmap)
                    status = "rejected"
                    message = "self-iteration planner output failed validation"
                else:
                    self.roadmap = after_roadmap
                    observed_after = self.continuation_summary()
                    validation = self._validate_self_iteration_output(
                        before_roadmap,
                        after_roadmap,
                        expected_new_stage_count=expected_new_stages,
                    )
                    if validation["status"] == "passed":
                        status = "planned"
                        count = len(validation.get("new_stage_ids", []))
                        message = f"self-iteration planner added {count} validated continuation stage(s)"
                    else:
                        self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
                        self.roadmap = deepcopy(before_roadmap)
                        status = "rejected"
                        message = "self-iteration planner output failed validation"

        if run.returncode != 0:
            self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
            self.roadmap = deepcopy(before_roadmap)
            validation = self._self_iteration_validation_result(
                status="skipped",
                errors=[],
                warnings=["planner command failed; output was restored and not validated"],
                expected_new_stage_count=expected_new_stages,
                actual_new_stage_count=0,
                new_stage_ids=[],
            )

        final_after = self.continuation_summary()
        checkpoint_gates = {"preflight": preflight_gate}
        if acceptance_gate is not None:
            checkpoint_gates["acceptance"] = acceptance_gate
        final_checkpoint_gate = acceptance_gate or preflight_gate
        final_checkpoint_readiness = final_checkpoint_gate["checkpoint_readiness"]

        failure_isolation = self._self_iteration_failure_isolation(
            run,
            status=status,
            message=message,
            report_path=report_path,
            snapshot_path=snapshot_path,
            context_path=context_path,
        )

        self._write_self_iteration_report(
            report_path,
            reason,
            snapshot_path,
            before,
            observed_after,
            run,
            validation=validation,
            context_pack=context_info,
            failure_isolation=failure_isolation,
            status=status,
            message=message,
            checkpoint_gate=final_checkpoint_gate,
            checkpoint_readiness=final_checkpoint_readiness,
        )
        assessment = self._write_self_iteration_assessment(
            report_path,
            {
                "generated_at": utc_now(),
                "reason": reason,
                "status": status,
                "message": message,
                "stage_count_before": before["stage_count"],
                "stage_count_after": final_after["stage_count"],
                "pending_stage_count_after": final_after["pending_stage_count"],
                "validation": validation,
                "report": str(report_path.relative_to(self.project_root)),
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
                "checkpoint_gate": final_checkpoint_gate,
                "checkpoint_gates": checkpoint_gates,
                "checkpoint_readiness": final_checkpoint_readiness,
                "goal_gap_scorecard": deepcopy(context_info.get("goal_gap_scorecard", {})),
                "run": {
                    "name": run.name,
                    "command": run.command,
                    "status": run.status,
                    "returncode": run.returncode,
                    "executor": run.executor,
                    "executor_metadata": run.executor_metadata,
                    "executor_result": self._executor_result_contract(run),
                },
                **({"failure_isolation": failure_isolation} if failure_isolation else {}),
            },
        )
        append_jsonl(
            self.decision_log_path,
            {
                "at": utc_now(),
                "event": "self_iteration",
                "reason": reason,
                "status": status,
                "message": message,
                "stage_count_before": before["stage_count"],
                "stage_count_after": final_after["stage_count"],
                "pending_stage_count_after": final_after["pending_stage_count"],
                "validation": validation,
                "report": str(report_path.relative_to(self.project_root)),
                "report_json": assessment["report_json"],
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
                "checkpoint_gate": final_checkpoint_gate,
                "checkpoint_readiness": final_checkpoint_readiness,
                "goal_gap_scorecard": deepcopy(context_info.get("goal_gap_scorecard", {})),
            },
        )
        self.drive_heartbeat(
            activity="self-iteration",
            message=message,
            clear_task=True,
        )
        return {
            "status": status,
            "message": message,
            "stage_count_before": before["stage_count"],
            "stage_count_after": final_after["stage_count"],
            "pending_stage_count_after": final_after["pending_stage_count"],
            "report": str(report_path.relative_to(self.project_root)),
            "report_json": assessment["report_json"],
            "snapshot": str(snapshot_path.relative_to(self.project_root)),
            "context_pack": context_info,
            "validation": validation,
            "checkpoint_gate": final_checkpoint_gate,
            "checkpoint_gates": checkpoint_gates,
            "checkpoint_readiness": final_checkpoint_readiness,
            "goal_gap_scorecard": deepcopy(context_info.get("goal_gap_scorecard", {})),
            "dirty_paths": deepcopy(final_checkpoint_gate.get("dirty_paths", [])),
            "blocking_paths": deepcopy(final_checkpoint_gate.get("blocking_paths", [])),
            "reason": final_checkpoint_gate.get("reason"),
            "recommended_action": final_checkpoint_gate.get("recommended_action"),
            "run": {
                "name": run.name,
                "command": run.command,
                "status": run.status,
                "returncode": run.returncode,
                "executor": run.executor,
                "executor_metadata": run.executor_metadata,
                "executor_result": self._executor_result_contract(run),
            },
            **({"failure_isolation": failure_isolation} if failure_isolation else {}),
        }

    def _self_iteration_context_pack(
        self,
        *,
        reason: str,
        snapshot_path: Path,
        context_path: Path,
    ) -> dict[str, Any]:
        roadmap_context = self._self_iteration_roadmap_context()
        manifest_context = self._self_iteration_manifest_context()
        report_context = self._self_iteration_report_context()
        docs_context = self._self_iteration_docs_context()
        test_inventory = self._self_iteration_test_inventory()
        source_inventory = self._self_iteration_source_inventory()
        git_context = self._self_iteration_git_context()
        duplicate_plan = self._self_iteration_duplicate_plan_summary()
        status_for_scorecard = self.status_summary()
        goal_gap_scorecard = deepcopy(status_for_scorecard.get("goal_gap_scorecard", {}))
        spec_coverage = self.spec_coverage_summary()
        spec_traceability = self._self_iteration_spec_traceability_context(self.roadmap)
        summary = {
            "project": roadmap_context.get("project"),
            "roadmap_path": roadmap_context.get("path"),
            "continuation_stage_count": roadmap_context.get("continuation", {}).get("stage_count", 0),
            "pending_stage_count": roadmap_context.get("continuation", {}).get("pending_stage_count", 0),
            "spec_status": spec_coverage.get("status"),
            "spec_traceability_required": bool(spec_traceability.get("required")),
            "spec_known_requirement_count": spec_coverage.get("known_requirement_count", 0),
            "spec_referenced_requirement_count": spec_coverage.get("referenced_requirement_count", 0),
            "duplicate_plan_fingerprint_count": duplicate_plan.get("fingerprint_count", 0),
            "duplicate_plan_duplicate_group_count": duplicate_plan.get("duplicate_group_count", 0),
            "manifest_count": manifest_context.get("index", {}).get("manifest_count", 0),
            "recent_manifest_count": len(manifest_context.get("recent_task_manifests", [])),
            "task_report_count": report_context.get("task_reports", {}).get("total_count", 0),
            "drive_report_count": report_context.get("drive_reports", {}).get("total_count", 0),
            "doc_count": docs_context.get("relevant_docs", {}).get("included_count", 0),
            "test_file_count": test_inventory.get("included_count", 0),
            "source_file_count": source_inventory.get("included_count", 0),
            "git_is_repository": git_context.get("is_repository", False),
            "git_status_line_count": len(git_context.get("status_lines", [])),
            "recent_commit_count": len(git_context.get("recent_commits", [])),
            "goal_gap_scorecard_status": goal_gap_scorecard.get("summary", {}).get("overall_status"),
            "goal_gap_scorecard_max_risk_score": goal_gap_scorecard.get("summary", {}).get("max_risk_score"),
            "goal_gap_scorecard_blocked_count": goal_gap_scorecard.get("summary", {})
            .get("status_counts", {})
            .get("blocked", 0),
        }
        payload: dict[str, Any] = {
            "schema_version": SELF_ITERATION_CONTEXT_SCHEMA_VERSION,
            "kind": "engineering-harness.self-iteration-context-pack",
            "reason": reason,
            "project_root": str(self.project_root),
            "snapshot_path": str(snapshot_path.relative_to(self.project_root)),
            "context_path": str(context_path.relative_to(self.project_root)),
            "limits": dict(SELF_ITERATION_CONTEXT_LIMITS),
            "summary": summary,
            "roadmap": roadmap_context,
            "spec": spec_coverage,
            "spec_traceability": spec_traceability,
            "duplicate_plan": duplicate_plan,
            "manifests": manifest_context,
            "reports": report_context,
            "docs": docs_context,
            "test_inventory": test_inventory,
            "source_inventory": source_inventory,
            "git": git_context,
            "goal_gap_scorecard": goal_gap_scorecard,
        }
        return self._redact_context_value(payload)

    def _self_iteration_roadmap_context(self) -> dict[str, Any]:
        milestones = self.roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            milestones = []
        continuation = self.continuation_summary()
        continuation_config = self.roadmap.get("continuation") if isinstance(self.roadmap.get("continuation"), dict) else {}
        continuation_stages = continuation_config.get("stages", []) if isinstance(continuation_config, dict) else []
        if not isinstance(continuation_stages, list):
            continuation_stages = []
        state = self.load_state()
        task_states = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        tasks = list(self.iter_tasks())
        task_status_counts: dict[str, int] = {}
        for task in tasks:
            task_state = task_states.get(task.id, {}) if isinstance(task_states.get(task.id), dict) else {}
            status = str(task_state.get("status", task.status))
            task_status_counts[status] = task_status_counts.get(status, 0) + 1
        goal = self.roadmap.get("goal") if isinstance(self.roadmap.get("goal"), dict) else {}
        generated_from = self.roadmap.get("generated_from") if isinstance(self.roadmap.get("generated_from"), dict) else {}
        return {
            "path": self._project_relative_path(self.roadmap_path),
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "profile": self.roadmap.get("profile"),
            "generated_by": self.roadmap.get("generated_by"),
            "goal": {
                "text": self._truncate_text(str(goal.get("text") or generated_from.get("goal") or ""), 500),
                "blueprint": goal.get("blueprint") or generated_from.get("blueprint_path"),
                "constraints": self._string_items(goal.get("constraints")) if isinstance(goal, dict) else [],
            },
            "milestone_count": len(milestones),
            "task_count": len(tasks),
            "task_status_counts": dict(sorted(task_status_counts.items())),
            "next_task": self.task_payload(self.next_task()),
            "continuation": {
                **continuation,
                "stages": [
                    self._self_iteration_stage_summary(stage)
                    for stage in continuation_stages[: SELF_ITERATION_CONTEXT_LIMITS["continuation_stage_count"]]
                    if isinstance(stage, dict)
                ],
                "stage_count_truncated": len(continuation_stages)
                > SELF_ITERATION_CONTEXT_LIMITS["continuation_stage_count"],
            },
            "self_iteration": self.self_iteration_summary(),
        }

    def _self_iteration_stage_summary(self, stage: dict[str, Any]) -> dict[str, Any]:
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        payload = {
            "id": str(stage.get("id", "")),
            "title": str(stage.get("title", "")),
            "status": str(stage.get("status", "planned")),
            "objective": self._truncate_text(str(stage.get("objective", "")), 500),
            "spec_refs": self._collect_stage_spec_refs(stage),
            "task_count": len(tasks),
            "tasks": [
                {
                    "id": str(task.get("id", "")),
                    "title": str(task.get("title", "")),
                    "status": str(task.get("status", "pending")),
                    "spec_refs": self._collect_task_spec_refs(task),
                    "file_scope": [str(scope) for scope in task.get("file_scope", [])]
                    if isinstance(task.get("file_scope"), list)
                    else [],
                    "acceptance_count": len(task.get("acceptance", [])) if isinstance(task.get("acceptance"), list) else 0,
                    "e2e_count": len(task.get("e2e", [])) if isinstance(task.get("e2e"), list) else 0,
                }
                for task in tasks[:8]
                if isinstance(task, dict)
            ],
            "task_count_truncated": len(tasks) > 8,
        }
        return payload

    def _append_unique_spec_refs(self, refs: list[str], value: Any) -> None:
        for ref in self._normalize_spec_refs(value):
            if ref not in refs:
                refs.append(ref)

    def _collect_command_group_spec_refs(self, value: Any) -> list[str]:
        refs: list[str] = []
        if not isinstance(value, list):
            return refs
        for item in value:
            if isinstance(item, dict):
                self._append_unique_spec_refs(refs, item.get("spec_refs"))
        return refs

    def _collect_task_spec_refs(self, task: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        self._append_unique_spec_refs(refs, task.get("spec_refs"))
        for group_name in ("implementation", "repair", "acceptance", "e2e"):
            for ref in self._collect_command_group_spec_refs(task.get(group_name)):
                if ref not in refs:
                    refs.append(ref)
        return refs

    def _collect_stage_spec_refs(self, stage: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        self._append_unique_spec_refs(refs, stage.get("spec_refs"))
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            return refs
        for task in tasks:
            if not isinstance(task, dict):
                continue
            for ref in self._collect_task_spec_refs(task):
                if ref not in refs:
                    refs.append(ref)
        return refs

    def _spec_coverage_summary_for_roadmap(self, roadmap: dict[str, Any]) -> dict[str, Any]:
        current_roadmap = self.roadmap
        self.roadmap = roadmap
        try:
            return self.spec_coverage_summary()
        finally:
            self.roadmap = current_roadmap

    def _roadmap_declared_spec_refs(self, roadmap: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        milestones = roadmap.get("milestones", [])
        if isinstance(milestones, list):
            for milestone in milestones:
                if isinstance(milestone, dict):
                    for ref in self._collect_stage_spec_refs(milestone):
                        if ref not in refs:
                            refs.append(ref)
        continuation = roadmap.get("continuation") if isinstance(roadmap.get("continuation"), dict) else {}
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if isinstance(stages, list):
            for stage in stages:
                if isinstance(stage, dict):
                    for ref in self._collect_stage_spec_refs(stage):
                        if ref not in refs:
                            refs.append(ref)
        return refs

    def _self_iteration_spec_traceability_context(self, roadmap: dict[str, Any]) -> dict[str, Any]:
        spec = roadmap.get("spec") if isinstance(roadmap.get("spec"), dict) else {}
        declared_refs = self._roadmap_declared_spec_refs(roadmap)
        coverage = self._spec_coverage_summary_for_roadmap(roadmap)
        spec_configured = bool(coverage.get("configured"))
        spec_fields = []
        for field_name in ("path", "requirements_index", "development_plan", "traceability_field"):
            if isinstance(spec, dict) and str(spec.get(field_name) or "").strip():
                spec_fields.append(field_name)
        required = bool(declared_refs or spec_configured or spec_fields)
        if declared_refs:
            reason = "roadmap_has_spec_refs"
        elif spec_configured or spec_fields:
            reason = "roadmap_spec_configured"
        else:
            reason = "roadmap_spec_traceability_unconfigured"
        candidate_refs: list[str] = []
        for key in ("referenced_requirements", "unreferenced_requirements", "unknown_requirements"):
            for ref in coverage.get(key, []):
                if ref not in candidate_refs:
                    candidate_refs.append(ref)
        return {
            "field": "spec_refs",
            "required": required,
            "reason": reason,
            "spec_fields": spec_fields,
            "known_requirement_count": coverage.get("known_requirement_count", 0),
            "referenced_requirement_count": len(declared_refs),
            "candidate_requirement_refs": candidate_refs[:25],
            "candidate_requirement_refs_truncated": len(candidate_refs) > 25,
            "referenced_requirements": declared_refs[:25],
            "referenced_requirements_truncated": len(declared_refs) > 25,
        }

    def _validate_self_iteration_new_stage_spec_refs(
        self,
        stage: dict[str, Any],
        *,
        stage_id: str,
        location: str,
        spec_traceability: dict[str, Any],
        errors: list[str],
    ) -> None:
        if not bool(spec_traceability.get("required")):
            return
        if not self._normalize_spec_refs(stage.get("spec_refs")):
            errors.append(
                f"new continuation stage `{stage_id}` must define non-empty spec_refs because "
                "the existing roadmap is spec-traceable"
            )
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            return
        for task_index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id", "")).strip() or f"{location}.tasks[{task_index}]"
            if not self._normalize_spec_refs(task.get("spec_refs")):
                errors.append(
                    f"new continuation task `{task_id}` must define non-empty spec_refs because "
                    "the existing roadmap is spec-traceable"
                )

    def _self_iteration_stage_requirement_ref_summaries(self, stages: list[Any]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            tasks = stage.get("tasks", [])
            if not isinstance(tasks, list):
                tasks = []
            task_summaries: list[dict[str, Any]] = []
            for task in tasks[:8]:
                if not isinstance(task, dict):
                    continue
                task_spec_refs = list(self._normalize_spec_refs(task.get("spec_refs")))
                command_spec_refs: list[str] = []
                for group_name in ("implementation", "repair", "acceptance", "e2e"):
                    for ref in self._collect_command_group_spec_refs(task.get(group_name)):
                        if ref not in command_spec_refs:
                            command_spec_refs.append(ref)
                task_summaries.append(
                    {
                        "task_id": str(task.get("id", "")),
                        "title": self._truncate_text(str(task.get("title", "")), 160),
                        "spec_refs": self._collect_task_spec_refs(task),
                        "task_spec_refs": task_spec_refs,
                        "command_spec_refs": command_spec_refs,
                    }
                )
            summaries.append(
                {
                    "stage_id": str(stage.get("id", "")),
                    "title": self._truncate_text(str(stage.get("title", "")), 160),
                    "spec_refs": self._collect_stage_spec_refs(stage),
                    "task_count": len(tasks),
                    "tasks": task_summaries,
                    "task_count_truncated": len(tasks) > 8,
                }
            )
        return summaries

    def _self_iteration_duplicate_plan_summary(self) -> dict[str, Any]:
        continuation = self.roadmap.get("continuation") if isinstance(self.roadmap.get("continuation"), dict) else {}
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if not isinstance(stages, list):
            stages = []
        valid_stages = [stage for stage in stages if isinstance(stage, dict)]
        stage_limit = int(SELF_ITERATION_CONTEXT_LIMITS["duplicate_plan_stage_count"])
        task_limit = int(SELF_ITERATION_CONTEXT_LIMITS["duplicate_plan_task_count"])
        group_limit = int(SELF_ITERATION_CONTEXT_LIMITS["duplicate_plan_group_count"])
        fingerprint_index = self._self_iteration_stage_fingerprint_index(valid_stages)
        entries = [
            self._self_iteration_duplicate_plan_entry(stage, task_limit=task_limit)
            for stage in valid_stages[:stage_limit]
        ]
        duplicate_groups = [
            {
                "fingerprint": fingerprint,
                "stage_ids": stage_ids[:task_limit],
                "stage_count": len(stage_ids),
                "stage_ids_truncated": len(stage_ids) > task_limit,
            }
            for fingerprint, stage_ids in sorted(fingerprint_index.items())
            if len(stage_ids) > 1
        ]
        return {
            "algorithm": "sha256:self-iteration-stage-plan:v1",
            "stage_count": len(valid_stages),
            "included_count": len(entries),
            "truncated": len(valid_stages) > stage_limit,
            "fingerprint_count": len(fingerprint_index),
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_groups": duplicate_groups[:group_limit],
            "duplicate_groups_truncated": len(duplicate_groups) > group_limit,
            "stages": entries,
        }

    def _self_iteration_duplicate_plan_entry(self, stage: dict[str, Any], *, task_limit: int) -> dict[str, Any]:
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        valid_tasks = [task for task in tasks if isinstance(task, dict)]
        return {
            "stage_id": str(stage.get("id", "")).strip(),
            "title": self._truncate_text(str(stage.get("title", "")), 160),
            "fingerprint": self._self_iteration_stage_fingerprint(stage),
            "identity_fingerprint": self._self_iteration_stage_fingerprint(stage, include_task_ids=True),
            "task_count": len(valid_tasks),
            "task_ids": [str(task.get("id", "")).strip() for task in valid_tasks[:task_limit]],
            "task_titles": [
                self._truncate_text(str(task.get("title", "")), 160)
                for task in valid_tasks[:task_limit]
            ],
            "tasks_truncated": len(valid_tasks) > task_limit,
        }

    def _self_iteration_stage_fingerprint_index(self, stages: list[Any]) -> dict[str, list[str]]:
        fingerprints: dict[str, list[str]] = {}
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            fingerprint = self._self_iteration_stage_fingerprint(stage)
            stage_id = str(stage.get("id", "")).strip() or f"continuation.stages[{stage_index}]"
            fingerprints.setdefault(fingerprint, []).append(stage_id)
        return fingerprints

    def _self_iteration_stage_fingerprint(
        self,
        stage: dict[str, Any],
        *,
        include_task_ids: bool = False,
    ) -> str:
        payload = self._self_iteration_stage_fingerprint_payload(stage, include_task_ids=include_task_ids)
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _self_iteration_stage_fingerprint_payload(
        self,
        stage: dict[str, Any],
        *,
        include_task_ids: bool,
    ) -> dict[str, Any]:
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        return {
            "version": 1,
            "title": self._self_iteration_fingerprint_text(stage.get("title")),
            "objective": self._self_iteration_fingerprint_text(stage.get("objective")),
            "tasks": [
                self._self_iteration_task_fingerprint_payload(task, include_task_ids=include_task_ids)
                for task in tasks
                if isinstance(task, dict)
            ],
        }

    def _self_iteration_task_fingerprint_payload(
        self,
        task: dict[str, Any],
        *,
        include_task_ids: bool,
    ) -> dict[str, Any]:
        payload = {
            "title": self._self_iteration_fingerprint_text(task.get("title")),
            "file_scope": self._self_iteration_fingerprint_file_scope(task.get("file_scope")),
            "acceptance_commands": self._self_iteration_fingerprint_commands(task.get("acceptance")),
            "e2e_commands": self._self_iteration_fingerprint_commands(task.get("e2e")),
        }
        if include_task_ids:
            payload["id"] = self._self_iteration_fingerprint_text(task.get("id"))
        return payload

    def _self_iteration_fingerprint_file_scope(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized = [self._self_iteration_fingerprint_text(item) for item in value]
        return sorted({item for item in normalized if item})

    def _self_iteration_fingerprint_commands(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        commands: list[str] = []
        for item in value:
            command = item.get("command") if isinstance(item, dict) else item
            normalized = self._self_iteration_fingerprint_text(command)
            if normalized:
                commands.append(normalized)
        return commands

    def _self_iteration_fingerprint_text(self, value: Any) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", redact(str(value))).strip().casefold()

    def _self_iteration_manifest_context(
        self,
        *,
        recent_manifest_count: int | None = None,
        manifest_run_count: int | None = None,
    ) -> dict[str, Any]:
        index = self._build_manifest_index()
        recent_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["recent_manifest_count"]
            if recent_manifest_count is None
            else max(0, int(recent_manifest_count))
        )
        run_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["manifest_run_count"]
            if manifest_run_count is None
            else max(0, int(manifest_run_count))
        )
        recent_entries = list(reversed(index.get("manifests", [])))[:recent_limit]
        recent_manifests = []
        for entry in recent_entries:
            manifest_path = self.project_root / str(entry.get("manifest_path", ""))
            try:
                manifest = load_mapping(manifest_path)
            except Exception as exc:
                recent_manifests.append(
                    {
                        **entry,
                        "load_error": self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"]),
                    }
                )
                continue
            recent_manifests.append(self._self_iteration_manifest_summary(entry, manifest, manifest_run_count=run_limit))
        return {
            "index": {
                "path": index.get("manifest_index_path"),
                "manifest_count": index.get("manifest_count", 0),
                "latest_manifest": index.get("latest_manifest"),
                "latest_by_task": index.get("latest_by_task", {}),
                "status_counts": index.get("status_counts", {}),
                "policy_decision_summary": index.get("policy_decision_summary", {}),
            },
            "recent_task_manifests": recent_manifests,
        }

    def _self_iteration_manifest_summary(
        self,
        entry: dict[str, Any],
        manifest: dict[str, Any],
        *,
        manifest_run_count: int | None = None,
    ) -> dict[str, Any]:
        runs = manifest.get("runs", []) if isinstance(manifest.get("runs"), list) else []
        git = manifest.get("git", {}) if isinstance(manifest.get("git"), dict) else {}
        run_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["manifest_run_count"]
            if manifest_run_count is None
            else max(0, int(manifest_run_count))
        )
        return {
            "manifest_path": entry.get("manifest_path") or manifest.get("manifest_path"),
            "report_path": entry.get("report_path") or manifest.get("report_path"),
            "task_id": entry.get("task_id") or manifest.get("task_id"),
            "task_title": entry.get("task_title"),
            "milestone_id": entry.get("milestone_id") or manifest.get("milestone_id"),
            "status": entry.get("status") or manifest.get("status"),
            "message": self._truncate_text(
                str(manifest.get("message", "")),
                SELF_ITERATION_CONTEXT_LIMITS["message_chars"],
            ),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "attempt": manifest.get("attempt"),
            "run_count": len(runs),
            "policy_decision_summary": manifest.get("policy_decision_summary", {}),
            "runs": [
                {
                    "phase": str(run.get("phase", "")),
                    "name": str(run.get("name", "")),
                    "executor": str(run.get("executor", "")),
                    "status": str(run.get("status", "")),
                    "returncode": run.get("returncode"),
                    "user_experience_gate": deepcopy(run.get("user_experience_gate"))
                    if isinstance(run.get("user_experience_gate"), dict)
                    else {},
                }
                for run in runs[:run_limit]
                if isinstance(run, dict)
            ],
            "run_count_truncated": len(runs) > run_limit,
            "git": {
                "is_repository": bool(git.get("is_repository", False)),
                "branch": git.get("branch"),
                "head": git.get("head"),
                "short_head": git.get("short_head"),
            },
        }

    def _self_iteration_report_context(self) -> dict[str, Any]:
        task_reports = self._self_iteration_recent_reports(self.report_dir.glob("*.md"))
        drive_reports = self._self_iteration_recent_reports((self.report_dir / "drives").glob("*.md"))
        self_iteration_reports = self._self_iteration_recent_reports(
            (self.report_dir / "assessments").glob("*-self-iteration.md")
        )
        return {
            "task_reports": task_reports,
            "drive_reports": drive_reports,
            "self_iteration_reports": self_iteration_reports,
        }

    def _self_iteration_recent_reports(self, paths: Any) -> dict[str, Any]:
        report_paths = sorted(
            [path for path in paths if isinstance(path, Path) and path.is_file()],
            key=self._project_relative_path,
        )
        recent = list(reversed(report_paths))[: SELF_ITERATION_CONTEXT_LIMITS["recent_report_count"]]
        return {
            "total_count": len(report_paths),
            "included_count": len(recent),
            "files": [self._self_iteration_report_file_summary(path) for path in recent],
        }

    def _self_iteration_report_file_summary(self, path: Path) -> dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        return {
            "path": self._project_relative_path(path),
            "bytes": size,
            "title": self._markdown_title(path),
        }

    def _runtime_path_relative_to(self, root: Path, path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    def _runtime_enriched_report_context(self) -> dict[str, Any]:
        return {
            "task_reports": self._runtime_recent_report_context(self.report_dir.glob("*.md")),
            "drive_reports": self._runtime_recent_report_context((self.report_dir / "drives").glob("*.md")),
        }

    def _runtime_recent_report_context(self, paths: Any) -> dict[str, Any]:
        report_paths = sorted(
            [path for path in paths if isinstance(path, Path) and path.is_file()],
            key=self._project_relative_path,
        )
        recent = list(reversed(report_paths))[: SELF_ITERATION_CONTEXT_LIMITS["recent_report_count"]]
        return {
            "total_count": len(report_paths),
            "included_count": len(recent),
            "files": [self._runtime_report_file_summary(path, root=self.project_root) for path in recent],
        }

    def _runtime_report_file_summary(self, path: Path, *, root: Path) -> dict[str, Any]:
        item = {
            "path": self._runtime_path_relative_to(root, path),
            "bytes": self._file_size(path),
            "title": self._markdown_title(path),
        }
        sidecar_path = path.with_suffix(".json")
        if not sidecar_path.exists():
            return item
        item["json_path"] = self._runtime_path_relative_to(root, sidecar_path)
        try:
            sidecar = load_mapping(sidecar_path)
        except Exception as exc:
            item["json_error"] = self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"])
            return item
        compact = self._runtime_compact_report_sidecar(sidecar)
        if compact:
            item["sidecar"] = compact
        return item

    def _runtime_compact_report_sidecar(self, sidecar: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in (
            "kind",
            "project",
            "status",
            "message",
            "scheduler_policy",
            "started_at",
            "finished_at",
            "drive_report",
            "drive_report_json",
            "dispatch_report",
            "dispatch_report_json",
        ):
            if sidecar.get(key) is not None:
                compact[key] = sidecar.get(key)
        checkpoint_readiness = sidecar.get("checkpoint_readiness")
        if isinstance(checkpoint_readiness, dict):
            compact["checkpoint_readiness"] = {
                key: deepcopy(checkpoint_readiness.get(key))
                for key in (
                    "ready",
                    "blocking",
                    "reason",
                    "dirty_paths",
                    "blocking_paths",
                    "safe_to_checkpoint_paths",
                    "recommended_action",
                )
            }
        results = sidecar.get("results")
        if isinstance(results, list):
            compact["result_count"] = len(results)
        continuations = sidecar.get("continuations")
        if isinstance(continuations, list):
            compact["continuation_count"] = len(continuations)
        self_iterations = sidecar.get("self_iterations")
        if isinstance(self_iterations, list):
            compact["self_iteration_count"] = len(self_iterations)
            for iteration in reversed(self_iterations):
                if not isinstance(iteration, dict):
                    continue
                latest_iteration = {
                    "status": iteration.get("status"),
                    "message": iteration.get("message"),
                    "report": iteration.get("report"),
                    "report_json": iteration.get("report_json"),
                }
                if isinstance(iteration.get("checkpoint_readiness"), dict):
                    latest_iteration["checkpoint_readiness"] = self._compact_checkpoint_readiness(
                        iteration.get("checkpoint_readiness")
                    )
                if isinstance(iteration.get("checkpoint_gate"), dict):
                    latest_iteration["checkpoint_gate"] = deepcopy(iteration.get("checkpoint_gate"))
                compact["latest_self_iteration"] = {
                    key: value for key, value in latest_iteration.items() if value is not None
                }
                break
        queue = sidecar.get("queue")
        if isinstance(queue, list):
            compact["queue_count"] = len(queue)
        recoveries = sidecar.get("stale_running_recoveries")
        if isinstance(recoveries, list):
            compact["stale_running_recovery_count"] = len(recoveries)
        blocks = sidecar.get("stale_running_blocks")
        if isinstance(blocks, list):
            compact["stale_running_block_count"] = len(blocks)
        selected = sidecar.get("selected")
        if isinstance(selected, dict):
            compact["selected"] = {
                key: selected.get(key)
                for key in (
                    "project",
                    "root",
                    "queue_index",
                    "scheduler_rank",
                    "scheduler_policy",
                    "score",
                    "selected_reason",
                    "backoff",
                    "checkpoint_readiness",
                    "stale_running_recovery",
                    "stale_running_preflight",
                    "drive_status",
                    "drive_report",
                    "drive_report_json",
                )
                if selected.get(key) is not None
            }
        retrospective = sidecar.get("goal_gap_retrospective")
        if isinstance(retrospective, dict):
            request = retrospective.get("request_self_iteration")
            trigger = retrospective.get("trigger") if isinstance(retrospective.get("trigger"), dict) else {}
            compact["goal_gap_retrospective"] = {
                "stop_class": trigger.get("stop_class"),
                "remaining_risk_count": len(retrospective.get("remaining_risks", [])),
                "next_action_count": len(retrospective.get("likely_next_stage_themes", [])),
                "request_self_iteration": bool(request.get("recommended")) if isinstance(request, dict) else False,
            }
        return compact

    def _runtime_workspace_dispatch_candidate_roots(self) -> list[Path]:
        candidates = [self.project_root, *self.project_root.parents]
        seen: set[str] = set()
        roots: list[Path] = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            roots.append(resolved)
        return roots

    def _runtime_workspace_dispatch_root(self) -> Path | None:
        for candidate in self._runtime_workspace_dispatch_candidate_roots():
            engineering = candidate / ".engineering"
            if (engineering / "reports" / "workspace-dispatches").exists():
                return candidate
            if (engineering / "state" / WORKSPACE_DISPATCH_LEASE_DIRNAME / "lease.json").exists():
                return candidate
        return None

    def _runtime_workspace_dispatch_summary(self) -> dict[str, Any]:
        workspace = self._runtime_workspace_dispatch_root()
        if workspace is None:
            return {
                "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
                "status": "not_found",
                "workspace_root": None,
                "scheduler_policy": None,
                "lease": {"status": "missing", "active": False, "path": None},
                "latest_reports": {"total_count": 0, "included_count": 0, "files": []},
                "latest_report": None,
                "queue_count": 0,
                "queue": [],
                "queue_summary": None,
                "selected": None,
                "latest_report_lease": None,
                "stale_running_recoveries": [],
                "stale_running_blocks": [],
            }

        reports = self._runtime_workspace_dispatch_reports(workspace)
        latest_report = reports["files"][0] if reports.get("files") else None
        latest_payload = self._runtime_load_workspace_dispatch_payload(workspace, latest_report)
        queue = self._runtime_compact_workspace_dispatch_queue(latest_payload)
        latest_report_lease = (
            deepcopy(latest_payload.get("lease"))
            if isinstance(latest_payload, dict) and isinstance(latest_payload.get("lease"), dict)
            else None
        )
        lease = self._runtime_workspace_dispatch_lease_summary(workspace)
        if lease.get("active"):
            status = "active_lease"
        elif latest_report is not None:
            status = "reported"
        else:
            status = "no_dispatch_reports"
        return {
            "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
            "status": status,
            "workspace_root": str(workspace),
            "scheduler_policy": (
                latest_payload.get("scheduler_policy")
                if isinstance(latest_payload, dict)
                else None
            ),
            "lease": lease,
            "latest_reports": reports,
            "latest_report": latest_report,
            "queue_count": len(queue),
            "queue": queue,
            "queue_summary": deepcopy(latest_payload.get("queue_summary"))
            if isinstance(latest_payload, dict) and isinstance(latest_payload.get("queue_summary"), dict)
            else None,
            "selected": deepcopy(latest_payload.get("selected")) if isinstance(latest_payload, dict) else None,
            "latest_report_lease": latest_report_lease,
            "stale_running_recoveries": deepcopy(latest_payload.get("stale_running_recoveries"))
            if isinstance(latest_payload, dict) and isinstance(latest_payload.get("stale_running_recoveries"), list)
            else [],
            "stale_running_blocks": deepcopy(latest_payload.get("stale_running_blocks"))
            if isinstance(latest_payload, dict) and isinstance(latest_payload.get("stale_running_blocks"), list)
            else [],
        }

    def _runtime_workspace_dispatch_reports(self, workspace: Path) -> dict[str, Any]:
        report_dir = workspace / ".engineering" / "reports" / "workspace-dispatches"
        paths = sorted(
            [path for path in report_dir.glob("*.md") if path.is_file()] if report_dir.exists() else [],
            key=lambda path: self._runtime_path_relative_to(workspace, path),
        )
        recent = list(reversed(paths))[:WORKSPACE_DISPATCH_REPORT_LIMIT]
        return {
            "total_count": len(paths),
            "included_count": len(recent),
            "files": [self._runtime_report_file_summary(path, root=workspace) for path in recent],
        }

    def _runtime_load_workspace_dispatch_payload(
        self,
        workspace: Path,
        latest_report: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(latest_report, dict):
            return None
        json_path = latest_report.get("json_path")
        if not json_path:
            return None
        candidate = Path(str(json_path))
        path = candidate if candidate.is_absolute() else workspace / candidate
        try:
            payload = load_mapping(path)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _runtime_compact_workspace_dispatch_queue(self, payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("queue"), list):
            return []
        compact: list[dict[str, Any]] = []
        for item in payload["queue"][:25]:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
            next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
            checkpoint_readiness = (
                item.get("checkpoint_readiness")
                if isinstance(item.get("checkpoint_readiness"), dict)
                else summary.get("checkpoint_readiness")
                if isinstance(summary.get("checkpoint_readiness"), dict)
                else {}
            )
            drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
            stale_running_recovery = (
                item.get("stale_running_recovery")
                if isinstance(item.get("stale_running_recovery"), dict)
                else drive_control.get("stale_running_recovery")
                if isinstance(drive_control.get("stale_running_recovery"), dict)
                else None
            )
            stale_running_preflight = (
                item.get("stale_running_preflight")
                if isinstance(item.get("stale_running_preflight"), dict)
                else drive_control.get("stale_running_preflight")
                if isinstance(drive_control.get("stale_running_preflight"), dict)
                else None
            )
            stale_running_block = (
                item.get("stale_running_block")
                if isinstance(item.get("stale_running_block"), dict)
                else drive_control.get("stale_running_block")
                if isinstance(drive_control.get("stale_running_block"), dict)
                else None
            )
            compact.append(
                {
                    "index": item.get("index"),
                    "scheduler_rank": item.get("scheduler_rank"),
                    "project": item.get("project"),
                    "root": item.get("root"),
                    "eligible": bool(item.get("eligible", False)),
                    "selected": bool(item.get("selected", False)),
                    "dispatch_status": item.get("dispatch_status"),
                    "scheduler_policy": item.get("scheduler_policy"),
                    "score": item.get("score"),
                    "score_components": deepcopy(item.get("score_components"))
                    if isinstance(item.get("score_components"), dict)
                    else {},
                    "priority": deepcopy(item.get("priority"))
                    if isinstance(item.get("priority"), dict)
                    else {},
                    "resource_budget": deepcopy(item.get("resource_budget"))
                    if isinstance(item.get("resource_budget"), dict)
                    else {},
                    "project_lease": deepcopy(item.get("project_lease"))
                    if isinstance(item.get("project_lease"), dict)
                    else {},
                    "retry_backoff_summary": deepcopy(item.get("retry_backoff_summary"))
                    if isinstance(item.get("retry_backoff_summary"), dict)
                    else {},
                    "selected_reason": deepcopy(item.get("selected_reason"))
                    if isinstance(item.get("selected_reason"), dict)
                    else None,
                    "backoff": deepcopy(item.get("backoff")) if isinstance(item.get("backoff"), dict) else None,
                    "checkpoint_readiness": deepcopy(checkpoint_readiness),
                    "stale_running_recovery": deepcopy(stale_running_recovery)
                    if isinstance(stale_running_recovery, dict)
                    else None,
                    "stale_running_preflight": deepcopy(stale_running_preflight)
                    if isinstance(stale_running_preflight, dict)
                    else None,
                    "stale_running_block": deepcopy(stale_running_block)
                    if isinstance(stale_running_block, dict)
                    else None,
                    "skip_codes": [
                        str(reason.get("code"))
                        for reason in item.get("skip_reasons", [])
                        if isinstance(reason, dict) and reason.get("code")
                    ],
                    "next_task": (
                        {
                            "id": next_task.get("id"),
                            "title": next_task.get("title"),
                            "milestone_id": next_task.get("milestone_id"),
                        }
                        if next_task
                        else None
                    ),
                }
            )
        return compact

    def _runtime_workspace_dispatch_lease_summary(self, workspace: Path) -> dict[str, Any]:
        lease_path = workspace / ".engineering" / "state" / WORKSPACE_DISPATCH_LEASE_DIRNAME / "lease.json"
        path = self._runtime_path_relative_to(workspace, lease_path)
        if not lease_path.exists():
            return {"status": "missing", "active": False, "path": path}
        try:
            lease = load_mapping(lease_path)
        except Exception as exc:
            return {
                "status": "invalid",
                "active": True,
                "path": path,
                "error": self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"]),
            }
        if not isinstance(lease, dict):
            return {"status": "invalid", "active": True, "path": path}

        threshold = self._coerce_optional_nonnegative_seconds(lease.get("stale_after_seconds")) or 0
        pid = self._coerce_pid(lease.get("owner_pid"))
        pid_alive = self._process_is_running(pid)
        heartbeat_at = lease.get("last_heartbeat_at")
        heartbeat_dt = parse_utc_timestamp(heartbeat_at)
        heartbeat_age_seconds = None
        if heartbeat_dt is not None:
            heartbeat_age_seconds = max(0, int((datetime.now(timezone.utc) - heartbeat_dt).total_seconds()))
        stale = False
        reason = None
        if pid is None:
            stale = True
            reason = "missing_pid"
        elif pid_alive is False:
            stale = True
            reason = "pid_gone"
        elif heartbeat_dt is None:
            stale = True
            reason = "missing_heartbeat"
        elif threshold and heartbeat_age_seconds is not None and heartbeat_age_seconds > threshold:
            stale = True
            reason = "heartbeat_stale"
        holder_keys = (
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
        )
        return {
            "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
            "status": "stale" if stale else "held",
            "active": True,
            "path": path,
            "stale": stale,
            "reason": reason,
            "pid": pid,
            "pid_alive": pid_alive,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "threshold_seconds": threshold,
            "holder": {key: deepcopy(lease.get(key)) for key in holder_keys if key in lease},
        }

    def _runtime_daemon_supervisor_root(self) -> Path | None:
        for candidate in self._runtime_workspace_dispatch_candidate_roots():
            engineering = candidate / ".engineering"
            if (engineering / "state" / DAEMON_SUPERVISOR_RUNTIME_STATE_FILENAME).exists():
                return candidate
            if (engineering / "reports" / DAEMON_SUPERVISOR_RUNTIME_REPORT_DIRNAME).exists():
                return candidate
        return None

    def _runtime_daemon_supervisor_summary(self) -> dict[str, Any]:
        workspace = self._runtime_daemon_supervisor_root()
        if workspace is None:
            return {
                "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
                "kind": "engineering-harness.daemon-supervisor-runtime-summary",
                "status": "not_found",
                "workspace_root": None,
                "state_path": None,
                "state": None,
                "active": False,
                "run_window": None,
                "restartable_loop": None,
                "last_tick": None,
                "last_decision": None,
                "stop_reason": None,
                "latest_reports": {"total_count": 0, "included_count": 0, "files": []},
                "latest_report": None,
            }

        state_path = workspace / ".engineering" / "state" / DAEMON_SUPERVISOR_RUNTIME_STATE_FILENAME
        state = self._runtime_load_daemon_supervisor_state(state_path)
        reports = self._runtime_daemon_supervisor_reports(workspace)
        latest_report = reports["files"][0] if reports.get("files") else None
        latest_payload = self._runtime_load_daemon_supervisor_payload(workspace, latest_report)
        source = state if isinstance(state, dict) else latest_payload if isinstance(latest_payload, dict) else {}
        active = bool(source.get("active", False)) or str(source.get("status") or "") == "running"
        status = "running" if active else str(source.get("status") or ("reported" if latest_report else "missing"))
        return {
            "schema_version": DAEMON_SUPERVISOR_RUNTIME_SCHEMA_VERSION,
            "kind": "engineering-harness.daemon-supervisor-runtime-summary",
            "status": status,
            "workspace_root": str(workspace),
            "state_path": self._runtime_path_relative_to(workspace, state_path),
            "state": self._runtime_compact_daemon_supervisor_state(state),
            "active": active,
            "run_window": deepcopy(source.get("run_window")) if isinstance(source.get("run_window"), dict) else None,
            "restartable_loop": self._runtime_compact_daemon_supervisor_restartable_loop(
                source.get("restartable_loop") if isinstance(source.get("restartable_loop"), dict) else None
            ),
            "last_tick": deepcopy(source.get("last_tick")) if isinstance(source.get("last_tick"), dict) else None,
            "last_decision": deepcopy(source.get("last_decision")) if isinstance(source.get("last_decision"), dict) else None,
            "stop_reason": deepcopy(source.get("stop_reason")) if isinstance(source.get("stop_reason"), dict) else None,
            "latest_reports": reports,
            "latest_report": latest_report,
            "latest_report_payload": self._runtime_compact_daemon_supervisor_payload(latest_payload),
        }

    def _runtime_load_daemon_supervisor_state(self, state_path: Path) -> dict[str, Any] | None:
        if not state_path.exists():
            return None
        try:
            payload = load_mapping(state_path)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _runtime_daemon_supervisor_reports(self, workspace: Path) -> dict[str, Any]:
        report_dir = workspace / ".engineering" / "reports" / DAEMON_SUPERVISOR_RUNTIME_REPORT_DIRNAME
        paths = sorted(
            [path for path in report_dir.glob("*.md") if path.is_file()] if report_dir.exists() else [],
            key=lambda path: self._runtime_path_relative_to(workspace, path),
        )
        recent = list(reversed(paths))[:DAEMON_SUPERVISOR_RUNTIME_REPORT_LIMIT]
        return {
            "total_count": len(paths),
            "included_count": len(recent),
            "files": [self._runtime_report_file_summary(path, root=workspace) for path in recent],
        }

    def _runtime_load_daemon_supervisor_payload(
        self,
        workspace: Path,
        latest_report: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(latest_report, dict):
            return None
        json_path = latest_report.get("json_path")
        if not json_path:
            return None
        candidate = Path(str(json_path))
        path = candidate if candidate.is_absolute() else workspace / candidate
        try:
            payload = load_mapping(path)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _runtime_compact_daemon_supervisor_state(self, state: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(state, dict):
            return None
        keys = (
            "schema_version",
            "kind",
            "workspace",
            "status",
            "active",
            "owner_pid",
            "loop_id",
            "generation",
            "started_at",
            "finished_at",
            "last_heartbeat_at",
            "heartbeat_count",
            "current_activity",
            "latest_report",
            "latest_report_json",
        )
        compact = {key: deepcopy(state.get(key)) for key in keys if key in state}
        if isinstance(state.get("run_window"), dict):
            compact["run_window"] = deepcopy(state.get("run_window"))
        if isinstance(state.get("last_decision"), dict):
            compact["last_decision"] = deepcopy(state.get("last_decision"))
        if isinstance(state.get("stop_reason"), dict):
            compact["stop_reason"] = deepcopy(state.get("stop_reason"))
        if isinstance(state.get("last_tick"), dict):
            last_tick = deepcopy(state.get("last_tick"))
            compact["last_tick"] = {
                key: last_tick.get(key)
                for key in (
                    "tick_index",
                    "dispatch_status",
                    "dispatch_exit_code",
                    "dispatch_report",
                    "dispatch_report_json",
                    "drive_status",
                    "drive_report",
                    "drive_report_json",
                )
                if last_tick.get(key) is not None
            }
        return compact

    def _runtime_compact_daemon_supervisor_restartable_loop(
        self,
        restartable: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(restartable, dict):
            return None
        completed = restartable.get("completed_dispatch_reports")
        if not isinstance(completed, list):
            completed = []
        return {
            "schema_version": restartable.get("schema_version"),
            "generation": restartable.get("generation"),
            "resume_count": restartable.get("resume_count"),
            "resumed_from": deepcopy(restartable.get("resumed_from"))
            if isinstance(restartable.get("resumed_from"), dict)
            else None,
            "recovered_previous": deepcopy(restartable.get("recovered_previous"))
            if isinstance(restartable.get("recovered_previous"), dict)
            else None,
            "completed_dispatch_report_count": len(completed),
            "completed_dispatch_reports": [
                deepcopy(item) for item in completed[-DAEMON_SUPERVISOR_RUNTIME_REPORT_LIMIT:] if isinstance(item, dict)
            ],
        }

    def _runtime_compact_daemon_supervisor_payload(
        self,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        ticks = payload.get("ticks") if isinstance(payload.get("ticks"), list) else []
        return {
            "status": payload.get("status"),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "runtime_report": payload.get("runtime_report"),
            "runtime_report_json": payload.get("runtime_report_json"),
            "tick_count": len(ticks),
            "run_window": deepcopy(payload.get("run_window")) if isinstance(payload.get("run_window"), dict) else None,
            "last_decision": deepcopy(payload.get("last_decision"))
            if isinstance(payload.get("last_decision"), dict)
            else None,
            "stop_reason": deepcopy(payload.get("stop_reason"))
            if isinstance(payload.get("stop_reason"), dict)
            else None,
        }

    def _self_iteration_docs_context(self) -> dict[str, Any]:
        blueprint = self._self_iteration_blueprint_context()
        blueprint_path = str(blueprint.get("path") or "")
        docs = self._self_iteration_doc_paths()
        docs.sort(key=lambda path: self._self_iteration_doc_sort_key(path, blueprint_path))
        included = docs[: SELF_ITERATION_CONTEXT_LIMITS["doc_count"]]
        return {
            "blueprint": blueprint,
            "relevant_docs": {
                "total_count": len(docs),
                "included_count": len(included),
                "files": [self._self_iteration_doc_summary(path) for path in included],
            },
        }

    def _self_iteration_blueprint_context(self) -> dict[str, Any]:
        path_value = self._self_iteration_blueprint_value()
        if not path_value:
            return {"path": None, "exists": False, "excerpt": ""}
        candidate = self._self_iteration_project_path(path_value)
        if candidate is None:
            return {"path": str(path_value), "exists": False, "error": "blueprint path is outside the project root"}
        payload = {
            "path": self._project_relative_path(candidate),
            "exists": candidate.exists() and candidate.is_file(),
        }
        if payload["exists"]:
            payload.update(self._text_excerpt_payload(candidate, SELF_ITERATION_CONTEXT_LIMITS["doc_excerpt_chars"]))
        return payload

    def _self_iteration_blueprint_value(self) -> str | None:
        for container_name, key in (
            ("goal", "blueprint"),
            ("generated_from", "blueprint_path"),
            ("continuation", "blueprint"),
        ):
            container = self.roadmap.get(container_name)
            if isinstance(container, dict):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _self_iteration_doc_paths(self) -> list[Path]:
        docs = self._iter_project_files(("docs",), extensions=SELF_ITERATION_DOC_EXTENSIONS)
        for readme_name in ("README.md", "README.rst", "README.txt"):
            readme = self.project_root / readme_name
            if readme.exists() and readme.is_file():
                docs.append(readme)
        unique = {self._project_relative_path(path): path for path in docs}
        return [unique[key] for key in sorted(unique)]

    def _self_iteration_doc_sort_key(self, path: Path, blueprint_path: str) -> tuple[int, str]:
        relative = self._project_relative_path(path)
        lowered = relative.lower()
        if blueprint_path and relative == blueprint_path:
            priority = 0
        elif Path(relative).name.lower().startswith("readme"):
            priority = 1
        elif any(keyword in lowered for keyword in ("blueprint", "roadmap", "planner", "plan", "contract", "design")):
            priority = 2
        else:
            priority = 3
        return (priority, relative)

    def _self_iteration_doc_summary(self, path: Path) -> dict[str, Any]:
        payload = self._text_excerpt_payload(path, SELF_ITERATION_CONTEXT_LIMITS["doc_excerpt_chars"])
        return {
            "path": self._project_relative_path(path),
            "bytes": payload.get("bytes"),
            "excerpt": payload.get("excerpt", ""),
            "excerpt_truncated": payload.get("excerpt_truncated", False),
        }

    def _self_iteration_test_inventory(
        self,
        *,
        test_file_count: int | None = None,
        test_name_count: int | None = None,
    ) -> dict[str, Any]:
        files = self._iter_project_files(("tests",), extensions=SELF_ITERATION_TEST_EXTENSIONS)
        file_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["test_file_count"]
            if test_file_count is None
            else max(0, int(test_file_count))
        )
        name_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["test_name_count"]
            if test_name_count is None
            else max(0, int(test_name_count))
        )
        included = files[:file_limit]
        return {
            "total_count": len(files),
            "included_count": len(included),
            "files": [self._self_iteration_test_file_summary(path, test_name_count=name_limit) for path in included],
        }

    def _self_iteration_test_file_summary(self, path: Path, *, test_name_count: int | None = None) -> dict[str, Any]:
        names = self._test_names(path)
        name_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["test_name_count"]
            if test_name_count is None
            else max(0, int(test_name_count))
        )
        return {
            "path": self._project_relative_path(path),
            "bytes": self._file_size(path),
            "test_count": len(names),
            "tests": names[:name_limit],
            "test_count_truncated": len(names) > name_limit,
        }

    def _self_iteration_source_inventory(self) -> dict[str, Any]:
        files = self._iter_project_files(
            ("src", "app", "lib", "packages", "cli"),
            extensions=SELF_ITERATION_SOURCE_EXTENSIONS,
        )
        for path in self.project_root.iterdir():
            if not path.is_file() or path.suffix.lower() not in SELF_ITERATION_SOURCE_EXTENSIONS:
                continue
            files.append(path)
        unique = {self._project_relative_path(path): path for path in files}
        files = [unique[key] for key in sorted(unique)]
        included = files[: SELF_ITERATION_CONTEXT_LIMITS["source_file_count"]]
        return {
            "total_count": len(files),
            "included_count": len(included),
            "files": [
                {
                    "path": self._project_relative_path(path),
                    "bytes": self._file_size(path),
                }
                for path in included
            ],
        }

    def _self_iteration_git_context(self, *, git_commit_count: int | None = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "is_repository": False,
            "root": None,
            "branch": None,
            "head": None,
            "short_head": None,
            "status": {"returncode": None, "stdout": "", "stderr": ""},
            "status_lines": [],
            "recent_commits": [],
        }
        if not self._is_git_repo():
            return context
        root = self._git(["rev-parse", "--show-toplevel"])
        head = self._git(["rev-parse", "HEAD"])
        short_head = self._git(["rev-parse", "--short", "HEAD"])
        status = self._git(["status", "--short"])
        commit_limit = (
            SELF_ITERATION_CONTEXT_LIMITS["git_commit_count"]
            if git_commit_count is None
            else max(0, int(git_commit_count))
        )
        commits = self._git(["log", "--oneline", f"-{commit_limit}"])
        context.update(
            {
                "is_repository": True,
                "root": root["stdout"].strip() if root["returncode"] == 0 else None,
                "branch": self._current_branch(),
                "head": head["stdout"].strip() if head["returncode"] == 0 else None,
                "short_head": short_head["stdout"].strip() if short_head["returncode"] == 0 else None,
                "status": status,
                "status_lines": [line for line in status["stdout"].splitlines() if line.strip()],
                "recent_commits": [line for line in commits["stdout"].splitlines() if line.strip()],
            }
        )
        return context

    def _iter_project_files(self, roots: tuple[str, ...], *, extensions: set[str]) -> list[Path]:
        files: dict[str, Path] = {}
        for root_name in roots:
            root = self.project_root / root_name
            if root.is_file():
                candidates = [root]
            elif root.exists():
                candidates = [path for path in root.rglob("*") if path.is_file()]
            else:
                candidates = []
            for path in candidates:
                relative = path.relative_to(self.project_root)
                if any(part in PRUNE_DIRS or part == ".engineering" for part in relative.parts):
                    continue
                if path.suffix.lower() not in extensions:
                    continue
                files[str(relative)] = path
        return [files[key] for key in sorted(files)]

    def _self_iteration_project_path(self, path_value: str) -> Path | None:
        if "://" in path_value:
            return None
        path = Path(path_value)
        candidate = path if path.is_absolute() else self.project_root / path
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return None
        if not resolved.is_relative_to(self.project_root):
            return None
        return resolved

    def _text_excerpt_payload(self, path: Path, max_chars: int) -> dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(max_chars + 1)
        except OSError as exc:
            return {
                "bytes": size,
                "excerpt": "",
                "excerpt_truncated": False,
                "read_error": self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"]),
            }
        return {
            "bytes": size,
            "excerpt": self._truncate_text(text, max_chars),
            "excerpt_truncated": len(text) > max_chars,
        }

    def _markdown_title(self, path: Path) -> str:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(4096)
        except OSError:
            return ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return self._truncate_text(stripped.lstrip("#").strip(), 160)
        return ""

    def _test_names(self, path: Path) -> list[str]:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(20000)
        except OSError:
            return []
        names = [
            match.group(1)
            for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", text, re.MULTILINE)
        ]
        names.extend(
            match.group(2)
            for match in re.finditer(r"^\s*(?:test|it)\s*\(\s*(['\"])(.*?)\1", text, re.MULTILINE)
        )
        return [name for name in names if name]

    def _file_size(self, path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    def _truncate_text(self, text: str, max_chars: int) -> str:
        text = redact(text)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    def _redact_context_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                text_key = str(key)
                if (
                    sensitive_evidence_key(text_key)
                    and item is not None
                    and not isinstance(item, bool)
                    and not isinstance(item, (dict, list, tuple))
                ):
                    redacted[text_key] = "[REDACTED]"
                else:
                    redacted[text_key] = self._redact_context_value(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_context_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact_context_value(item) for item in value]
        if isinstance(value, str):
            return redact(value)
        return value

    def _self_iteration_validation_result(
        self,
        *,
        status: str,
        errors: list[str],
        warnings: list[str],
        expected_new_stage_count: int,
        actual_new_stage_count: int,
        new_stage_ids: list[str],
        new_stage_requirement_refs: list[dict[str, Any]] | None = None,
        spec_traceability: dict[str, Any] | None = None,
        roadmap_validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
            "expected_new_stage_count": expected_new_stage_count,
            "actual_new_stage_count": actual_new_stage_count,
            "new_stage_ids": new_stage_ids,
            "new_stage_requirement_refs": new_stage_requirement_refs or [],
            "spec_traceability": spec_traceability or {},
            "roadmap_validation": roadmap_validation,
        }

    def _validate_self_iteration_output(
        self,
        before_roadmap: dict[str, Any],
        after_roadmap: dict[str, Any],
        *,
        expected_new_stage_count: int,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        before_stages = self._self_iteration_continuation_stages(
            before_roadmap,
            location="previous roadmap continuation.stages",
            errors=errors,
        )
        after_stages = self._self_iteration_continuation_stages(
            after_roadmap,
            location="planner output continuation.stages",
            errors=errors,
        )
        existing_stage_count = len(before_stages)
        if len(after_stages) < existing_stage_count:
            errors.append("planner output removed existing continuation stages")
            new_stages: list[Any] = []
        else:
            existing_prefix = after_stages[:existing_stage_count]
            if existing_prefix != before_stages:
                errors.append("planner output mutated existing continuation stages; only appends are allowed")
            new_stages = after_stages[existing_stage_count:]
        new_stage_requirement_refs = self._self_iteration_stage_requirement_ref_summaries(new_stages)

        if self._self_iteration_without_new_stages(after_roadmap, existing_stage_count) != before_roadmap:
            if after_roadmap.get("milestones", []) != before_roadmap.get("milestones", []):
                errors.append("planner output mutated existing milestones; only continuation.stages may be appended")
            else:
                errors.append("planner output changed existing roadmap fields; only continuation.stages may be appended")

        new_stage_ids: list[str] = []
        if len(new_stages) != expected_new_stage_count:
            errors.append(
                f"expected exactly {expected_new_stage_count} new continuation stage(s), found {len(new_stages)}"
            )

        spec_traceability = self._self_iteration_spec_traceability_context(before_roadmap)
        existing_ids = self._self_iteration_existing_ids(before_roadmap)
        existing_task_ids = self._self_iteration_existing_task_ids(before_roadmap)
        existing_stage_fingerprints = self._self_iteration_stage_fingerprint_index(before_stages)
        seen_new_ids: set[str] = set()
        seen_new_fingerprints: dict[str, str] = {}
        materialized_stage_ids = self._self_iteration_milestone_ids(after_roadmap)
        for offset, stage in enumerate(new_stages):
            stage_index = existing_stage_count + offset
            location = f"continuation.stages[{stage_index}]"
            if not isinstance(stage, dict):
                errors.append(f"{location} must be a mapping")
                continue
            stage_id = str(stage.get("id", "")).strip()
            stage_label = stage_id or f"stage-{stage_index}"
            stage_fingerprint = self._self_iteration_stage_fingerprint(stage)
            existing_fingerprint_stage_ids = existing_stage_fingerprints.get(stage_fingerprint, [])
            if existing_fingerprint_stage_ids:
                errors.append(
                    f"new continuation stage `{stage_label}` duplicates existing continuation stage plan "
                    f"`{existing_fingerprint_stage_ids[0]}` (fingerprint {stage_fingerprint[:12]})"
                )
            if stage_fingerprint in seen_new_fingerprints:
                errors.append(
                    f"new continuation stage `{stage_label}` duplicates new continuation stage plan "
                    f"`{seen_new_fingerprints[stage_fingerprint]}` (fingerprint {stage_fingerprint[:12]})"
                )
            else:
                seen_new_fingerprints[stage_fingerprint] = stage_label
            if not stage_id:
                errors.append(f"{location}.id is required")
            else:
                new_stage_ids.append(stage_id)
                if stage_id in existing_ids:
                    errors.append(f"new continuation stage id duplicates an existing id: {stage_id}")
                if stage_id in seen_new_ids:
                    errors.append(f"duplicate new continuation id: {stage_id}")
                if stage_id in materialized_stage_ids:
                    errors.append(f"new continuation stage `{stage_id}` was also materialized as a milestone")
                seen_new_ids.add(stage_id)
            stage_status = str(stage.get("status", "planned")).strip()
            if stage_status and stage_status not in {"planned", "pending"}:
                errors.append(f"new continuation stage `{stage_id or stage_index}` status must be planned or pending")
            self._validate_self_iteration_new_stage_spec_refs(
                stage,
                stage_id=stage_id or f"stage-{stage_index}",
                location=location,
                spec_traceability=spec_traceability,
                errors=errors,
            )
            self._validate_self_iteration_new_stage(
                stage,
                stage_id=stage_id or f"stage-{stage_index}",
                location=location,
                existing_ids=existing_ids,
                existing_task_ids=existing_task_ids,
                seen_new_ids=seen_new_ids,
                errors=errors,
                warnings=warnings,
            )

        if errors:
            return self._self_iteration_validation_result(
                status="failed",
                errors=errors,
                warnings=warnings,
                expected_new_stage_count=expected_new_stage_count,
                actual_new_stage_count=len(new_stages),
                new_stage_ids=new_stage_ids,
                new_stage_requirement_refs=new_stage_requirement_refs,
                spec_traceability=spec_traceability,
            )

        current_roadmap = self.roadmap
        self.roadmap = after_roadmap
        try:
            roadmap_validation = self.validate_roadmap()
        finally:
            self.roadmap = current_roadmap
        if roadmap_validation["status"] != "passed":
            errors.extend(f"roadmap validation: {error}" for error in roadmap_validation.get("errors", []))
        warnings.extend(f"roadmap validation: {warning}" for warning in roadmap_validation.get("warnings", []))
        return self._self_iteration_validation_result(
            status="passed" if not errors else "failed",
            errors=errors,
            warnings=warnings,
            expected_new_stage_count=expected_new_stage_count,
            actual_new_stage_count=len(new_stages),
            new_stage_ids=new_stage_ids,
            new_stage_requirement_refs=new_stage_requirement_refs,
            spec_traceability=spec_traceability,
            roadmap_validation=roadmap_validation,
        )

    def _self_iteration_continuation_stages(
        self,
        roadmap: dict[str, Any],
        *,
        location: str,
        errors: list[str],
    ) -> list[Any]:
        continuation = roadmap.get("continuation", {})
        if continuation is None:
            return []
        if not isinstance(continuation, dict):
            errors.append(f"{location} parent must be a mapping")
            return []
        stages = continuation.get("stages", [])
        if stages is None:
            return []
        if not isinstance(stages, list):
            errors.append(f"{location} must be a list")
            return []
        return stages

    def _self_iteration_without_new_stages(
        self,
        roadmap: dict[str, Any],
        existing_stage_count: int,
    ) -> dict[str, Any]:
        payload = deepcopy(roadmap)
        continuation = payload.get("continuation")
        if isinstance(continuation, dict):
            stages = continuation.get("stages", [])
            if isinstance(stages, list):
                continuation["stages"] = deepcopy(stages[:existing_stage_count])
        return payload

    def _self_iteration_milestone_ids(self, roadmap: dict[str, Any]) -> set[str]:
        milestones = roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            return set()
        return {
            str(milestone.get("id", "")).strip()
            for milestone in milestones
            if isinstance(milestone, dict) and str(milestone.get("id", "")).strip()
        }

    def _self_iteration_existing_ids(self, roadmap: dict[str, Any]) -> set[str]:
        ids: set[str] = set()
        milestones = roadmap.get("milestones", [])
        if isinstance(milestones, list):
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                milestone_id = str(milestone.get("id", "")).strip()
                if milestone_id:
                    ids.add(milestone_id)
                tasks = milestone.get("tasks", [])
                if isinstance(tasks, list):
                    ids.update(
                        str(task.get("id", "")).strip()
                        for task in tasks
                        if isinstance(task, dict) and str(task.get("id", "")).strip()
                    )
        continuation = roadmap.get("continuation", {})
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                stage_id = str(stage.get("id", "")).strip()
                if stage_id:
                    ids.add(stage_id)
                tasks = stage.get("tasks", [])
                if isinstance(tasks, list):
                    ids.update(
                        str(task.get("id", "")).strip()
                        for task in tasks
                        if isinstance(task, dict) and str(task.get("id", "")).strip()
                    )
        return ids

    def _self_iteration_existing_task_ids(self, roadmap: dict[str, Any]) -> set[str]:
        task_ids: set[str] = set()
        milestones = roadmap.get("milestones", [])
        if isinstance(milestones, list):
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                tasks = milestone.get("tasks", [])
                if isinstance(tasks, list):
                    task_ids.update(
                        str(task.get("id", "")).strip()
                        for task in tasks
                        if isinstance(task, dict) and str(task.get("id", "")).strip()
                    )
        continuation = roadmap.get("continuation", {})
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                tasks = stage.get("tasks", [])
                if isinstance(tasks, list):
                    task_ids.update(
                        str(task.get("id", "")).strip()
                        for task in tasks
                        if isinstance(task, dict) and str(task.get("id", "")).strip()
                    )
        return task_ids

    def _validate_self_iteration_new_stage(
        self,
        stage: dict[str, Any],
        *,
        stage_id: str,
        location: str,
        existing_ids: set[str],
        existing_task_ids: set[str],
        seen_new_ids: set[str],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        self._validate_self_iteration_unsafe_requirements(stage, location=location, errors=errors)
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list) or not tasks:
            errors.append(f"new continuation stage `{stage_id}` must define at least one task")
            return
        for task_index, task in enumerate(tasks):
            task_location = f"{location}.tasks[{task_index}]"
            if not isinstance(task, dict):
                errors.append(f"{task_location} must be a mapping")
                continue
            task_id = str(task.get("id", "")).strip()
            if not task_id:
                errors.append(f"{task_location}.id is required")
                task_id = f"{stage_id}-task-{task_index}"
            elif task_id in existing_task_ids:
                errors.append(f"new continuation task id duplicates an existing roadmap task id: {task_id}")
            elif task_id in existing_ids:
                errors.append(f"new continuation task id duplicates an existing stage id: {task_id}")
            elif task_id in seen_new_ids:
                errors.append(f"duplicate new continuation task id: {task_id}")
            seen_new_ids.add(task_id)
            self._validate_self_iteration_new_task(
                task,
                task_id=task_id,
                location=task_location,
                errors=errors,
                warnings=warnings,
            )

    def _validate_self_iteration_new_task(
        self,
        task: dict[str, Any],
        *,
        task_id: str,
        location: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        status = str(task.get("status", "pending")).strip()
        if status and status not in {"pending", "planned"}:
            errors.append(f"new continuation task `{task_id}` status must be pending or planned")

        file_scope = task.get("file_scope")
        if not isinstance(file_scope, list) or not any(str(item).strip() for item in file_scope):
            errors.append(f"new continuation task `{task_id}` must define non-empty file_scope")

        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(f"new continuation task `{task_id}` must define acceptance commands")
        elif not any(isinstance(item, dict) and str(item.get("command", "")).strip() for item in acceptance):
            errors.append(f"new continuation task `{task_id}` must define at least one acceptance command")

        implementation = task.get("implementation", [])
        if implementation is None:
            implementation = []
        repair = task.get("repair", [])
        if repair is None:
            repair = []
        if implementation:
            if not isinstance(implementation, list):
                errors.append(f"new continuation task `{task_id}` implementation must be a list")
            elif not self._self_iteration_has_codex_entry(implementation):
                errors.append(f"new continuation task `{task_id}` implementation work must use a codex entry")
            if not isinstance(repair, list) or not self._self_iteration_has_codex_entry(repair):
                errors.append(f"new continuation task `{task_id}` implementation work must define a codex repair entry")

        for group_name in ("implementation", "repair", "acceptance", "e2e"):
            group = task.get(group_name, [])
            if group is None:
                continue
            if not isinstance(group, list):
                continue
            for command_index, item in enumerate(group):
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command", "") or "")
                if not command:
                    continue
                outcome, reason, metadata = self._command_policy_match(command)
                if outcome == "requires_approval" or metadata.get("blocked_pattern"):
                    errors.append(
                        f"new continuation task `{task_id}` {group_name}[{command_index}] has unsafe command: {reason}"
                    )
                elif outcome == "denied":
                    warnings.append(
                        f"new continuation task `{task_id}` {group_name}[{command_index}] command is not currently allowlisted: {reason}"
                    )

    def _self_iteration_has_codex_entry(self, items: list[Any]) -> bool:
        return any(
            isinstance(item, dict)
            and str(item.get("executor", "")).strip() == "codex"
            and str(item.get("prompt", "") or item.get("command", "")).strip()
            for item in items
        )

    def _validate_self_iteration_unsafe_requirements(
        self,
        value: Any,
        *,
        location: str,
        errors: list[str],
    ) -> None:
        seen: set[tuple[str, str, str]] = set()
        for text_location, text in self._self_iteration_string_values(value, location=location):
            lowered = text.lower()
            for reason, pattern in SELF_ITERATION_UNSAFE_REQUIREMENT_PATTERNS:
                for match in pattern.finditer(lowered):
                    if self._self_iteration_requirement_is_negated(lowered, match.start()):
                        continue
                    matched_text = text[match.start() : match.end()]
                    key = (text_location, reason, matched_text.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    errors.append(
                        f"{text_location} introduces unsafe {reason} requirement `{matched_text}`"
                    )

    def _self_iteration_string_values(self, value: Any, *, location: str) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                values.extend(self._self_iteration_string_values(item, location=f"{location}.{key}"))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                values.extend(self._self_iteration_string_values(item, location=f"{location}[{index}]"))
        elif isinstance(value, str) and value.strip():
            values.append((location, value))
        return values

    def _self_iteration_requirement_is_negated(self, text: str, match_start: int) -> bool:
        prefix = text[max(0, match_start - 120) : match_start]
        sentence_prefix = re.split(r"[.;\n]", prefix)[-1]
        return bool(
            re.search(
                r"\b(no|not|never|without|avoid|exclude|excluding|do not|don't|does not|must not|"
                r"should not|cannot|can't|free of|free from)\b",
                sentence_prefix,
            )
        )

    def _self_iteration_task(self, command: AcceptanceCommand, snapshot_path: Path, prompt: str) -> HarnessTask:
        return HarnessTask(
            id="self-iteration-planner",
            title="Self-assess current state and generate the next roadmap stage",
            milestone_id="self-iteration",
            milestone_title="Autonomous Self Iteration",
            status="pending",
            max_attempts=1,
            file_scope=tuple(str(scope) for scope in (self.roadmap.get("self_iteration") or {}).get("file_scope", [".engineering/**", "docs/**"])),
            manual_approval_required=False,
            agent_approval_required=bool(self.executor_registry.metadata_for(command.executor).get("requires_agent_approval")),
            max_task_iterations=1,
            spec_refs=(),
            implementation=(),
            repair=(),
            acceptance=(
                AcceptanceCommand(
                    name="roadmap has new continuation stage",
                    command=f"python3 -c \"import json; from pathlib import Path; x=json.loads(Path('{self.roadmap_path}').read_text()); assert x.get('continuation', {{}}).get('stages')\"",
                    timeout_seconds=60,
                ),
            ),
            e2e=(),
        )

    def _self_iteration_prompt(self, config: dict[str, Any], snapshot_path: Path, context_path: Path) -> str:
        custom = str((config.get("planner") or {}).get("prompt", "")).strip()
        objective = str(config.get("objective", "Assess current project status and plan the next engineering stage."))
        max_stages = int(config.get("max_stages_per_iteration", 1))
        base = f"""
You are the self-iteration planner for an autonomous engineering harness.

Project root: {self.project_root}
Roadmap file: {self.roadmap_path}
Status snapshot: {snapshot_path}
Planner context pack: {context_path}
Objective: {objective}

Read the bounded JSON planner context pack first. It summarizes the roadmap, continuation state,
recent manifests and reports, blueprint/docs excerpts, test and source inventories, git status, and
recent commits so you can assess current state without ad hoc repository discovery. Use the roadmap
file and status snapshot only when you need to verify or write the final roadmap diff. Append exactly
{max_stages} new unmaterialized stage(s) to `continuation.stages` in the roadmap file.

Rules:
- Do not edit `.engineering/state` or `.engineering/reports`.
- Do not mark tasks done and do not add generated stages to `milestones`.
- Existing roadmap fields, milestones, tasks, statuses, and continuation stages must remain unchanged.
- New stages must be concrete, measurable, and automatable.
- Each new task must include non-empty `file_scope` and local acceptance commands.
- If code must be written, use an `implementation` entry with `"executor": "codex"` and a focused prompt.
- Include a `repair` entry with `"executor": "codex"` for every task that has implementation work.
- Read `spec_traceability` in the context pack. When it is required, add relevant requirement refs to
  new stages, tasks, acceptance commands, and E2E commands so validation and the assessment can
  explain the requirements advanced.
- Do not require live operations, private keys, mainnet writes, production deployments, paid services, or external accounts.
- Prefer the next step that moves the project toward the stated blueprint and vision.
- Keep scope tight enough that a coding agent can complete the stage in one iteration.
Planner output is accepted only if validation can prove that exactly {max_stages} safe, unmaterialized
continuation stage(s) were appended.
"""
        return base if not custom else f"{base}\n\nProject-specific planning guidance:\n{custom}\n"

    def _write_self_iteration_report(
        self,
        report_path: Path,
        reason: str,
        snapshot_path: Path,
        before: dict[str, Any],
        after: dict[str, Any],
        run: CommandRun,
        validation: dict[str, Any] | None = None,
        context_pack: dict[str, Any] | None = None,
        failure_isolation: dict[str, Any] | None = None,
        status: str | None = None,
        message: str | None = None,
        checkpoint_gate: dict[str, Any] | None = None,
        checkpoint_readiness: dict[str, Any] | None = None,
        goal_gap_scorecard: dict[str, Any] | None = None,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Self Iteration Report",
            "",
            f"- Reason: `{reason}`",
            f"- Status: `{status or run.status}`",
            f"- Message: {message or run.stderr or run.stdout or run.status}",
            f"- Snapshot: `{snapshot_path.relative_to(self.project_root)}`",
        ]
        if context_pack is not None:
            lines.append(f"- Context pack: `{context_pack.get('path')}`")
        lines.extend(
            [
                f"- Before stages: `{before.get('stage_count')}` pending `{before.get('pending_stage_count')}`",
                f"- After stages: `{after.get('stage_count')}` pending `{after.get('pending_stage_count')}`",
                "",
            ]
        )
        if context_pack is not None:
            lines.extend(
                [
                    "## Planner Context Pack",
                    "",
                    "```json",
                    json.dumps(context_pack.get("summary", {}), indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        scorecard_payload = (
            deepcopy(goal_gap_scorecard)
            if isinstance(goal_gap_scorecard, dict)
            else deepcopy(context_pack.get("goal_gap_scorecard"))
            if isinstance(context_pack, dict) and isinstance(context_pack.get("goal_gap_scorecard"), dict)
            else None
        )
        if isinstance(scorecard_payload, dict):
            lines.extend(
                [
                    "## Goal-Gap Scorecard",
                    "",
                    "```json",
                    json.dumps(
                        {
                            "summary": scorecard_payload.get("summary", {}),
                            "category_order": scorecard_payload.get("category_order", []),
                            "categories": scorecard_payload.get("categories", []),
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                    "",
                ]
            )
        readiness_payload = checkpoint_readiness
        if not isinstance(readiness_payload, dict) and isinstance(checkpoint_gate, dict):
            readiness_payload = checkpoint_gate.get("checkpoint_readiness")
        if isinstance(readiness_payload, dict):
            compact_readiness = self._compact_checkpoint_readiness(readiness_payload)
            lines.extend(
                [
                    "## Checkpoint Readiness",
                    "",
                    f"- Gate phase: `{(checkpoint_gate or {}).get('phase', 'unknown')}`",
                    f"- Gate status: `{(checkpoint_gate or {}).get('status', 'unknown')}`",
                    f"- Ready: `{str(bool(compact_readiness.get('ready'))).lower()}`",
                    f"- Blocking: `{str(bool(compact_readiness.get('blocking'))).lower()}`",
                    f"- Reason: `{compact_readiness.get('reason')}`",
                    f"- Dirty paths: `{len(compact_readiness.get('dirty_paths', []))}`",
                    f"- Blocking paths: `{len(compact_readiness.get('blocking_paths', []))}`",
                    f"- Recommended operator action: {compact_readiness.get('recommended_action')}",
                    "",
                ]
            )
            if compact_readiness.get("dirty_paths"):
                lines.extend(["Dirty paths:", ""])
                lines.extend(f"- `{path}`" for path in compact_readiness.get("dirty_paths", []))
                lines.append("")
            if compact_readiness.get("blocking_paths"):
                lines.extend(["Blocking paths:", ""])
                lines.extend(f"- `{path}`" for path in compact_readiness.get("blocking_paths", []))
                lines.append("")
            lines.extend(
                [
                    "Machine-readable checkpoint readiness:",
                    "",
                    "```json",
                    json.dumps(compact_readiness, indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
                "## Planner Run",
                "",
                f"- Name: {run.name}",
                f"- Status: `{run.status}`",
                f"- Return code: `{run.returncode}`",
                "",
                "```bash",
                run.command,
                "```",
                "",
            ]
        )
        if validation is not None:
            lines.extend(
                [
                    "## Output Validation",
                    "",
                    f"- Status: `{validation.get('status')}`",
                    f"- Expected new stages: `{validation.get('expected_new_stage_count')}`",
                    f"- Actual new stages: `{validation.get('actual_new_stage_count')}`",
                    f"- New stage ids: `{', '.join(validation.get('new_stage_ids') or [])}`",
                    f"- Errors: `{validation.get('error_count', 0)}`",
                    f"- Warnings: `{validation.get('warning_count', 0)}`",
                    "",
                ]
            )
            if validation.get("errors"):
                lines.extend(["Errors:", ""])
                lines.extend(f"- {error}" for error in validation.get("errors", []))
                lines.append("")
            if validation.get("warnings"):
                lines.extend(["Warnings:", ""])
                lines.extend(f"- {warning}" for warning in validation.get("warnings", []))
                lines.append("")
            requirement_refs = validation.get("new_stage_requirement_refs")
            if isinstance(requirement_refs, list) and requirement_refs:
                lines.extend(["## Requirement Advancement", ""])
                for item in requirement_refs:
                    if not isinstance(item, dict):
                        continue
                    stage_id = str(item.get("stage_id") or "unknown")
                    refs = item.get("spec_refs") if isinstance(item.get("spec_refs"), list) else []
                    ref_text = ", ".join(f"`{ref}`" for ref in refs) if refs else "`none declared`"
                    lines.append(f"- `{stage_id}` advances: {ref_text}")
                    tasks = item.get("tasks") if isinstance(item.get("tasks"), list) else []
                    for task in tasks:
                        if not isinstance(task, dict):
                            continue
                        task_id = str(task.get("task_id") or "unknown")
                        task_refs = task.get("spec_refs") if isinstance(task.get("spec_refs"), list) else []
                        task_ref_text = (
                            ", ".join(f"`{ref}`" for ref in task_refs) if task_refs else "`none declared`"
                        )
                        lines.append(f"  - Task `{task_id}`: {task_ref_text}")
                lines.append("")
        if run.stdout:
            lines.extend(["Stdout:", "", "```text", run.stdout, "```", ""])
        if run.stderr:
            lines.extend(["Stderr:", "", "```text", run.stderr, "```", ""])
        if failure_isolation is not None:
            lines.extend(
                [
                    "## Failure Isolation",
                    "",
                    f"- Phase: `{failure_isolation.get('phase')}`",
                    f"- Failure kind: `{failure_isolation.get('failure_kind')}`",
                    f"- Local next action: {failure_isolation.get('local_next_action')}",
                    "",
                    "```json",
                    json.dumps(failure_isolation, indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        report_path.write_text("\n".join(lines), encoding="utf-8")

    def _self_iteration_failure_isolation(
        self,
        run: CommandRun,
        *,
        status: str,
        message: str,
        report_path: Path,
        snapshot_path: Path,
        context_path: Path,
    ) -> dict[str, Any] | None:
        if run.status not in EXECUTOR_WATCHDOG_FAILURE_STATUSES:
            return None
        report_relative = str(report_path.relative_to(self.project_root))
        snapshot_relative = str(snapshot_path.relative_to(self.project_root))
        context_relative = str(context_path.relative_to(self.project_root))
        executor_watchdog = self._failure_isolation_executor_watchdog([run], phase=run.phase) or {}
        failure_kind = "executor_no_progress" if run.status == "no_progress" else "executor_timeout"
        return {
            "schema_version": FAILURE_ISOLATION_SCHEMA_VERSION,
            "kind": "engineering-harness.planner-failure-isolation",
            "status": status,
            "phase": run.phase,
            "failure_kind": failure_kind,
            "message": message,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "report_paths": {
                "self_iteration_report": report_relative,
                "self_iteration_snapshot": snapshot_relative,
                "self_iteration_context": context_relative,
            },
            "relevant_report_paths": [report_relative, snapshot_relative, context_relative],
            "executor_watchdog": executor_watchdog,
            "local_next_action": (
                f"Inspect the self-iteration planner watchdog evidence in {report_relative}, "
                "fix the local planner command or its no-progress threshold, then rerun self-iteration."
            ),
            "resolved": False,
        }

    def _continuation_stage_payload(self, stage: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "id": str(stage.get("id", "")),
            "title": str(stage.get("title", stage.get("id", ""))),
            "objective": str(stage.get("objective", "")),
            "task_count": len(stage.get("tasks", []) if isinstance(stage.get("tasks", []), list) else []),
        }
        refs = self._collect_stage_spec_refs(stage)
        if refs:
            payload["spec_refs"] = refs
        return payload

    def _materialize_continuation_stage(
        self,
        stage: dict[str, Any],
        *,
        existing_tasks: set[str],
    ) -> dict[str, Any]:
        stage_id = str(stage.get("id", ""))
        if not stage_id:
            raise ValueError("continuation stage is missing id")
        tasks = stage.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"continuation stage {stage_id} must define at least one task")
        materialized_tasks = []
        for task in tasks:
            task_id = str(task.get("id", ""))
            if not task_id:
                raise ValueError(f"continuation stage {stage_id} has a task without id")
            if task_id in existing_tasks:
                raise ValueError(f"continuation task id already exists: {task_id}")
            acceptance = task.get("acceptance") or []
            if not isinstance(acceptance, list) or not acceptance:
                raise ValueError(f"continuation task {task_id} must define acceptance commands")
            materialized_task = {
                "id": task_id,
                "title": str(task.get("title", task_id)),
                "status": str(task.get("status", "pending")),
                "max_attempts": int(task.get("max_attempts", 2)),
                "max_task_iterations": int(task.get("max_task_iterations", 1)),
                "manual_approval_required": bool(task.get("manual_approval_required", False)),
                "agent_approval_required": bool(task.get("agent_approval_required", False)),
                "file_scope": list(task.get("file_scope", [])),
                "implementation": task.get("implementation", []),
                "repair": task.get("repair", []),
                "acceptance": acceptance,
                "e2e": task.get("e2e", []),
                "generated_by": "engineering-harness-continuation",
                "generated_at": utc_now(),
            }
            self._copy_traceability_fields(task, materialized_task, fields=("spec_refs", "source_spec_task"))
            materialized_tasks.append(materialized_task)
        milestone = {
            "id": stage_id,
            "title": str(stage.get("title", stage_id)),
            "status": str(stage.get("status", "planned")),
            "objective": str(stage.get("objective", "")),
            "generated_by": "engineering-harness-continuation",
            "generated_at": utc_now(),
            "tasks": materialized_tasks,
        }
        self._copy_traceability_fields(stage, milestone, fields=("spec_refs", "source"))
        return milestone

    def _copy_traceability_fields(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
        *,
        fields: tuple[str, ...],
    ) -> None:
        for field_name in fields:
            if field_name in source:
                target[field_name] = deepcopy(source[field_name])

    def iter_tasks(self) -> list[HarnessTask]:
        tasks: list[HarnessTask] = []
        for milestone in self.roadmap.get("milestones", []):
            if str(milestone.get("status", "planned")) in BLOCKED_STATUSES:
                continue
            for task in milestone.get("tasks", []):
                implementation = self._parse_task_commands(task.get("implementation", []), default_name="implementation")
                repair = self._parse_task_commands(task.get("repair", []), default_name="repair")
                acceptance = self._parse_task_commands(task.get("acceptance", []), default_name="acceptance")
                e2e = self._parse_task_commands(task.get("e2e", []), default_name="e2e")
                tasks.append(
                    HarnessTask(
                        id=str(task["id"]),
                        title=str(task.get("title", task["id"])),
                        milestone_id=str(milestone["id"]),
                        milestone_title=str(milestone.get("title", milestone["id"])),
                        status=str(task.get("status", "pending")),
                        max_attempts=int(task.get("max_attempts", 3)),
                        file_scope=tuple(str(scope) for scope in task.get("file_scope", [])),
                        manual_approval_required=bool(task.get("manual_approval_required", False)),
                        agent_approval_required=bool(task.get("agent_approval_required", bool(implementation or repair))),
                        max_task_iterations=max(1, int(task.get("max_task_iterations", 1))),
                        spec_refs=self._normalize_spec_refs(task.get("spec_refs")),
                        implementation=tuple(implementation),
                        repair=tuple(repair),
                        acceptance=tuple(acceptance),
                        e2e=tuple(e2e),
                    )
                )
        return tasks

    def _parse_task_commands(self, items: list[dict[str, Any]] | None, *, default_name: str) -> list[AcceptanceCommand]:
        commands = []
        for item in items or []:
            executor = str(item.get("executor", "shell"))
            command = item.get("command")
            prompt = item.get("prompt")
            name = item.get("name") or command or prompt or default_name
            no_progress_timeout = self._coerce_optional_nonnegative_seconds(
                item.get("no_progress_timeout_seconds", item.get("no_progress_seconds"))
            )
            capability_field = self._requested_capability_field(item)
            requested_capabilities = (
                self._normalize_requested_capabilities(item.get(capability_field))
                if capability_field is not None
                else ()
            )
            spec_refs = self._normalize_spec_refs(item.get("spec_refs"))
            user_experience_gate = item.get("user_experience_gate")
            if not isinstance(user_experience_gate, dict):
                user_experience_gate = item.get("browser_user_experience")
            if not isinstance(user_experience_gate, dict):
                user_experience_gate = {}
            commands.append(
                AcceptanceCommand(
                    name=str(name),
                    command=str(command) if command is not None else None,
                    timeout_seconds=int(item.get("timeout_seconds", self.default_timeout)),
                    required=bool(item.get("required", True)),
                    executor=executor,
                    prompt=str(prompt) if prompt is not None else None,
                    model=str(item["model"]) if item.get("model") else None,
                    sandbox=str(item.get("sandbox", "workspace-write")),
                    no_progress_timeout_seconds=no_progress_timeout,
                    requested_capabilities=requested_capabilities,
                    user_experience_gate=deepcopy(user_experience_gate),
                    spec_refs=spec_refs,
                )
            )
        return commands

    def task_by_id(self, task_id: str) -> HarnessTask:
        for task in self.iter_tasks():
            if task.id == task_id:
                return task
        raise KeyError(f"Unknown task: {task_id}")

    def next_task(self) -> HarnessTask | None:
        state = self.load_state()
        state_tasks = state.get("tasks", {})
        for task in self.iter_tasks():
            task_state = state_tasks.get(task.id, {})
            status = str(task_state.get("status", task.status))
            attempts = int(task_state.get("attempts", 0))
            if status in COMPLETED_STATUSES or status in BLOCKED_STATUSES:
                continue
            if attempts >= task.max_attempts:
                continue
            return task
        return None

    def browser_user_experience_summary(self) -> dict[str, Any]:
        experience = self.frontend_experience_plan()
        experience_kind = str(experience.get("kind") or "")
        browser_required = is_browser_experience_kind(experience_kind)
        journeys = [item for item in experience.get("e2e_journeys", []) if isinstance(item, dict)]
        command_gates: list[dict[str, Any]] = []
        for task in self.iter_tasks():
            for command in task.e2e:
                if not self._command_is_user_experience_gate(command):
                    continue
                gate = deepcopy(command.user_experience_gate)
                command_gates.append(
                    {
                        "task_id": task.id,
                        "milestone_id": task.milestone_id,
                        "name": command.name,
                        "command": self._display_command(command, task),
                        "required": command.required,
                        "gate": gate,
                        "journey_id": str((gate.get("journey") or {}).get("id") or ""),
                    }
                )

        latest_runs_by_journey: dict[str, dict[str, Any]] = {}
        latest_failures: list[dict[str, Any]] = []
        index = self._build_manifest_index()
        manifests = index.get("manifests", []) if isinstance(index.get("manifests"), list) else []
        for manifest in reversed(manifests):
            runs = manifest.get("runs", []) if isinstance(manifest.get("runs"), list) else []
            for run in reversed(runs):
                if not isinstance(run, dict) or str(run.get("phase")) != "e2e":
                    continue
                gate = run.get("user_experience_gate") if isinstance(run.get("user_experience_gate"), dict) else {}
                if str(gate.get("kind") or "") != BROWSER_USER_EXPERIENCE_GATE_KIND:
                    continue
                journey = gate.get("journey") if isinstance(gate.get("journey"), dict) else {}
                journey_id = str(journey.get("id") or "")
                if not journey_id:
                    continue
                run_payload = {
                    "journey_id": journey_id,
                    "task_id": manifest.get("task_id"),
                    "manifest_path": manifest.get("manifest_path"),
                    "report_path": manifest.get("report_path"),
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "returncode": run.get("returncode"),
                    "finished_at": manifest.get("finished_at"),
                }
                latest_runs_by_journey.setdefault(journey_id, run_payload)
                if str(run.get("status")) != "passed":
                    latest_failures.append(run_payload)

        journey_summaries: list[dict[str, Any]] = []
        for journey in journeys:
            journey_id = str(journey.get("id") or "primary-browser-journey")
            gates_for_journey = [gate for gate in command_gates if gate.get("journey_id") == journey_id]
            gate_payload = (
                deepcopy((gates_for_journey[0].get("gate") or {}))
                if gates_for_journey
                else browser_user_experience_gate(self.project_root, experience=experience, journey=journey)
                if browser_required
                else {}
            )
            declaration_paths = (
                gate_payload.get("route_form_role_declarations")
                if isinstance(gate_payload.get("route_form_role_declarations"), list)
                else []
            )
            evidence_paths = gate_payload.get("evidence_paths") if isinstance(gate_payload.get("evidence_paths"), dict) else {}
            latest_run = latest_runs_by_journey.get(journey_id)
            journey_summaries.append(
                {
                    "id": journey_id,
                    "persona": str(journey.get("persona") or ""),
                    "goal": str(journey.get("goal") or ""),
                    "gate_configured": bool(gates_for_journey),
                    "command_count": len(gates_for_journey),
                    "commands": [
                        {
                            "task_id": item.get("task_id"),
                            "milestone_id": item.get("milestone_id"),
                            "name": item.get("name"),
                            "command": item.get("command"),
                            "required": item.get("required"),
                        }
                        for item in gates_for_journey
                    ],
                    "declaration_paths": self._browser_path_statuses(declaration_paths),
                    "declaration_summary": self._browser_declaration_summary(declaration_paths),
                    "evidence_paths": self._browser_evidence_statuses(evidence_paths),
                    "latest_run": latest_run,
                    "latest_status": latest_run.get("status") if isinstance(latest_run, dict) else "not_run",
                }
            )

        if not browser_required:
            status = "not_applicable"
        elif latest_failures:
            status = "failed"
        elif journey_summaries and all(item.get("latest_status") == "passed" for item in journey_summaries):
            status = "passed"
        elif command_gates:
            status = "configured"
        else:
            status = "planned"

        return {
            "schema_version": BROWSER_USER_EXPERIENCE_SCHEMA_VERSION,
            "kind": "engineering-harness.browser-user-experience-summary",
            "status": status,
            "browser_required": browser_required,
            "experience_kind": experience_kind,
            "journey_count": len(journey_summaries),
            "configured_gate_count": len(command_gates),
            "playwright": detect_playwright_support(self.project_root),
            "fallback": {
                "kind": "static-html-smoke",
                "requires_external_services": False,
                "evidence_dir": BROWSER_E2E_EVIDENCE_DIR,
            },
            "journeys": journey_summaries,
            "latest_failures": latest_failures[:FAILURE_ISOLATION_SUMMARY_LIMIT],
        }

    def _browser_path_statuses(self, paths: list[Any]) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for path in paths:
            text = str(path)
            if not text.strip():
                continue
            candidate = self.project_root / text
            statuses.append({"path": text, "exists": candidate.exists()})
        return statuses

    def _browser_evidence_statuses(self, paths: dict[str, Any]) -> dict[str, Any]:
        return {
            key: {"path": str(value), "exists": (self.project_root / str(value)).exists()}
            for key, value in sorted(paths.items())
            if str(value).strip()
        }

    def _browser_declaration_summary(self, paths: list[Any]) -> dict[str, Any]:
        for path in paths:
            candidate = self.project_root / str(path)
            if not candidate.exists():
                continue
            try:
                payload = load_mapping(candidate)
            except Exception as exc:
                return {"path": str(path), "status": "invalid", "error": self._truncate_text(str(exc), 200)}
            routes = payload.get("routes") if isinstance(payload, dict) and isinstance(payload.get("routes"), list) else []
            form_count = 0
            role_count = 0
            for route in routes:
                if not isinstance(route, dict):
                    continue
                forms = route.get("expect_forms", route.get("forms", []))
                if isinstance(forms, dict):
                    form_count += 1
                elif isinstance(forms, list):
                    form_count += sum(1 for item in forms if isinstance(item, dict))
                roles = route.get("expect_roles", route.get("roles", []))
                if isinstance(roles, list):
                    role_count += len([item for item in roles if str(item).strip()])
            return {
                "path": str(path),
                "status": "loaded",
                "route_count": len([route for route in routes if isinstance(route, dict)]),
                "form_count": form_count,
                "role_count": role_count,
            }
        return {"status": "missing", "route_count": 0, "form_count": 0, "role_count": 0}

    def runtime_dashboard_summary(self, status_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        summary = status_summary or self.status_summary()
        if status_summary is None and isinstance(summary.get("runtime_dashboard"), dict):
            return deepcopy(summary["runtime_dashboard"])
        drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
        approval_queue = summary.get("approval_queue") if isinstance(summary.get("approval_queue"), dict) else {}
        failure_isolation = (
            summary.get("failure_isolation") if isinstance(summary.get("failure_isolation"), dict) else {}
        )
        replay_guard = (
            summary.get("replay_guard") if isinstance(summary.get("replay_guard"), dict) else {}
        )
        capability_policy = (
            summary.get("capability_policy") if isinstance(summary.get("capability_policy"), dict) else {}
        )
        executor_diagnostics = (
            summary.get("executor_diagnostics")
            if isinstance(summary.get("executor_diagnostics"), dict)
            else self.executor_diagnostics_summary()
        )
        checkpoint_readiness = (
            summary.get("checkpoint_readiness") if isinstance(summary.get("checkpoint_readiness"), dict) else {}
        )
        latest_reports = self._runtime_enriched_report_context()
        workspace_dispatch = self._runtime_workspace_dispatch_summary()
        daemon_supervisor = (
            summary.get("daemon_supervisor_runtime")
            if isinstance(summary.get("daemon_supervisor_runtime"), dict)
            else self._runtime_daemon_supervisor_summary()
        )
        latest_reports["workspace_dispatch_reports"] = deepcopy(workspace_dispatch.get("latest_reports", {}))
        latest_reports["daemon_supervisor_reports"] = deepcopy(daemon_supervisor.get("latest_reports", {}))
        current_task = self._runtime_current_task(summary, drive_control)
        current_phase = (current_task or {}).get("phase")
        if current_phase is None and bool(drive_control.get("active", False)):
            current_phase = drive_control.get("current_activity")
        goal_gap_scorecard = (
            deepcopy(summary.get("goal_gap_scorecard"))
            if isinstance(summary.get("goal_gap_scorecard"), dict)
            else self.goal_gap_scorecard(status_summary=summary, latest_reports=latest_reports)
        )
        frontend_experience = (
            deepcopy(summary.get("experience"))
            if isinstance(summary.get("experience"), dict)
            else self.frontend_experience_plan()
        )
        browser_user_experience = (
            deepcopy(summary.get("browser_user_experience"))
            if isinstance(summary.get("browser_user_experience"), dict)
            else self.browser_user_experience_summary()
        )
        spec_coverage = (
            deepcopy(summary.get("spec"))
            if isinstance(summary.get("spec"), dict)
            else self.spec_coverage_summary()
        )
        return {
            "schema_version": RUNTIME_DASHBOARD_SCHEMA_VERSION,
            "kind": "engineering-harness.runtime-dashboard",
            "generated_at": utc_now(),
            "project": summary.get("project"),
            "root": summary.get("root"),
            "status_source": "engh status --json",
            "spec": spec_coverage,
            "frontend_experience": frontend_experience,
            "domain_frontend": self._runtime_domain_frontend_payload(frontend_experience),
            "browser_user_experience": browser_user_experience,
            "drive_control": self._runtime_drive_control_payload(drive_control),
            "drive_watchdog": deepcopy(drive_control.get("watchdog") if isinstance(drive_control.get("watchdog"), dict) else {}),
            "current_task": current_task,
            "current_phase": current_phase,
            "executor_no_progress": self._runtime_executor_no_progress_payload(
                summary,
                drive_control,
                failure_isolation,
            ),
            "approval_leases": self._runtime_approval_leases_payload(approval_queue),
            "capability_policy": deepcopy(capability_policy),
            "executor_diagnostics": deepcopy(executor_diagnostics),
            "failure_isolation": deepcopy(failure_isolation),
            "replay_guard": deepcopy(replay_guard),
            "checkpoint_readiness": deepcopy(checkpoint_readiness),
            "self_iteration": deepcopy(summary.get("self_iteration", {})),
            "workspace_dispatch": workspace_dispatch,
            "daemon_supervisor_runtime": daemon_supervisor,
            "latest_reports": latest_reports,
            "goal_gap_scorecard": goal_gap_scorecard,
            "goal_gap": self._runtime_goal_gap_payload(summary, latest_reports),
        }

    def operator_console_summary(self, status_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        if status_summary is None:
            summary = self.status_summary()
            if isinstance(summary.get("operator_console"), dict):
                return deepcopy(summary["operator_console"])
        else:
            summary = status_summary
        state = self.load_state()
        manifest_index = self.manifest_index()
        latest_reports = self._runtime_enriched_report_context()
        drive_history = self._operator_console_drive_history()
        task_history = self._operator_console_task_run_history(manifest_index)
        all_approvals = self._approval_queue_summary_from_state(state, status_filter=None)
        scorecard = (
            deepcopy(summary.get("goal_gap_scorecard"))
            if isinstance(summary.get("goal_gap_scorecard"), dict)
            else self.goal_gap_scorecard(status_summary=summary, latest_reports=latest_reports)
        )
        artifact_paths = self.operator_console_artifact_paths()
        artifact_available = (
            (self.project_root / artifact_paths["json_path"]).exists()
            and (self.project_root / artifact_paths["markdown_path"]).exists()
        )
        payload = {
            "schema_version": OPERATOR_CONSOLE_SCHEMA_VERSION,
            "kind": "engineering-harness.operator-console",
            "project": summary.get("project"),
            "root": summary.get("root"),
            "roadmap": summary.get("roadmap"),
            "status_source": "engh status --json",
            "snapshot_at": self._operator_console_snapshot_at(
                summary=summary,
                state=state,
                manifest_index=manifest_index,
                drive_history=drive_history,
            ),
            "local_only": True,
            "requires_external_services": False,
            "artifact": {
                "status": "available" if artifact_available else "not_written",
                "json_path": artifact_paths["json_path"],
                "markdown_path": artifact_paths["markdown_path"],
            },
            "queue_state": self._operator_console_queue_state(summary, state),
            "run_history": {
                "task_runs": task_history,
                "drive_runs": drive_history,
            },
            "task_timelines": self._operator_console_task_timelines(state),
            "approvals": self._operator_console_approvals(all_approvals),
            "failures": self._operator_console_failures(summary, manifest_index),
            "checkpoint_readiness": self._operator_console_checkpoint_readiness(summary),
            "goal_gap_scorecard": self._operator_console_goal_gap_scorecard(scorecard),
            "replay_guard": self._operator_console_replay_guard(summary),
            "e2e_artifacts": self._operator_console_e2e_artifacts(summary, manifest_index),
            "latest_reports": {
                "task_reports": deepcopy(latest_reports.get("task_reports", {})),
                "drive_reports": deepcopy(latest_reports.get("drive_reports", {})),
            },
            "recommended_actions": [],
            "limits": deepcopy(OPERATOR_CONSOLE_LIMITS),
        }
        payload["recommended_actions"] = self._operator_console_recommended_actions(payload, summary)
        return self._operator_console_finalize(payload)

    def operator_console_artifact_paths(self) -> dict[str, str]:
        report_dir = self.report_dir.parent / "operator-console"
        json_path = report_dir / "operator-console.json"
        markdown_path = report_dir / "operator-console.md"
        return {
            "json_path": self._project_relative_path(json_path),
            "markdown_path": self._project_relative_path(markdown_path),
        }

    def write_operator_console_artifact(
        self,
        status_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self.operator_console_summary(status_summary=status_summary)
        paths = self.operator_console_artifact_paths()
        payload["artifact"] = {
            "status": "written",
            "json_path": paths["json_path"],
            "markdown_path": paths["markdown_path"],
        }
        payload = self._operator_console_finalize(payload)
        json_path = self.project_root / paths["json_path"]
        markdown_path = self.project_root / paths["markdown_path"]
        write_json(json_path, payload)
        self._write_operator_console_markdown(markdown_path, payload)
        return payload

    def _operator_console_finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._redact_context_value(payload)
        payload["bounds"] = {
            "max_json_bytes": OPERATOR_CONSOLE_LIMITS["max_json_bytes"],
            "estimated_json_bytes": 0,
            "within_limit": True,
        }
        estimated = len(json.dumps(payload, sort_keys=True).encode("utf-8"))
        payload["bounds"]["estimated_json_bytes"] = estimated
        payload["bounds"]["within_limit"] = estimated <= OPERATOR_CONSOLE_LIMITS["max_json_bytes"]
        return payload

    def _write_operator_console_markdown(self, markdown_path: Path, payload: dict[str, Any]) -> None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        queue = payload.get("queue_state") if isinstance(payload.get("queue_state"), dict) else {}
        task_runs = payload.get("run_history", {}).get("task_runs", {}) if isinstance(payload.get("run_history"), dict) else {}
        drive_runs = payload.get("run_history", {}).get("drive_runs", {}) if isinstance(payload.get("run_history"), dict) else {}
        approvals = payload.get("approvals") if isinstance(payload.get("approvals"), dict) else {}
        failures = payload.get("failures") if isinstance(payload.get("failures"), dict) else {}
        checkpoint = payload.get("checkpoint_readiness") if isinstance(payload.get("checkpoint_readiness"), dict) else {}
        scorecard = payload.get("goal_gap_scorecard") if isinstance(payload.get("goal_gap_scorecard"), dict) else {}
        lines = [
            "# Operator Console",
            "",
            f"- Project: `{payload.get('project')}`",
            f"- Snapshot: `{payload.get('snapshot_at')}`",
            f"- Pending tasks: `{queue.get('pending_count', 0)}`",
            f"- Task runs: `{task_runs.get('total_count', 0)}`",
            f"- Drive runs: `{drive_runs.get('total_count', 0)}`",
            f"- Pending approvals: `{approvals.get('pending_count', 0)}`",
            f"- Unresolved failures: `{failures.get('unresolved_count', 0)}`",
            f"- Checkpoint readiness: `{checkpoint.get('reason')}` blocking=`{str(bool(checkpoint.get('blocking'))).lower()}`",
            f"- Goal-gap status: `{scorecard.get('overall_status')}`",
            "",
            "## Recommended Actions",
            "",
        ]
        actions = payload.get("recommended_actions") if isinstance(payload.get("recommended_actions"), list) else []
        if not actions:
            lines.append("- No operator action is currently recommended.")
        for action in actions:
            if not isinstance(action, dict):
                continue
            lines.append(f"- `{action.get('id')}` `{action.get('severity')}` - {action.get('title')}")
        lines.extend(
            [
                "",
                "## Machine Payload",
                "",
                "```json",
                json.dumps(payload, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
        markdown_path.write_text("\n".join(lines), encoding="utf-8")

    def _operator_console_snapshot_at(
        self,
        *,
        summary: dict[str, Any],
        state: dict[str, Any],
        manifest_index: dict[str, Any],
        drive_history: dict[str, Any],
    ) -> str | None:
        timestamps: list[str] = []
        for value in (
            state.get("updated_at"),
            manifest_index.get("updated_at"),
        ):
            if value:
                timestamps.append(str(value))
        drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
        for key in ("updated_at", "finished_at", "last_heartbeat_at", "started_at"):
            if drive_control.get(key):
                timestamps.append(str(drive_control[key]))
        for run in drive_history.get("recent", []):
            if isinstance(run, dict):
                for key in ("finished_at", "started_at"):
                    if run.get(key):
                        timestamps.append(str(run[key]))
        return max(timestamps) if timestamps else None

    def _operator_console_status_counts_for_tasks(self, state: dict[str, Any]) -> dict[str, int]:
        state_tasks = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        counts: dict[str, int] = {}
        for task in self.iter_tasks():
            task_state = state_tasks.get(task.id, {}) if isinstance(state_tasks.get(task.id), dict) else {}
            status = str(task_state.get("status", task.status))
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def _operator_console_task_brief(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(task, dict):
            return None
        return {
            "id": task.get("id"),
            "title": self._truncate_text(str(task.get("title") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
            "milestone_id": task.get("milestone_id"),
            "milestone_title": task.get("milestone_title"),
            "status": task.get("status"),
            "manual_approval_required": bool(task.get("manual_approval_required", False)),
            "agent_approval_required": bool(task.get("agent_approval_required", False)),
            "spec_refs": deepcopy(task.get("spec_refs", [])) if isinstance(task.get("spec_refs"), list) else [],
        }

    def _operator_console_queue_state(self, summary: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        state_tasks = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        pending_tasks: list[dict[str, Any]] = []
        blocked_tasks: list[dict[str, Any]] = []
        for task in self.iter_tasks():
            task_state = state_tasks.get(task.id, {}) if isinstance(state_tasks.get(task.id), dict) else {}
            status = str(task_state.get("status", task.status))
            item = {
                "id": task.id,
                "title": self._truncate_text(task.title, OPERATOR_CONSOLE_LIMITS["message_chars"]),
                "milestone_id": task.milestone_id,
                "status": status,
                "manual_approval_required": task.manual_approval_required,
                "agent_approval_required": task.agent_approval_required,
            }
            if status in BLOCKED_STATUSES or status == "failed":
                blocked_tasks.append(item)
            elif status not in COMPLETED_STATUSES:
                pending_tasks.append(item)
        continuation = summary.get("continuation") if isinstance(summary.get("continuation"), dict) else {}
        return {
            "status_counts": self._operator_console_status_counts_for_tasks(state),
            "milestones": deepcopy(summary.get("milestones", [])),
            "next_task": self._operator_console_task_brief(
                summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
            ),
            "pending_count": len(pending_tasks),
            "blocked_count": len(blocked_tasks),
            "pending_tasks": pending_tasks[: OPERATOR_CONSOLE_LIMITS["pending_tasks"]],
            "pending_tasks_truncated": len(pending_tasks) > OPERATOR_CONSOLE_LIMITS["pending_tasks"],
            "blocked_tasks": blocked_tasks[: OPERATOR_CONSOLE_LIMITS["pending_tasks"]],
            "blocked_tasks_truncated": len(blocked_tasks) > OPERATOR_CONSOLE_LIMITS["pending_tasks"],
            "continuation": {
                key: deepcopy(continuation.get(key))
                for key in (
                    "enabled",
                    "pending_stage_count",
                    "materialized_stage_count",
                    "total_stage_count",
                    "next_stage",
                )
                if key in continuation
            },
        }

    def _operator_console_task_run_history(self, manifest_index: dict[str, Any]) -> dict[str, Any]:
        manifests = [
            item for item in manifest_index.get("manifests", []) if isinstance(item, dict)
        ]
        trend = [
            self._operator_console_task_run_item(item)
            for item in manifests[-OPERATOR_CONSOLE_LIMITS["recent_task_runs"] :]
        ]
        recent = list(reversed(trend))
        return {
            "total_count": len(manifests),
            "included_count": len(recent),
            "status_counts": deepcopy(manifest_index.get("status_counts", {})),
            "latest_manifest": manifest_index.get("latest_manifest"),
            "latest_by_task": deepcopy(manifest_index.get("latest_by_task", {})),
            "trend": trend,
            "recent": recent,
            "truncated": len(manifests) > OPERATOR_CONSOLE_LIMITS["recent_task_runs"],
        }

    def _operator_console_task_run_item(self, item: dict[str, Any]) -> dict[str, Any]:
        runs = item.get("runs") if isinstance(item.get("runs"), list) else []
        return {
            "task_id": item.get("task_id"),
            "task_title": self._truncate_text(str(item.get("task_title") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
            "milestone_id": item.get("milestone_id"),
            "status": item.get("status"),
            "message": self._truncate_text(str(item.get("message") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
            "attempt": item.get("attempt"),
            "manifest_path": item.get("manifest_path"),
            "report_path": item.get("report_path"),
            "run_count": item.get("run_count"),
            "phases": [
                {
                    "phase": run.get("phase"),
                    "name": run.get("name"),
                    "executor": run.get("executor"),
                    "status": run.get("status"),
                    "returncode": run.get("returncode"),
                }
                for run in runs[: OPERATOR_CONSOLE_LIMITS["timeline_events_per_task"]]
                if isinstance(run, dict)
            ],
        }

    def _operator_console_drive_history(self) -> dict[str, Any]:
        report_dir = self.report_dir / "drives"
        paths = sorted([path for path in report_dir.glob("*.json") if path.is_file()], key=self._project_relative_path)
        runs: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        for path in paths:
            try:
                payload = load_mapping(path)
            except Exception as exc:
                item = {
                    "report_json": self._project_relative_path(path),
                    "status": "unreadable",
                    "message": self._truncate_text(str(exc), OPERATOR_CONSOLE_LIMITS["message_chars"]),
                }
            else:
                item = self._operator_console_drive_run_item(path, payload)
            status = str(item.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            runs.append(item)
        trend = runs[-OPERATOR_CONSOLE_LIMITS["recent_drive_runs"] :]
        return {
            "total_count": len(runs),
            "included_count": len(trend),
            "status_counts": dict(sorted(status_counts.items())),
            "trend": trend,
            "recent": list(reversed(trend)),
            "truncated": len(runs) > OPERATOR_CONSOLE_LIMITS["recent_drive_runs"],
        }

    def _operator_console_drive_run_item(self, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        retrospective = (
            payload.get("goal_gap_retrospective")
            if isinstance(payload.get("goal_gap_retrospective"), dict)
            else {}
        )
        trigger = retrospective.get("trigger") if isinstance(retrospective.get("trigger"), dict) else {}
        replay_guard = payload.get("replay_guard") if isinstance(payload.get("replay_guard"), dict) else {}
        checkpoint = payload.get("checkpoint_readiness") if isinstance(payload.get("checkpoint_readiness"), dict) else {}
        return {
            "report_json": self._project_relative_path(path),
            "report": payload.get("drive_report"),
            "status": payload.get("status"),
            "message": self._truncate_text(str(payload.get("message") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "result_count": len(payload.get("results", [])) if isinstance(payload.get("results"), list) else 0,
            "continuation_count": len(payload.get("continuations", [])) if isinstance(payload.get("continuations"), list) else 0,
            "self_iteration_count": len(payload.get("self_iterations", [])) if isinstance(payload.get("self_iterations"), list) else 0,
            "checkpoint_reason": checkpoint.get("reason"),
            "checkpoint_blocking": bool(checkpoint.get("blocking", False)),
            "replay_guard_status": replay_guard.get("status"),
            "reused_phase_count": replay_guard.get("reused_phase_count"),
            "goal_gap_stop_class": trigger.get("stop_class"),
            "request_self_iteration": (
                retrospective.get("request_self_iteration", {}).get("recommended")
                if isinstance(retrospective.get("request_self_iteration"), dict)
                else None
            ),
        }

    def _operator_console_task_timelines(self, state: dict[str, Any]) -> dict[str, Any]:
        state_tasks = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        timelines: list[dict[str, Any]] = []
        for task_id, task_state in sorted(state_tasks.items()):
            if not isinstance(task_state, dict):
                continue
            history = [item for item in task_state.get("phase_history", []) if isinstance(item, dict)]
            history.sort(key=lambda item: (int(item.get("sequence", 0) or 0), str(item.get("recorded_at") or "")))
            if not history and not task_state.get("status"):
                continue
            events = history[-OPERATOR_CONSOLE_LIMITS["timeline_events_per_task"] :]
            latest = history[-1] if history else {}
            timelines.append(
                {
                    "task_id": str(task_id),
                    "status": task_state.get("status"),
                    "attempts": int(task_state.get("attempts", 0) or 0),
                    "last_report": task_state.get("last_report"),
                    "last_manifest": task_state.get("last_manifest"),
                    "latest_event": self._operator_console_phase_event(latest) if latest else None,
                    "event_count": len(history),
                    "events": [self._operator_console_phase_event(item) for item in events],
                    "events_truncated": len(history) > OPERATOR_CONSOLE_LIMITS["timeline_events_per_task"],
                }
            )
        timelines.sort(
            key=lambda item: (
                str((item.get("latest_event") or {}).get("recorded_at") or ""),
                str(item.get("task_id") or ""),
            ),
            reverse=True,
        )
        return {
            "included_count": min(len(timelines), OPERATOR_CONSOLE_LIMITS["timeline_tasks"]),
            "total_count": len(timelines),
            "timelines": timelines[: OPERATOR_CONSOLE_LIMITS["timeline_tasks"]],
            "truncated": len(timelines) > OPERATOR_CONSOLE_LIMITS["timeline_tasks"],
        }

    def _operator_console_phase_event(self, event: dict[str, Any]) -> dict[str, Any]:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        return {
            "sequence": event.get("sequence"),
            "recorded_at": event.get("recorded_at"),
            "phase": event.get("phase"),
            "event": event.get("event"),
            "status": event.get("status"),
            "message": self._truncate_text(str(event.get("message") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
            "run_count": len(event.get("runs", [])) if isinstance(event.get("runs"), list) else metadata.get("run_count"),
            "report_path": metadata.get("report_path"),
            "manifest_path": metadata.get("manifest_path"),
        }

    def _operator_console_approvals(self, approval_queue: dict[str, Any]) -> dict[str, Any]:
        items = [item for item in approval_queue.get("items", []) if isinstance(item, dict)]
        return {
            "schema_version": approval_queue.get("schema_version"),
            "path": approval_queue.get("path"),
            "lease_ttl_seconds": approval_queue.get("lease_ttl_seconds"),
            "counts": deepcopy(approval_queue.get("counts", {})),
            "pending_count": int(approval_queue.get("pending_count", 0) or 0),
            "approved_count": int(approval_queue.get("approved_count", 0) or 0),
            "consumed_count": int(approval_queue.get("consumed_count", 0) or 0),
            "stale_count": int(approval_queue.get("stale_count", 0) or 0),
            "stale_reasons": deepcopy(approval_queue.get("stale_reasons", {})),
            "items": [
                {
                    "id": item.get("id"),
                    "task_id": item.get("task_id"),
                    "milestone_id": item.get("milestone_id"),
                    "approval_kind": item.get("approval_kind"),
                    "decision_kind": item.get("decision_kind"),
                    "phase": item.get("phase"),
                    "name": item.get("name"),
                    "executor": item.get("executor"),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                    "lease_expires_at": item.get("lease_expires_at"),
                    "reason": self._truncate_text(str(item.get("reason") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
                    "stale_reason": self._truncate_text(str(item.get("stale_reason") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
                }
                for item in items[: OPERATOR_CONSOLE_LIMITS["approval_items"]]
            ],
            "truncated": len(items) > OPERATOR_CONSOLE_LIMITS["approval_items"],
        }

    def _operator_console_failures(
        self,
        summary: dict[str, Any],
        manifest_index: dict[str, Any],
    ) -> dict[str, Any]:
        failure_isolation = (
            summary.get("failure_isolation") if isinstance(summary.get("failure_isolation"), dict) else {}
        )
        manifests = [item for item in manifest_index.get("manifests", []) if isinstance(item, dict)]
        failed = [
            self._operator_console_task_run_item(item)
            for item in manifests
            if str(item.get("status")) in {"failed", "blocked", "timeout", "no_progress"}
        ]
        failed = failed[-OPERATOR_CONSOLE_LIMITS["failure_items"] :]
        latest_isolated = [
            deepcopy(item)
            for item in failure_isolation.get("latest_isolated_failures", [])
            if isinstance(item, dict)
        ]
        return {
            "schema_version": failure_isolation.get("schema_version", FAILURE_ISOLATION_SCHEMA_VERSION),
            "unresolved_count": int(failure_isolation.get("unresolved_count", 0) or 0),
            "has_unresolved": bool(failure_isolation.get("has_unresolved", False)),
            "latest_isolated_failures": latest_isolated[: OPERATOR_CONSOLE_LIMITS["failure_items"]],
            "recent_failed_task_runs": list(reversed(failed)),
            "recent_failed_task_runs_count": len(failed),
        }

    def _operator_console_checkpoint_readiness(self, summary: dict[str, Any]) -> dict[str, Any]:
        readiness = summary.get("checkpoint_readiness") if isinstance(summary.get("checkpoint_readiness"), dict) else {}
        return {
            "schema_version": readiness.get("schema_version", CHECKPOINT_READINESS_SCHEMA_VERSION),
            "kind": readiness.get("kind", "engineering-harness.checkpoint-readiness"),
            "is_repository": bool(readiness.get("is_repository", False)),
            "ready": bool(readiness.get("ready", False)),
            "blocking": bool(readiness.get("blocking", False)),
            "reason": readiness.get("reason"),
            "dirty_count": len(readiness.get("dirty_paths", [])) if isinstance(readiness.get("dirty_paths"), list) else 0,
            "blocking_paths": deepcopy(readiness.get("blocking_paths", [])),
            "safe_to_checkpoint_paths": deepcopy(readiness.get("safe_to_checkpoint_paths", [])),
            "recommended_action": readiness.get("recommended_action"),
            "task": self._operator_console_task_brief(
                readiness.get("task") if isinstance(readiness.get("task"), dict) else None
            ),
        }

    def _operator_console_goal_gap_scorecard(self, scorecard: dict[str, Any]) -> dict[str, Any]:
        summary = scorecard.get("summary") if isinstance(scorecard.get("summary"), dict) else {}
        categories = [
            {
                "id": category.get("id"),
                "title": category.get("title"),
                "status": category.get("status"),
                "risk_score": category.get("risk_score"),
                "severity": category.get("severity"),
                "rationale": self._truncate_text(str(category.get("rationale") or ""), OPERATOR_CONSOLE_LIMITS["message_chars"]),
                "recommended_next_stage_themes": deepcopy(category.get("recommended_next_stage_themes", [])),
                "evidence_paths": deepcopy(category.get("evidence_paths", [])),
            }
            for category in scorecard.get("categories", [])
            if isinstance(category, dict)
        ]
        return {
            "schema_version": scorecard.get("schema_version", GOAL_GAP_SCORECARD_SCHEMA_VERSION),
            "kind": scorecard.get("kind", "engineering-harness.goal-gap-scorecard"),
            "overall_status": summary.get("overall_status"),
            "status_counts": deepcopy(summary.get("status_counts", {})),
            "max_risk_score": summary.get("max_risk_score"),
            "highest_severity": summary.get("highest_severity"),
            "category_order": deepcopy(scorecard.get("category_order", [])),
            "categories": categories,
            "recommended_next_stage_themes": deepcopy(scorecard.get("recommended_next_stage_themes", [])),
        }

    def _operator_console_replay_guard(self, summary: dict[str, Any]) -> dict[str, Any]:
        replay_guard = summary.get("replay_guard") if isinstance(summary.get("replay_guard"), dict) else {}
        reused = [
            deepcopy(item)
            for item in replay_guard.get("reused_phases", [])
            if isinstance(item, dict)
        ]
        return {
            "schema_version": replay_guard.get("schema_version", REPLAY_GUARD_SCHEMA_VERSION),
            "kind": replay_guard.get("kind", "engineering-harness.replay-guard-summary"),
            "status": replay_guard.get("status", "none"),
            "reused_phase_count": int(replay_guard.get("reused_phase_count", 0) or 0),
            "reused_phases": reused[: OPERATOR_CONSOLE_LIMITS["replay_guard_items"]],
            "truncated": len(reused) > OPERATOR_CONSOLE_LIMITS["replay_guard_items"],
        }

    def _operator_console_e2e_artifacts(
        self,
        summary: dict[str, Any],
        manifest_index: dict[str, Any],
    ) -> dict[str, Any]:
        browser = (
            summary.get("browser_user_experience")
            if isinstance(summary.get("browser_user_experience"), dict)
            else {}
        )
        runs: list[dict[str, Any]] = []
        for manifest in manifest_index.get("manifests", []):
            if not isinstance(manifest, dict):
                continue
            for run in manifest.get("runs", []):
                if not isinstance(run, dict):
                    continue
                phase = str(run.get("phase") or "")
                if phase != "e2e" and not run.get("user_experience_gate"):
                    continue
                runs.append(
                    {
                        "task_id": manifest.get("task_id"),
                        "manifest_path": manifest.get("manifest_path"),
                        "phase": phase,
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "returncode": run.get("returncode"),
                        "executor": run.get("executor"),
                        "user_experience_gate": deepcopy(run.get("user_experience_gate", {})),
                    }
                )
        evidence_dir = self.project_root / BROWSER_E2E_EVIDENCE_DIR
        files = []
        if evidence_dir.exists():
            for path in sorted([item for item in evidence_dir.rglob("*") if item.is_file()], key=self._project_relative_path):
                files.append(
                    {
                        "path": self._project_relative_path(path),
                        "bytes": self._file_size(path),
                    }
                )
        journeys = browser.get("journeys") if isinstance(browser.get("journeys"), list) else []
        return {
            "status": browser.get("status", "unknown"),
            "browser_required": bool(browser.get("browser_required", False)),
            "journey_count": int(browser.get("journey_count", 0) or 0),
            "configured_gate_count": int(browser.get("configured_gate_count", 0) or 0),
            "journeys": [
                {
                    "id": journey.get("id"),
                    "latest_status": journey.get("latest_status"),
                    "gate_configured": bool(journey.get("gate_configured", False)),
                    "evidence_paths": deepcopy(journey.get("evidence_paths", {})),
                }
                for journey in journeys[: OPERATOR_CONSOLE_LIMITS["e2e_runs"]]
                if isinstance(journey, dict)
            ],
            "runs": list(reversed(runs[-OPERATOR_CONSOLE_LIMITS["e2e_runs"] :])),
            "runs_truncated": len(runs) > OPERATOR_CONSOLE_LIMITS["e2e_runs"],
            "files": list(reversed(files[-OPERATOR_CONSOLE_LIMITS["e2e_files"] :])),
            "files_truncated": len(files) > OPERATOR_CONSOLE_LIMITS["e2e_files"],
            "evidence_dir": BROWSER_E2E_EVIDENCE_DIR,
        }

    def _operator_console_recommended_actions(
        self,
        payload: dict[str, Any],
        summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []

        def add(action_id: str, title: str, *, severity: str, source: str, details: dict[str, Any] | None = None) -> None:
            if any(action.get("id") == action_id for action in actions):
                return
            actions.append(
                {
                    "id": action_id,
                    "title": title,
                    "severity": severity,
                    "source": source,
                    "details": details or {},
                }
            )

        failures = payload.get("failures") if isinstance(payload.get("failures"), dict) else {}
        approvals = payload.get("approvals") if isinstance(payload.get("approvals"), dict) else {}
        checkpoint = payload.get("checkpoint_readiness") if isinstance(payload.get("checkpoint_readiness"), dict) else {}
        queue = payload.get("queue_state") if isinstance(payload.get("queue_state"), dict) else {}
        drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
        scorecard = payload.get("goal_gap_scorecard") if isinstance(payload.get("goal_gap_scorecard"), dict) else {}
        if int(failures.get("unresolved_count", 0) or 0) > 0:
            add(
                "recover-isolated-failure",
                "Resolve unresolved isolated task failures before extending unattended work.",
                severity="error",
                source="failure_isolation",
                details={"unresolved_count": failures.get("unresolved_count")},
            )
        if int(approvals.get("pending_count", 0) or 0) > 0:
            add(
                "review-approval-leases",
                "Review pending local approval leases and approve or adjust the blocked task.",
                severity="approval",
                source="approval_queue",
                details={"pending_count": approvals.get("pending_count")},
            )
        if int(approvals.get("stale_count", 0) or 0) > 0:
            add(
                "refresh-stale-approvals",
                "Regenerate or clear stale approval leases before rerunning the blocked task.",
                severity="warning",
                source="approval_queue",
                details={"stale_count": approvals.get("stale_count")},
            )
        if bool(checkpoint.get("blocking", False)):
            add(
                "clear-checkpoint-blockers",
                str(checkpoint.get("recommended_action") or "Clear checkpoint blockers before dispatch."),
                severity="error",
                source="checkpoint_readiness",
                details={"blocking_paths": checkpoint.get("blocking_paths", [])},
            )
        if drive_control.get("status") == "stale" or bool(drive_control.get("stale", False)):
            add(
                "recover-stale-running-drive",
                "Resume or recover stale drive state before starting another drive.",
                severity="error",
                source="drive_control",
                details={"stale_reason": drive_control.get("stale_reason")},
            )
        categories = [item for item in scorecard.get("categories", []) if isinstance(item, dict)]
        for category in sorted(
            categories,
            key=lambda item: (int(item.get("risk_score", 0) or 0), str(item.get("id") or "")),
            reverse=True,
        ):
            if str(category.get("status")) not in {"blocked", "missing", "partial"}:
                continue
            add(
                f"goal-gap-{category.get('id')}",
                f"Address goal-gap category `{category.get('id')}`: {category.get('rationale')}",
                severity="warning" if category.get("status") != "blocked" else "error",
                source="goal_gap_scorecard",
                details={"status": category.get("status"), "risk_score": category.get("risk_score")},
            )
            if len(actions) >= OPERATOR_CONSOLE_LIMITS["recommended_actions"]:
                break
        next_task = queue.get("next_task") if isinstance(queue.get("next_task"), dict) else None
        if next_task and len(actions) < OPERATOR_CONSOLE_LIMITS["recommended_actions"]:
            add(
                "run-next-roadmap-task",
                f"Run or dispatch next roadmap task `{next_task.get('id')}`.",
                severity="info",
                source="queue_state",
                details={"task_id": next_task.get("id")},
            )
        if not actions:
            add(
                "monitor-next-drive",
                "Run the next local drive and inspect the generated operator console.",
                severity="info",
                source="operator_console",
            )
        return actions[: OPERATOR_CONSOLE_LIMITS["recommended_actions"]]

    def _runtime_domain_frontend_payload(self, experience: dict[str, Any]) -> dict[str, Any]:
        decision = (
            deepcopy(experience.get("decision_contract"))
            if isinstance(experience.get("decision_contract"), dict)
            else {}
        )
        personas = self._string_items(experience.get("personas"))
        surfaces = self._string_items(experience.get("primary_surfaces"))
        journeys = [item for item in experience.get("e2e_journeys", []) if isinstance(item, dict)]
        return {
            "schema_version": DOMAIN_FRONTEND_PLAN_SCHEMA_VERSION,
            "kind": "engineering-harness.domain-frontend-runtime-evidence.v1",
            "status": "required" if bool(experience.get("frontend_required", True)) else "optional",
            "generated_by": DOMAIN_FRONTEND_GENERATOR_ID,
            "experience_kind": experience.get("kind"),
            "domain": experience.get("domain") or decision.get("domain"),
            "source": experience.get("source"),
            "derived": bool(experience.get("derived", False)),
            "required": bool(experience.get("required", True)),
            "frontend_required": bool(experience.get("frontend_required", True)),
            "surface_policy": experience.get("surface_policy") or decision.get("surface_policy"),
            "persona_count": len(personas),
            "surface_count": len(surfaces),
            "journey_count": len(journeys),
            "decision_contract": decision,
        }

    def _runtime_drive_control_payload(self, drive_control: dict[str, Any]) -> dict[str, Any]:
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
        return {
            "schema_version": drive_control.get("schema_version"),
            "status": drive_control.get("status", "idle"),
            "active": bool(drive_control.get("active", False)),
            "pause_requested": bool(drive_control.get("pause_requested", False)),
            "cancel_requested": bool(drive_control.get("cancel_requested", False)),
            "stale": bool(drive_control.get("stale", False)),
            "stale_reason": drive_control.get("stale_reason"),
            "pid": drive_control.get("pid"),
            "started_at": drive_control.get("started_at"),
            "last_heartbeat_at": drive_control.get("last_heartbeat_at"),
            "heartbeat_count": int(drive_control.get("heartbeat_count", 0) or 0),
            "current_activity": drive_control.get("current_activity"),
            "last_progress_message": drive_control.get("last_progress_message"),
            "latest_executor_event": deepcopy(drive_control.get("latest_executor_event"))
            if isinstance(drive_control.get("latest_executor_event"), dict)
            else None,
            "executor_event_count": int(drive_control.get("executor_event_count", 0) or 0),
            "stale_running_recovery": stale_running_recovery,
            "stale_running_preflight": stale_running_preflight,
            "stale_running_block": stale_running_block,
        }

    def _runtime_current_task(
        self,
        summary: dict[str, Any],
        drive_control: dict[str, Any],
    ) -> dict[str, Any] | None:
        current = drive_control.get("current_task") if isinstance(drive_control.get("current_task"), dict) else None
        if current:
            return {
                "source": "drive_control",
                "active": bool(drive_control.get("active", False)),
                "id": current.get("id"),
                "title": current.get("title"),
                "milestone_id": current.get("milestone_id"),
                "milestone_title": current.get("milestone_title"),
                "phase": current.get("phase") or drive_control.get("current_activity"),
            }
        next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
        if next_task:
            return {
                "source": "roadmap_next_task",
                "active": False,
                "id": next_task.get("id"),
                "title": next_task.get("title"),
                "milestone_id": next_task.get("milestone_id"),
                "milestone_title": next_task.get("milestone_title"),
                "phase": None,
            }
        return None

    def _runtime_approval_leases_payload(self, approval_queue: dict[str, Any]) -> dict[str, Any]:
        counts = approval_queue.get("counts") if isinstance(approval_queue.get("counts"), dict) else {}
        items = approval_queue.get("items") if isinstance(approval_queue.get("items"), list) else []
        pending_items = [
            {
                "id": item.get("id"),
                "task_id": item.get("task_id"),
                "approval_kind": item.get("approval_kind"),
                "decision_kind": item.get("decision_kind"),
                "phase": item.get("phase"),
                "name": item.get("name"),
                "executor": item.get("executor"),
                "status": item.get("status"),
                "lease_expires_at": item.get("lease_expires_at"),
                "reason": item.get("reason"),
            }
            for item in items[:25]
            if isinstance(item, dict)
        ]
        pending_count = int(approval_queue.get("pending_count", counts.get("pending", 0)) or 0)
        approved_count = int(approval_queue.get("approved_count", counts.get("approved", 0)) or 0)
        consumed_count = int(approval_queue.get("consumed_count", counts.get("consumed", 0)) or 0)
        stale_count = int(approval_queue.get("stale_count", counts.get("stale", 0)) or 0)
        return {
            "schema_version": approval_queue.get("schema_version", APPROVAL_QUEUE_SCHEMA_VERSION),
            "path": approval_queue.get("path"),
            "lease_ttl_seconds": approval_queue.get("lease_ttl_seconds"),
            "counts": deepcopy(counts),
            "pending_count": pending_count,
            "approved_count": approved_count,
            "consumed_count": consumed_count,
            "stale_count": stale_count,
            "open_count": pending_count + approved_count,
            "stale_reasons": deepcopy(approval_queue.get("stale_reasons", {})),
            "pending_items": pending_items,
        }

    def _runtime_executor_no_progress_payload(
        self,
        summary: dict[str, Any],
        drive_control: dict[str, Any],
        failure_isolation: dict[str, Any],
    ) -> dict[str, Any]:
        configured = summary.get("executor_watchdog") if isinstance(summary.get("executor_watchdog"), dict) else {}
        current = (
            deepcopy(drive_control.get("executor_watchdog"))
            if isinstance(drive_control.get("executor_watchdog"), dict)
            else None
        )
        latest_failure = None
        latest_no_progress = None
        failures = failure_isolation.get("latest_isolated_failures")
        if isinstance(failures, list):
            for item in failures:
                if not isinstance(item, dict):
                    continue
                watchdog = item.get("executor_watchdog") if isinstance(item.get("executor_watchdog"), dict) else {}
                if latest_failure is None and watchdog:
                    latest_failure = deepcopy(item)
                if item.get("failure_kind") == "executor_no_progress" or watchdog.get("status") == "no_progress":
                    latest_no_progress = deepcopy(item)
                    break
        current_status = current.get("status") if isinstance(current, dict) else None
        return {
            "schema_version": EXECUTOR_WATCHDOG_CONTRACT_VERSION,
            "enabled": bool(configured.get("enabled", False)),
            "default_no_progress_seconds": configured.get("default_no_progress_seconds"),
            "phase_no_progress_seconds": deepcopy(configured.get("phase_no_progress_seconds", {})),
            "current": current,
            "current_status": current_status,
            "current_no_progress": current_status == "no_progress",
            "latest_failure": latest_failure,
            "latest_no_progress_failure": latest_no_progress,
            "has_unresolved_no_progress": latest_no_progress is not None,
        }

    def _runtime_goal_gap_payload(
        self,
        summary: dict[str, Any],
        latest_reports: dict[str, Any],
    ) -> dict[str, Any]:
        drive_reports = latest_reports.get("drive_reports") if isinstance(latest_reports.get("drive_reports"), dict) else {}
        files = drive_reports.get("files") if isinstance(drive_reports.get("files"), list) else []
        for item in files:
            if not isinstance(item, dict) or not item.get("json_path"):
                continue
            path = self.project_root / str(item["json_path"])
            try:
                payload = load_mapping(path)
            except Exception:
                continue
            retrospective = payload.get("goal_gap_retrospective") if isinstance(payload, dict) else None
            if isinstance(retrospective, dict):
                return self._runtime_goal_gap_from_retrospective(
                    retrospective,
                    source_report=str(item.get("path")),
                    source_report_json=str(item.get("json_path")),
                )
        return {
            "source": "current_status",
            "source_report": None,
            "source_report_json": None,
            "next_actions": self._runtime_goal_gap_fallback_actions(summary),
            "request_self_iteration": {"recommended": False, "reason": "no latest drive retrospective is available"},
            "remaining_risks": [],
        }

    def _runtime_goal_gap_from_retrospective(
        self,
        retrospective: dict[str, Any],
        *,
        source_report: str,
        source_report_json: str,
    ) -> dict[str, Any]:
        request = retrospective.get("request_self_iteration")
        request_payload = deepcopy(request) if isinstance(request, dict) else {}
        risks = [
            deepcopy(item)
            for item in retrospective.get("remaining_risks", [])
            if isinstance(item, dict)
        ]
        actions = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "source": "latest_drive_goal_gap",
                "source_risks": deepcopy(item.get("source_risks", [])),
            }
            for item in retrospective.get("likely_next_stage_themes", [])
            if isinstance(item, dict)
        ]
        if not actions and request_payload.get("recommended"):
            actions.append(
                {
                    "id": "request-self-iteration",
                    "title": "Request a self-iteration stage from the latest local evidence",
                    "source": "latest_drive_goal_gap",
                    "source_risks": ["roadmap_queue_empty"],
                }
            )
        if not actions:
            actions.append(
                {
                    "id": "monitor-next-drive",
                    "title": "Run the next drive and compare the resulting local reports",
                    "source": "latest_drive_goal_gap",
                    "source_risks": [],
                }
            )
        trigger = retrospective.get("trigger") if isinstance(retrospective.get("trigger"), dict) else {}
        return {
            "source": "latest_drive_report",
            "source_report": source_report,
            "source_report_json": source_report_json,
            "goal": retrospective.get("goal"),
            "stop_class": trigger.get("stop_class"),
            "request_self_iteration": request_payload,
            "remaining_risks": risks,
            "next_actions": actions,
        }

    def _runtime_goal_gap_fallback_actions(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        failure_isolation = summary.get("failure_isolation") if isinstance(summary.get("failure_isolation"), dict) else {}
        approval_queue = summary.get("approval_queue") if isinstance(summary.get("approval_queue"), dict) else {}
        next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
        if int(failure_isolation.get("unresolved_count", 0) or 0) > 0:
            return [
                {
                    "id": "recover-isolated-failure",
                    "title": "Resolve unresolved isolated task failures before extending the roadmap",
                    "source": "current_status",
                    "source_risks": ["unresolved_isolated_failures"],
                }
            ]
        if int(approval_queue.get("pending_count", 0) or 0) > 0:
            return [
                {
                    "id": "review-approval-leases",
                    "title": "Review pending approval gates before the next unattended drive",
                    "source": "current_status",
                    "source_risks": ["pending_approvals"],
                }
            ]
        if next_task:
            return [
                {
                    "id": "run-next-task",
                    "title": f"Run or dispatch the next roadmap task `{next_task.get('id')}`",
                    "source": "current_status",
                    "source_risks": ["pending_work"],
                }
            ]
        return [
            {
                "id": "monitor-next-drive",
                "title": "Run the next drive and inspect the generated local reports",
                "source": "current_status",
                "source_risks": [],
            }
        ]

    def _checkpoint_readiness_task(
        self,
        next_task: HarnessTask | None,
        drive_control: dict[str, Any],
    ) -> HarnessTask | None:
        current = drive_control.get("current_task") if isinstance(drive_control.get("current_task"), dict) else {}
        task_id = str(current.get("id") or "")
        if task_id:
            try:
                return self.task_by_id(task_id)
            except KeyError:
                pass
        return next_task

    def goal_gap_scorecard(
        self,
        *,
        status_summary: dict[str, Any] | None = None,
        latest_reports: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary = deepcopy(status_summary) if isinstance(status_summary, dict) else self.status_summary()
        evidence = self._goal_gap_evidence(summary)
        task_counts = self._goal_gap_task_counts(summary)
        duplicate_plan = self._self_iteration_duplicate_plan_summary()
        manifest_context = self._self_iteration_manifest_context()
        source_inventory = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
        latest_retrospective = self._goal_gap_scorecard_latest_retrospective_summary(latest_reports=latest_reports)
        workspace_dispatch = self._runtime_workspace_dispatch_summary()
        categories = self._goal_gap_scorecard_categories(
            summary=summary,
            evidence=evidence,
            task_counts=task_counts,
            duplicate_plan=duplicate_plan,
            manifest_context=manifest_context,
            source_inventory=source_inventory,
            latest_retrospective=latest_retrospective,
            workspace_dispatch=workspace_dispatch,
        )
        status_counts = {status: 0 for status in GOAL_GAP_SCORECARD_STATUS_ORDER}
        for category in categories:
            status_counts[str(category.get("status", "missing"))] = (
                status_counts.get(str(category.get("status", "missing")), 0) + 1
            )
        max_risk = max((int(category.get("risk_score", 0) or 0) for category in categories), default=0)
        max_severity = max((int(category.get("severity", 0) or 0) for category in categories), default=0)
        if status_counts.get("blocked", 0):
            overall_status = "blocked"
        elif status_counts.get("missing", 0) or status_counts.get("partial", 0):
            overall_status = "partial"
        else:
            overall_status = "complete"
        self_iteration = summary.get("self_iteration") if isinstance(summary.get("self_iteration"), dict) else {}
        objective = str(self_iteration.get("objective") or UNATTENDED_RELIABILITY_GOAL)
        payload = {
            "schema_version": GOAL_GAP_SCORECARD_SCHEMA_VERSION,
            "kind": "engineering-harness.goal-gap-scorecard",
            "generated_at": utc_now(),
            "goal": UNATTENDED_RELIABILITY_GOAL,
            "objective": objective,
            "objective_source": "self_iteration.objective" if self_iteration.get("objective") else "default",
            "category_order": list(GOAL_GAP_SCORECARD_CATEGORY_ORDER),
            "summary": {
                "category_count": len(categories),
                "overall_status": overall_status,
                "status_counts": status_counts,
                "max_risk_score": max_risk,
                "highest_severity": max_severity,
            },
            "categories": categories,
            "latest_drive_goal_gap_retrospective": latest_retrospective,
            "recommended_next_stage_themes": self._goal_gap_scorecard_theme_summary(categories),
            "limits": {
                "category_count": len(GOAL_GAP_SCORECARD_CATEGORY_ORDER),
                "evidence_paths_per_category": SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_evidence_paths"],
                "themes_per_category": SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_themes"],
                "recent_manifest_count": SELF_ITERATION_CONTEXT_LIMITS["recent_manifest_count"],
            },
            "evidence_sources": [
                "latest_drive_goal_gap_retrospective",
                "manifest_index",
                "capability_policy",
                "failure_isolation",
                "approval_queue",
                "drive_control",
                "checkpoint_readiness",
                "workspace_dispatch",
                "test_inventory",
                "source_inventory",
                "git",
            ],
        }
        return self._redact_context_value(payload)

    def _goal_gap_scorecard_categories(
        self,
        *,
        summary: dict[str, Any],
        evidence: dict[str, Any],
        task_counts: dict[str, int],
        duplicate_plan: dict[str, Any],
        manifest_context: dict[str, Any],
        source_inventory: dict[str, Any],
        latest_retrospective: dict[str, Any],
        workspace_dispatch: dict[str, Any],
    ) -> list[dict[str, Any]]:
        drive_control = evidence.get("drive_control") if isinstance(evidence.get("drive_control"), dict) else {}
        watchdog = drive_control.get("watchdog") if isinstance(drive_control.get("watchdog"), dict) else {}
        executor_watchdog = summary.get("executor_watchdog") if isinstance(summary.get("executor_watchdog"), dict) else {}
        failure_isolation = (
            evidence.get("failure_isolation") if isinstance(evidence.get("failure_isolation"), dict) else {}
        )
        approval_queue = evidence.get("approval_queue") if isinstance(evidence.get("approval_queue"), dict) else {}
        capability_policy = (
            summary.get("capability_policy") if isinstance(summary.get("capability_policy"), dict) else {}
        )
        checkpoint_readiness = (
            evidence.get("checkpoint_readiness") if isinstance(evidence.get("checkpoint_readiness"), dict) else {}
        )
        self_iteration = summary.get("self_iteration") if isinstance(summary.get("self_iteration"), dict) else {}
        manifest_index = evidence.get("manifest_index") if isinstance(evidence.get("manifest_index"), dict) else {}
        tests = evidence.get("tests") if isinstance(evidence.get("tests"), dict) else {}
        git = evidence.get("git") if isinstance(evidence.get("git"), dict) else {}

        current_executor_watchdog = (
            drive_control.get("executor_watchdog")
            if isinstance(drive_control.get("executor_watchdog"), dict)
            else {}
        )
        unresolved_no_progress = any(
            isinstance(item, dict)
            and (
                item.get("failure_kind") == "executor_no_progress"
                or (
                    isinstance(item.get("executor_watchdog"), dict)
                    and item.get("executor_watchdog", {}).get("status") == "no_progress"
                )
            )
            for item in failure_isolation.get("latest_isolated_failures", [])
            if isinstance(item, dict)
        )
        if current_executor_watchdog.get("status") == "no_progress" or unresolved_no_progress:
            stuck_status, stuck_risk = "blocked", 88
            stuck_rationale = "Current no-progress watchdog evidence is unresolved."
        elif executor_watchdog.get("enabled") and watchdog.get("schema_version"):
            stuck_status, stuck_risk = "complete", 8
            stuck_rationale = "Drive stale checks and executor no-progress thresholds are visible."
        elif watchdog.get("schema_version") or executor_watchdog.get("schema_version"):
            stuck_status, stuck_risk = "partial", 42
            stuck_rationale = "Only part of the stuck-detection evidence is available or enabled."
        else:
            stuck_status, stuck_risk = "missing", 72
            stuck_rationale = "No local watchdog evidence is available."

        stale_preflight = (
            drive_control.get("stale_running_preflight")
            if isinstance(drive_control.get("stale_running_preflight"), dict)
            else {}
        )
        stale_recovery = (
            drive_control.get("stale_running_recovery")
            if isinstance(drive_control.get("stale_running_recovery"), dict)
            else {}
        )
        stale_block = (
            drive_control.get("stale_running_block")
            if isinstance(drive_control.get("stale_running_block"), dict)
            else {}
        )
        stale_preflight_status = str(stale_preflight.get("status") or "")
        stale_recommendations: list[str] = []
        if stale_preflight_status == "recoverable":
            stale_status, stale_risk = "blocked", 88
            stale_rationale = "Stale-running recovery is required for a stale heartbeat with a dead or missing owner pid."
            stale_recommendations = ["recover-stale-running-drive"]
        elif stale_preflight_status in {"in_progress", "protected"} or (
            stale_preflight_status == "blocked" and stale_preflight.get("reason") == "heartbeat_fresh"
        ):
            stale_status, stale_risk = "complete", 5
            stale_rationale = "in_progress: running drive has a fresh heartbeat, so stale recovery is not needed."
        elif stale_block.get("blocking"):
            stale_status, stale_risk = "partial", 42
            stale_rationale = str(
                stale_block.get("message") or "Active drive state is protected from stale-running recovery."
            )
        elif stale_preflight.get("schema_version") or stale_recovery.get("schema_version"):
            stale_status, stale_risk = "complete", 8
            stale_rationale = "Stale-running preflight or recovery evidence is recorded locally."
        else:
            stale_status, stale_risk = "missing", 68
            stale_rationale = "No stale-running recovery preflight is recorded."

        dirty_paths = checkpoint_readiness.get("dirty_paths") if isinstance(checkpoint_readiness.get("dirty_paths"), list) else []
        blocking_paths = (
            checkpoint_readiness.get("blocking_paths")
            if isinstance(checkpoint_readiness.get("blocking_paths"), list)
            else []
        )
        safe_paths = (
            checkpoint_readiness.get("safe_to_checkpoint_paths")
            if isinstance(checkpoint_readiness.get("safe_to_checkpoint_paths"), list)
            else []
        )
        drive_active = str(drive_control.get("status") or "") == "running" or bool(drive_control.get("active"))
        checkpoint_recommendations = ["close-git-boundary"] if blocking_paths else []
        if blocking_paths:
            checkpoint_status, checkpoint_risk = "blocked", 92
            checkpoint_rationale = str(
                checkpoint_readiness.get("recommended_action") or "Checkpoint readiness is blocked."
            )
        elif checkpoint_readiness.get("blocking"):
            checkpoint_status, checkpoint_risk = "blocked", 84
            checkpoint_rationale = str(
                checkpoint_readiness.get("recommended_action") or "Checkpoint readiness is blocked."
            )
        elif not checkpoint_readiness.get("is_repository"):
            checkpoint_status, checkpoint_risk = "missing", 65
            checkpoint_rationale = "No git repository is available for unattended checkpoint boundaries."
        elif safe_paths:
            checkpoint_status, checkpoint_risk = "partial", 24 if drive_active else 30
            checkpoint_state = "in_progress" if drive_active else "checkpoint_pending"
            checkpoint_rationale = (
                f"{checkpoint_state}: {len(safe_paths)} safe-to-checkpoint path(s) are dirty and no "
                f"blocking paths are present: {', '.join(str(path) for path in safe_paths[:4])}"
            )
        elif dirty_paths:
            checkpoint_status, checkpoint_risk = "partial", 36
            checkpoint_rationale = f"{len(dirty_paths)} local git path(s) are dirty but not blocking."
        elif checkpoint_readiness.get("ready"):
            checkpoint_status, checkpoint_risk = "complete", 4
            checkpoint_rationale = "Checkpoint readiness is clean."
        else:
            checkpoint_status, checkpoint_risk = "partial", 45
            checkpoint_rationale = "Checkpoint readiness is present but not cleanly ready."

        unresolved_failures = int(failure_isolation.get("unresolved_count", 0) or 0)
        if unresolved_failures > 0:
            failure_status, failure_risk = "blocked", 92
            failure_rationale = f"{unresolved_failures} unresolved isolated failure(s) need local recovery."
        elif failure_isolation.get("schema_version"):
            failure_status, failure_risk = "complete", 6
            failure_rationale = "Failure isolation summary is available with no unresolved failures."
        else:
            failure_status, failure_risk = "missing", 68
            failure_rationale = "Failure isolation summary is missing."

        duplicate_groups = int(duplicate_plan.get("duplicate_group_count", 0) or 0)
        if bool(self_iteration.get("enabled")) and duplicate_plan.get("algorithm"):
            duplicate_status = "partial" if duplicate_groups else "complete"
            duplicate_risk = 34 if duplicate_groups else 10
            duplicate_rationale = (
                f"{duplicate_groups} duplicate continuation plan group(s) are visible to the guard."
                if duplicate_groups
                else "Duplicate-plan fingerprints are available for self-iteration planning."
            )
        elif duplicate_plan.get("algorithm"):
            duplicate_status, duplicate_risk = "partial", 44
            duplicate_rationale = "Duplicate-plan fingerprints exist, but self-iteration is not enabled."
        else:
            duplicate_status, duplicate_risk = "missing", 70
            duplicate_rationale = "No duplicate-plan fingerprint evidence is available."

        retrospective_risks = latest_retrospective.get("remaining_risks")
        if not isinstance(retrospective_risks, list):
            retrospective_risks = []
        blocking_risk_ids = {
            "blocked_task",
            "blocked_roadmap_tasks",
            "checkpoint_blocking_paths",
            "checkpoint_not_ready",
            "failed_task",
            "failed_roadmap_tasks",
            "isolated_failure",
            "pending_approvals",
            "unresolved_isolated_failures",
        }
        has_blocking_retrospective_risk = any(
            isinstance(item, dict)
            and (
                str(item.get("id")) in blocking_risk_ids
                or str(item.get("severity", "")).lower() == "high"
            )
            for item in retrospective_risks
        )
        if not latest_retrospective.get("available"):
            retrospective_status, retrospective_risk = "missing", 64
            retrospective_rationale = "No latest drive goal-gap retrospective is available."
        elif has_blocking_retrospective_risk:
            retrospective_status, retrospective_risk = "blocked", 86
            retrospective_rationale = "Latest drive retrospective contains unresolved high-risk blockers."
        elif retrospective_risks:
            retrospective_status, retrospective_risk = "partial", 46
            retrospective_rationale = "Latest drive retrospective exists and still lists remaining risks."
        else:
            retrospective_status, retrospective_risk = "complete", 8
            retrospective_rationale = "Latest drive retrospective has no remaining risks."

        runtime_fields = (
            drive_control.get("schema_version"),
            approval_queue.get("schema_version"),
            failure_isolation.get("schema_version"),
            manifest_index.get("manifest_count") is not None,
        )
        if all(runtime_fields):
            runtime_status, runtime_risk = "complete", 6
            runtime_rationale = "Status, drive control, approval, failure, and manifest summaries are dashboard-ready."
        elif any(runtime_fields):
            runtime_status, runtime_risk = "partial", 38
            runtime_rationale = "Runtime dashboard evidence is partially populated."
        else:
            runtime_status, runtime_risk = "missing", 70
            runtime_rationale = "Runtime dashboard evidence is not available."

        pending_approvals = int(approval_queue.get("pending_count", 0) or 0)
        stale_approvals = int(approval_queue.get("stale_count", 0) or 0)
        capability_blocking = int(capability_policy.get("blocking_count", 0) or 0)
        capability_requires_approval = len(
            capability_policy.get("requires_approval", [])
            if isinstance(capability_policy.get("requires_approval"), list)
            else []
        )
        if pending_approvals > 0 or capability_blocking > 0:
            approval_status, approval_risk = "blocked", 94
            approval_rationale = (
                f"{pending_approvals} pending approval(s) and {capability_blocking} capability policy blocker(s)."
            )
        elif stale_approvals > 0 or capability_requires_approval > 0:
            approval_status, approval_risk = "partial", 48
            approval_rationale = (
                f"{stale_approvals} stale approval(s) and "
                f"{capability_requires_approval} capability decision(s) requiring approval."
            )
        elif approval_queue.get("schema_version") and capability_policy.get("schema_version"):
            approval_status, approval_risk = "complete", 6
            approval_rationale = "Approval queue and capability policy summaries have no open blockers."
        else:
            approval_status, approval_risk = "missing", 72
            approval_rationale = "Approval queue or capability policy summary is missing."

        workspace_status_value = str(workspace_dispatch.get("status") or "not_found")
        workspace_lease = workspace_dispatch.get("lease") if isinstance(workspace_dispatch.get("lease"), dict) else {}
        workspace_queue = workspace_dispatch.get("queue") if isinstance(workspace_dispatch.get("queue"), list) else []
        if workspace_lease.get("stale"):
            workspace_status, workspace_risk = "blocked", 78
            workspace_rationale = "Workspace dispatch has a stale lease requiring operator attention."
        elif workspace_status_value == "active_lease":
            workspace_status, workspace_risk = "partial", 32
            workspace_rationale = "Workspace dispatch has an active lease; fairness evidence is still being produced."
        elif workspace_status_value == "not_found":
            workspace_status, workspace_risk = "missing", 55
            workspace_rationale = "No workspace dispatch reports or lease evidence were found."
        elif workspace_queue:
            workspace_status, workspace_risk = "complete", 12
            workspace_rationale = f"Workspace dispatch queue evidence includes {len(workspace_queue)} project item(s)."
        else:
            workspace_status, workspace_risk = "partial", 36
            workspace_rationale = f"Workspace dispatch status is {workspace_status_value} without a queue."

        recent_manifests = (
            manifest_context.get("recent_task_manifests")
            if isinstance(manifest_context.get("recent_task_manifests"), list)
            else []
        )
        e2e_runs: list[dict[str, Any]] = []
        e2e_manifest_paths: list[str] = []
        for manifest in recent_manifests:
            if not isinstance(manifest, dict):
                continue
            manifest_path = str(manifest.get("manifest_path") or "")
            for run in manifest.get("runs", []):
                if not isinstance(run, dict) or str(run.get("phase")) != "e2e":
                    continue
                e2e_runs.append(run)
                if manifest_path:
                    e2e_manifest_paths.append(manifest_path)
        passed_e2e = any(str(run.get("status")) == "passed" for run in e2e_runs)
        roadmap_e2e_count = sum(1 for task in self.iter_tasks() if task.e2e)
        browser_ux = summary.get("browser_user_experience") if isinstance(summary.get("browser_user_experience"), dict) else {}
        browser_ux_status = str(browser_ux.get("status") or "")
        test_count = int(tests.get("total_count", 0) or 0)
        source_count = int(source_inventory.get("total_count", 0) or 0)
        if browser_ux_status == "failed":
            e2e_status, e2e_risk = "blocked", 84
            e2e_rationale = "A browser user-experience gate has a recent unresolved failure."
        elif browser_ux_status == "passed":
            e2e_status, e2e_risk = "complete", 6
            e2e_rationale = "Browser user-experience gates have passing local evidence."
        elif passed_e2e:
            e2e_status, e2e_risk = "complete", 8
            e2e_rationale = "Recent manifests include a passing E2E run."
        elif e2e_runs or roadmap_e2e_count > 0 or browser_ux_status == "configured":
            e2e_status, e2e_risk = "partial", 52
            e2e_rationale = "E2E gates are defined or have run, but no recent passing E2E evidence was found."
        elif test_count > 0 and source_count > 0:
            e2e_status, e2e_risk = "partial", 58
            e2e_rationale = "Local tests and source inventory exist, but no E2E journey evidence was found."
        else:
            e2e_status, e2e_risk = "missing", 78
            e2e_rationale = "No local E2E evidence, tests, or source inventory were found."

        category_by_id = {
            "stuck_detection": self._goal_gap_scorecard_category(
                "stuck_detection",
                "Stuck detection",
                stuck_status,
                stuck_risk,
                ["drive_control.watchdog", "executor_watchdog", "failure_isolation.executor_watchdog"],
                stuck_rationale,
                ["tighten-watchdog-thresholds"] if stuck_status != "complete" else [],
            ),
            "stale_running_recovery": self._goal_gap_scorecard_category(
                "stale_running_recovery",
                "Stale running recovery",
                stale_status,
                stale_risk,
                [
                    "drive_control.stale_running_preflight",
                    "drive_control.stale_running_recovery",
                    "drive_control.stale_running_block",
                ],
                stale_rationale,
                stale_recommendations,
            ),
            "checkpoint_boundaries": self._goal_gap_scorecard_category(
                "checkpoint_boundaries",
                "Checkpoint boundaries",
                checkpoint_status,
                checkpoint_risk,
                ["checkpoint_readiness", "git.status_lines"],
                checkpoint_rationale,
                checkpoint_recommendations,
            ),
            "failure_isolation": self._goal_gap_scorecard_category(
                "failure_isolation",
                "Failure isolation",
                failure_status,
                failure_risk,
                ["failure_isolation.latest_isolated_failures", "manifest_index.failure_isolation"],
                failure_rationale,
                ["recover-isolated-failure"] if failure_status == "blocked" else [],
            ),
            "duplicate_plan_guard": self._goal_gap_scorecard_category(
                "duplicate_plan_guard",
                "Duplicate-plan guard",
                duplicate_status,
                duplicate_risk,
                ["duplicate_plan", "roadmap.continuation.stages", "self_iteration.latest_assessment"],
                duplicate_rationale,
                ["refresh-duplicate-plan-context"] if duplicate_status != "complete" else [],
            ),
            "goal_gap_retrospective": self._goal_gap_scorecard_category(
                "goal_gap_retrospective",
                "Goal-gap retrospective",
                retrospective_status,
                retrospective_risk,
                [
                    path
                    for path in (
                        latest_retrospective.get("source_report_json"),
                        latest_retrospective.get("source_report"),
                        "runtime_dashboard.goal_gap",
                    )
                    if path
                ],
                retrospective_rationale,
                [
                    str(item.get("id"))
                    for item in latest_retrospective.get("likely_next_stage_themes", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                or (["produce-goal-gap-retrospective"] if retrospective_status == "missing" else []),
            ),
            "runtime_dashboard": self._goal_gap_scorecard_category(
                "runtime_dashboard",
                "Runtime dashboard",
                runtime_status,
                runtime_risk,
                ["runtime_dashboard", "latest_reports", "status_summary"],
                runtime_rationale,
                ["refresh-runtime-dashboard-evidence"] if runtime_status != "complete" else [],
            ),
            "approval_capability_policy_safety": self._goal_gap_scorecard_category(
                "approval_capability_policy_safety",
                "Approval and capability policy safety",
                approval_status,
                approval_risk,
                ["approval_queue", "capability_policy", "manifest_index.policy_decision_summary"],
                approval_rationale,
                ["resolve-approval-policy-blockers"] if approval_status == "blocked" else [],
            ),
            "workspace_dispatch_fairness_backoff": self._goal_gap_scorecard_category(
                "workspace_dispatch_fairness_backoff",
                "Workspace dispatch fairness and backoff",
                workspace_status,
                workspace_risk,
                ["runtime_dashboard.workspace_dispatch", "workspace_dispatch.latest_reports"],
                workspace_rationale,
                ["add-workspace-dispatch-evidence"] if workspace_status == "missing" else [],
            ),
            "real_e2e_evidence": self._goal_gap_scorecard_category(
                "real_e2e_evidence",
                "Real E2E evidence",
                e2e_status,
                e2e_risk,
                [
                    *e2e_manifest_paths,
                    "browser_user_experience",
                    "runtime_dashboard.browser_user_experience",
                    "test_inventory",
                    "source_inventory",
                    "roadmap.tasks.e2e",
                ],
                e2e_rationale,
                ["add-real-e2e-evidence"] if e2e_status != "complete" else [],
            ),
        }
        return [category_by_id[category_id] for category_id in GOAL_GAP_SCORECARD_CATEGORY_ORDER]

    def _goal_gap_scorecard_category(
        self,
        category_id: str,
        title: str,
        status: str,
        risk_score: int,
        evidence_paths: list[Any],
        rationale: str,
        recommended_next_stage_themes: list[Any],
    ) -> dict[str, Any]:
        normalized_status = status if status in GOAL_GAP_SCORECARD_STATUS_ORDER else "missing"
        bounded_evidence = [
            str(path)
            for path in dict.fromkeys(str(path) for path in evidence_paths if str(path).strip())
        ][: SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_evidence_paths"]]
        bounded_themes = [
            str(theme)
            for theme in dict.fromkeys(str(theme) for theme in recommended_next_stage_themes if str(theme).strip())
        ][: SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_themes"]]
        score = max(0, min(100, int(risk_score)))
        return {
            "id": category_id,
            "title": title,
            "status": normalized_status,
            "risk_score": score,
            "severity": self._goal_gap_scorecard_severity(score),
            "evidence_paths": bounded_evidence,
            "rationale": self._truncate_text(str(rationale), SELF_ITERATION_CONTEXT_LIMITS["message_chars"]),
            "recommended_next_stage_themes": bounded_themes,
        }

    def _goal_gap_scorecard_severity(self, risk_score: int) -> int:
        if risk_score >= 80:
            return 4
        if risk_score >= 60:
            return 3
        if risk_score >= 30:
            return 2
        if risk_score > 0:
            return 1
        return 0

    def _goal_gap_scorecard_theme_summary(self, categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        themes: dict[str, dict[str, Any]] = {}
        for category in categories:
            category_id = str(category.get("id") or "")
            for theme in category.get("recommended_next_stage_themes", []):
                theme_id = str(theme)
                item = themes.setdefault(theme_id, {"id": theme_id, "source_categories": []})
                if category_id and category_id not in item["source_categories"]:
                    item["source_categories"].append(category_id)
        return [themes[key] for key in sorted(themes)[: SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_themes"] * 3]]

    def _goal_gap_scorecard_latest_retrospective_summary(
        self,
        *,
        latest_reports: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidates: list[tuple[Path, str | None]] = []
        reports = latest_reports if isinstance(latest_reports, dict) else None
        drive_reports = reports.get("drive_reports") if isinstance(reports, dict) else {}
        files = drive_reports.get("files") if isinstance(drive_reports, dict) else []
        if isinstance(files, list):
            for item in files:
                if not isinstance(item, dict) or not item.get("json_path"):
                    continue
                json_path = self.project_root / str(item["json_path"])
                candidates.append((json_path, str(item.get("path")) if item.get("path") else None))
        if not candidates:
            drive_dir = self.report_dir / "drives"
            json_paths = sorted(
                [path for path in drive_dir.glob("*.json") if path.is_file()] if drive_dir.exists() else [],
                key=self._project_relative_path,
            )
            candidates = [(path, None) for path in reversed(json_paths)]

        seen: set[str] = set()
        for json_path, report_path in candidates:
            key = str(json_path)
            if key in seen:
                continue
            seen.add(key)
            try:
                payload = load_mapping(json_path)
            except Exception:
                continue
            retrospective = payload.get("goal_gap_retrospective") if isinstance(payload, dict) else None
            if not isinstance(retrospective, dict):
                continue
            trigger = retrospective.get("trigger") if isinstance(retrospective.get("trigger"), dict) else {}
            request = (
                retrospective.get("request_self_iteration")
                if isinstance(retrospective.get("request_self_iteration"), dict)
                else {}
            )
            risks = [
                {
                    "id": item.get("id"),
                    "severity": item.get("severity"),
                    "evidence": item.get("evidence"),
                }
                for item in retrospective.get("remaining_risks", [])
                if isinstance(item, dict)
            ]
            themes = [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "source_risks": deepcopy(item.get("source_risks", [])),
                }
                for item in retrospective.get("likely_next_stage_themes", [])
                if isinstance(item, dict)
            ]
            source_report = report_path or payload.get("drive_report")
            return {
                "available": True,
                "source": "latest_drive_report",
                "source_report": str(source_report) if source_report else None,
                "source_report_json": self._project_relative_path(json_path),
                "stop_class": trigger.get("stop_class"),
                "status": trigger.get("status"),
                "remaining_risk_count": len(risks),
                "remaining_risks": risks[: SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_themes"] * 2],
                "likely_next_stage_theme_count": len(themes),
                "likely_next_stage_themes": themes[: SELF_ITERATION_CONTEXT_LIMITS["goal_gap_scorecard_themes"]],
                "request_self_iteration": {
                    "recommended": bool(request.get("recommended", False)),
                    "reason": request.get("reason"),
                    "blocked_by": deepcopy(request.get("blocked_by", []))
                    if isinstance(request.get("blocked_by"), list)
                    else [],
                },
            }
        return {
            "available": False,
            "source": "current_status",
            "source_report": None,
            "source_report_json": None,
            "remaining_risk_count": 0,
            "remaining_risks": [],
            "likely_next_stage_theme_count": 0,
            "likely_next_stage_themes": [],
            "request_self_iteration": {"recommended": False, "reason": "no latest drive retrospective is available"},
        }

    def capability_policy_summary(self, manifest_index: dict[str, Any] | None = None) -> dict[str, Any]:
        index = manifest_index or self.manifest_index_summary()
        policy_summary = index.get("policy_decision_summary") if isinstance(index, dict) else {}
        if not isinstance(policy_summary, dict):
            policy_summary = {}
        blocking = [
            deepcopy(decision)
            for decision in policy_summary.get("blocking", [])
            if isinstance(decision, dict) and decision.get("kind") == "capability_policy"
        ]
        requires_approval = [
            deepcopy(decision)
            for decision in policy_summary.get("requires_approval", [])
            if isinstance(decision, dict) and decision.get("kind") == "capability_policy"
        ]
        return {
            "schema_version": CAPABILITY_POLICY_SCHEMA_VERSION,
            "known_capabilities": sorted(self._known_capability_names()),
            "unsafe_capabilities": sorted(UNSAFE_EXECUTOR_CAPABILITIES),
            "capability_classifications": classify_capabilities(tuple(sorted(self._known_capability_names()))),
            "deny_by_default_classes": sorted(UNSAFE_CAPABILITY_CLASSES),
            "decision_count": int(policy_summary.get("by_kind", {}).get("capability_policy", 0))
            if isinstance(policy_summary.get("by_kind"), dict)
            else 0,
            "blocking_count": len(blocking),
            "blocking": blocking,
            "requires_approval": requires_approval,
        }

    def executor_diagnostics_summary(self) -> dict[str, Any]:
        executors: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        action_required_count = 0
        warning_count = 0
        for executor_id in self.executor_registry.ids():
            executor = self.executor_registry.get(executor_id)
            metadata = self.executor_registry.metadata_for(executor_id)
            diagnostics: dict[str, Any] = {}
            if executor is not None:
                diagnostics_fn = getattr(executor, "diagnostics", None)
                if callable(diagnostics_fn):
                    try:
                        raw = diagnostics_fn(project_root=self.project_root)
                        diagnostics = raw if isinstance(raw, dict) else {}
                    except Exception as exc:
                        diagnostics = {
                            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
                            "id": executor_id,
                            "status": "error",
                            "configured": False,
                            "enabled": False,
                            "error": str(exc),
                            "recommended_action": "Inspect the executor diagnostics failure.",
                        }
            capabilities = [
                str(capability)
                for capability in metadata.get("capabilities", [])
                if str(capability).strip()
            ]
            unsafe_capabilities = [
                capability
                for capability in capabilities
                if capability in UNSAFE_EXECUTOR_CAPABILITIES
            ]
            unsafe_classifications = classify_capabilities(unsafe_capabilities)
            unsafe_classes = sorted(
                class_name
                for class_name in UNSAFE_CAPABILITY_CLASSES
                if unsafe_classifications.get("classes", {}).get(class_name)
            )
            status = str(diagnostics.get("status") or "registered")
            by_status[status] = by_status.get(status, 0) + 1
            warnings = diagnostics.get("warnings") if isinstance(diagnostics.get("warnings"), list) else []
            warning_count += len(warnings)
            if diagnostics.get("recommended_action"):
                action_required_count += 1
            executors.append(
                {
                    "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
                    "id": executor_id,
                    "name": metadata.get("name"),
                    "kind": metadata.get("kind"),
                    "adapter": metadata.get("adapter"),
                    "status": status,
                    "configured": bool(diagnostics.get("configured", status in {"ready", "registered"})),
                    "enabled": bool(diagnostics.get("enabled", True)),
                    "requires_agent_approval": metadata.get("requires_agent_approval"),
                    "uses_command_policy": metadata.get("uses_command_policy"),
                    "capabilities": capabilities,
                    "unsafe_capabilities": unsafe_capabilities,
                    "unsafe_classes": unsafe_classes,
                    "diagnostics": diagnostics,
                }
            )
        return {
            "schema_version": EXECUTOR_DIAGNOSTICS_SCHEMA_VERSION,
            "kind": "engineering-harness.executor-diagnostics",
            "executor_count": len(executors),
            "by_status": dict(sorted(by_status.items())),
            "ready_count": by_status.get("ready", 0),
            "configured_count": sum(1 for item in executors if item["configured"]),
            "action_required_count": action_required_count,
            "warning_count": warning_count,
            "executors": executors,
        }

    def status_summary(self, *, refresh_approvals: bool = True) -> dict[str, Any]:
        state = self.load_state()
        state_tasks = state.get("tasks", {})
        milestones: dict[str, dict[str, Any]] = {}
        for task in self.iter_tasks():
            task_state = state_tasks.get(task.id, {})
            status = str(task_state.get("status", task.status))
            milestone = milestones.setdefault(
                task.milestone_id,
                {
                    "id": task.milestone_id,
                    "title": task.milestone_title,
                    "total": 0,
                    "done": 0,
                    "blocked": 0,
                    "failed": 0,
                    "pending": 0,
                },
            )
            milestone["total"] += 1
            if status in COMPLETED_STATUSES:
                milestone["done"] += 1
            elif status in BLOCKED_STATUSES:
                milestone["blocked"] += 1
            elif status == "failed":
                milestone["failed"] += 1
            else:
                milestone["pending"] += 1
        drive_control = self.drive_control_summary()
        executor_watchdog = self.executor_watchdog_summary()
        approval_queue = (
            self.approval_queue_summary(status_filter="pending")
            if refresh_approvals
            else self._approval_queue_summary_from_state(state, status_filter="pending")
        )
        manifest_index = self.manifest_index_summary()
        capability_policy = self.capability_policy_summary(manifest_index)
        executor_diagnostics = self.executor_diagnostics_summary()
        safety_audit = manifest_index.get("safety_audit", {}) if isinstance(manifest_index, dict) else {}
        failure_isolation = self.latest_isolated_failures_summary()
        replay_guard = self.replay_guard_summary()
        next_task = self.next_task()
        checkpoint_readiness = self.checkpoint_readiness(self._checkpoint_readiness_task(next_task, drive_control))
        daemon_supervisor_runtime = self._runtime_daemon_supervisor_summary()
        experience = self.frontend_experience_plan()
        browser_user_experience = self.browser_user_experience_summary()
        spec_coverage = self.spec_coverage_summary()
        summary = {
            "project": self.roadmap.get("project", self.project_root.name),
            "profile": self.roadmap.get("profile"),
            "root": str(self.project_root),
            "roadmap": str(self.roadmap_path),
            "state": str(self.state_path),
            "spec": spec_coverage,
            "milestones": list(milestones.values()),
            "next_task": redact_evidence(self.task_payload(next_task)),
            "checkpoint_readiness": checkpoint_readiness,
            "experience": experience,
            "domain_frontend": deepcopy(experience.get("decision_contract", {})),
            "browser_user_experience": browser_user_experience,
            "continuation": self.continuation_summary(),
            "self_iteration": self.self_iteration_summary(),
            "drive_control": drive_control,
            "executor_watchdog": executor_watchdog,
            "approval_queue": approval_queue,
            "manifest_index": manifest_index,
            "capability_policy": capability_policy,
            "executor_diagnostics": executor_diagnostics,
            "safety_audit": safety_audit,
            "failure_isolation": failure_isolation,
            "replay_guard": replay_guard,
            "daemon_supervisor_runtime": daemon_supervisor_runtime,
        }
        summary["goal_gap_scorecard"] = self.goal_gap_scorecard(status_summary=summary)
        summary["runtime_dashboard"] = self.runtime_dashboard_summary(summary)
        summary["operator_console"] = self.operator_console_summary(summary)
        return summary

    def drive_goal_gap_retrospective(
        self,
        drive_payload: dict[str, Any],
        *,
        final_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status_summary = deepcopy(final_status or self.status_summary())
        evidence = self._goal_gap_evidence(status_summary)
        task_counts = self._goal_gap_task_counts(status_summary)
        stop_class = self._goal_gap_stop_class(drive_payload, status_summary, task_counts)
        risks = self._goal_gap_remaining_risks(drive_payload, evidence, task_counts, stop_class)
        request_self_iteration = self._goal_gap_self_iteration_request(
            drive_payload,
            evidence,
            task_counts,
            stop_class,
        )
        payload = {
            "schema_version": GOAL_GAP_RETROSPECTIVE_SCHEMA_VERSION,
            "kind": "engineering-harness.goal-gap-retrospective",
            "generated_at": utc_now(),
            "goal": UNATTENDED_RELIABILITY_GOAL,
            "trigger": {
                "status": str(drive_payload.get("status", "unknown")),
                "message": str(drive_payload.get("message", "")),
                "stop_class": stop_class,
                "tasks_run": len(drive_payload.get("results", [])),
                "continuations": len(drive_payload.get("continuations", [])),
                "self_iterations": len(drive_payload.get("self_iterations", [])),
                "result_status_counts": self._goal_gap_drive_result_status_counts(drive_payload),
            },
            "task_counts": task_counts,
            "completed_reliability_capabilities": self._goal_gap_completed_capabilities(evidence, task_counts),
            "remaining_risks": risks,
            "likely_next_stage_themes": self._goal_gap_next_stage_themes(
                risks,
                evidence,
                task_counts,
                stop_class,
                request_self_iteration,
            ),
            "request_self_iteration": request_self_iteration,
            "evidence": evidence,
            "evidence_sources": [
                "status_summary",
                "manifest_index",
                "latest_reports",
                "drive_control",
                "approval_queue",
                "failure_isolation",
                "checkpoint_readiness",
                "self_iteration_context_packs",
                "tests",
                "source",
                "git",
            ],
        }
        return self._redact_context_value(payload)

    def _goal_gap_evidence(self, status_summary: dict[str, Any]) -> dict[str, Any]:
        manifest_index = self.manifest_index()
        return {
            "status_summary": {
                "project": status_summary.get("project"),
                "profile": status_summary.get("profile"),
                "root": status_summary.get("root"),
                "roadmap": status_summary.get("roadmap"),
                "state": status_summary.get("state"),
                "milestones": deepcopy(status_summary.get("milestones", [])),
                "next_task": deepcopy(status_summary.get("next_task")),
                "continuation": deepcopy(status_summary.get("continuation", {})),
                "self_iteration": deepcopy(status_summary.get("self_iteration", {})),
                "failure_isolation": deepcopy(status_summary.get("failure_isolation", {})),
                "checkpoint_readiness": deepcopy(status_summary.get("checkpoint_readiness", {})),
            },
            "manifest_index": {
                "path": manifest_index.get("manifest_index_path"),
                "manifest_count": manifest_index.get("manifest_count", 0),
                "latest_manifest": manifest_index.get("latest_manifest"),
                "latest_by_task": manifest_index.get("latest_by_task", {}),
                "status_counts": manifest_index.get("status_counts", {}),
                "policy_decision_summary": manifest_index.get("policy_decision_summary", {}),
            },
            "latest_reports": self._self_iteration_report_context(),
            "drive_control": deepcopy(status_summary.get("drive_control") or self.drive_control_summary()),
            "approval_queue": deepcopy(
                status_summary.get("approval_queue") or self.approval_queue_summary(status_filter="pending")
            ),
            "failure_isolation": deepcopy(
                status_summary.get("failure_isolation") or self.latest_isolated_failures_summary()
            ),
            "checkpoint_readiness": deepcopy(status_summary.get("checkpoint_readiness") or self.checkpoint_readiness()),
            "self_iteration_context_packs": self._goal_gap_self_iteration_context_pack_summaries(),
            "tests": self._self_iteration_test_inventory(),
            "source": self._self_iteration_source_inventory(),
            "git": self._self_iteration_git_context(),
        }

    def _goal_gap_self_iteration_context_pack_summaries(self) -> dict[str, Any]:
        assessment_dir = self.report_dir / "assessments"
        paths = sorted(
            assessment_dir.glob("*-self-iteration-context.json") if assessment_dir.exists() else [],
            key=self._project_relative_path,
        )
        recent = list(reversed(paths))[: SELF_ITERATION_CONTEXT_LIMITS["recent_report_count"]]
        files: list[dict[str, Any]] = []
        for path in recent:
            item: dict[str, Any] = {
                "path": self._project_relative_path(path),
                "bytes": self._file_size(path),
            }
            try:
                context = load_mapping(path)
            except Exception as exc:
                item["load_error"] = self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"])
            else:
                summary = context.get("summary") if isinstance(context.get("summary"), dict) else {}
                item.update(
                    {
                        "reason": context.get("reason"),
                        "snapshot_path": context.get("snapshot_path"),
                        "context_path": context.get("context_path"),
                        "summary": deepcopy(summary),
                    }
                )
            files.append(item)
        return {
            "total_count": len(paths),
            "included_count": len(files),
            "files": files,
        }

    def _goal_gap_task_counts(self, status_summary: dict[str, Any]) -> dict[str, int]:
        counts = {
            "total": 0,
            "done": 0,
            "pending": 0,
            "failed": 0,
            "blocked": 0,
        }
        for milestone in status_summary.get("milestones", []):
            if not isinstance(milestone, dict):
                continue
            for key in counts:
                counts[key] += int(milestone.get(key, 0) or 0)
        return counts

    def _goal_gap_drive_result_status_counts(self, drive_payload: dict[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in drive_payload.get("results", []):
            if not isinstance(result, dict):
                continue
            status = str(result.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def _goal_gap_stop_class(
        self,
        drive_payload: dict[str, Any],
        status_summary: dict[str, Any],
        task_counts: dict[str, int],
    ) -> str:
        status = str(drive_payload.get("status", "unknown"))
        message = str(drive_payload.get("message", "")).lower()
        if status == "budget_exhausted":
            return "budget_exhausted"
        if status == "isolated_failure":
            return "isolated_failure"
        if status in {"blocked", "failed"}:
            return status
        if status in {"cancelled", "paused", "stale", "stalled", "timeout"}:
            return "interrupted"
        if status == "completed" and status_summary.get("next_task") is None:
            if "queue is empty" in message or task_counts.get("pending", 0) == 0:
                return "queue_empty"
        return status

    def _goal_gap_completed_capabilities(
        self,
        evidence: dict[str, Any],
        task_counts: dict[str, int],
    ) -> list[dict[str, Any]]:
        capabilities: list[dict[str, Any]] = []

        def add(capability_id: str, title: str, evidence_ref: str, detail: str) -> None:
            capabilities.append(
                {
                    "id": capability_id,
                    "title": title,
                    "evidence": evidence_ref,
                    "detail": detail,
                }
            )

        add("status_summary", "Durable status summary is available", "status_summary", "final drive state was captured")
        drive_control = evidence.get("drive_control", {})
        if isinstance(drive_control, dict) and drive_control.get("schema_version"):
            add(
                "drive_control_watchdog",
                "Drive control and watchdog state are recorded",
                "drive_control",
                f"drive control status is {drive_control.get('status', 'unknown')}",
            )
        approval_queue = evidence.get("approval_queue", {})
        if isinstance(approval_queue, dict) and approval_queue.get("schema_version"):
            add(
                "approval_queue_audit",
                "Approval queue state is auditable",
                "approval_queue",
                f"{approval_queue.get('pending_count', 0)} pending approval(s)",
            )
        if task_counts.get("done", 0) > 0:
            add(
                "validated_task_execution",
                "At least one roadmap task completed under harness control",
                "status_summary.milestones",
                f"{task_counts['done']} completed task(s)",
            )
        manifest_index = evidence.get("manifest_index", {})
        if int(manifest_index.get("manifest_count", 0) or 0) > 0:
            add(
                "task_manifest_index",
                "Task run manifests are indexed",
                "manifest_index",
                f"{manifest_index.get('manifest_count', 0)} manifest(s)",
            )
        latest_reports = evidence.get("latest_reports", {})
        task_reports = latest_reports.get("task_reports", {}) if isinstance(latest_reports, dict) else {}
        drive_reports = latest_reports.get("drive_reports", {}) if isinstance(latest_reports, dict) else {}
        if int(task_reports.get("included_count", 0) or 0) or int(drive_reports.get("included_count", 0) or 0):
            add(
                "local_report_evidence",
                "Recent local report metadata is available",
                "latest_reports",
                (
                    f"{task_reports.get('included_count', 0)} task report(s), "
                    f"{drive_reports.get('included_count', 0)} drive report(s)"
                ),
            )
        tests = evidence.get("tests", {})
        if int(tests.get("total_count", 0) or 0) > 0:
            add(
                "test_inventory",
                "Local test inventory is visible",
                "tests",
                f"{tests.get('total_count', 0)} test file(s)",
            )
        git = evidence.get("git", {})
        if git.get("is_repository"):
            add(
                "git_state",
                "Git state was inspected locally",
                "git",
                f"{len(git.get('status_lines', []))} status line(s)",
            )
        context_packs = evidence.get("self_iteration_context_packs", {})
        if int(context_packs.get("included_count", 0) or 0) > 0:
            add(
                "self_iteration_context_packs",
                "Self-iteration context packs exist",
                "self_iteration_context_packs",
                f"{context_packs.get('included_count', 0)} context pack(s)",
            )
        return capabilities

    def _goal_gap_remaining_risks(
        self,
        drive_payload: dict[str, Any],
        evidence: dict[str, Any],
        task_counts: dict[str, int],
        stop_class: str,
    ) -> list[dict[str, Any]]:
        risks: list[dict[str, Any]] = []

        def add(risk_id: str, severity: str, summary: str, evidence_ref: str) -> None:
            risks.append(
                {
                    "id": risk_id,
                    "severity": severity,
                    "summary": summary,
                    "evidence": evidence_ref,
                }
            )

        status_summary = evidence.get("status_summary", {})
        next_task = status_summary.get("next_task") if isinstance(status_summary, dict) else None
        continuation = status_summary.get("continuation", {}) if isinstance(status_summary, dict) else {}
        self_iteration = status_summary.get("self_iteration", {}) if isinstance(status_summary, dict) else {}
        approval_queue = evidence.get("approval_queue", {})
        pending_approvals = int(approval_queue.get("pending_count", 0) or 0) if isinstance(approval_queue, dict) else 0
        failure_isolation = evidence.get("failure_isolation", {})
        unresolved_isolated = (
            int(failure_isolation.get("unresolved_count", 0) or 0)
            if isinstance(failure_isolation, dict)
            else 0
        )

        if stop_class == "budget_exhausted":
            add(
                "budget_exhausted",
                "medium",
                str(drive_payload.get("message", "drive budget was exhausted")),
                "trigger",
            )
        elif stop_class == "isolated_failure":
            add(
                "isolated_failure",
                "high",
                str(drive_payload.get("message", "drive stopped on an isolated failure")),
                "failure_isolation",
            )
        elif stop_class == "failed":
            add("failed_task", "high", str(drive_payload.get("message", "drive stopped on failure")), "trigger")
        elif stop_class == "blocked":
            add("blocked_task", "high", str(drive_payload.get("message", "drive stopped on a blocker")), "trigger")
        elif stop_class == "interrupted":
            add(
                "interrupted_drive",
                "medium",
                str(drive_payload.get("message", "drive stopped before completion")),
                "drive_control",
            )

        if task_counts.get("pending", 0) > 0:
            add(
                "pending_roadmap_tasks",
                "medium",
                f"{task_counts['pending']} roadmap task(s) remain pending",
                "status_summary.milestones",
            )
        if task_counts.get("failed", 0) > 0:
            add(
                "failed_roadmap_tasks",
                "high",
                f"{task_counts['failed']} roadmap task(s) are failed",
                "status_summary.milestones",
            )
        if task_counts.get("blocked", 0) > 0:
            add(
                "blocked_roadmap_tasks",
                "high",
                f"{task_counts['blocked']} roadmap task(s) are blocked",
                "status_summary.milestones",
            )
        if pending_approvals > 0:
            add(
                "pending_approvals",
                "high",
                f"{pending_approvals} approval gate(s) are pending",
                "approval_queue",
            )
        if unresolved_isolated > 0:
            add(
                "unresolved_isolated_failures",
                "high",
                f"{unresolved_isolated} isolated task failure(s) need local recovery",
                "failure_isolation",
            )

        manifest_index = evidence.get("manifest_index", {})
        if int(manifest_index.get("manifest_count", 0) or 0) == 0:
            add("missing_task_manifests", "medium", "no task run manifests are indexed yet", "manifest_index")

        tests = evidence.get("tests", {})
        if int(tests.get("total_count", 0) or 0) == 0:
            add("missing_tests", "medium", "no local test files were discovered", "tests")

        git = evidence.get("git", {})
        checkpoint_readiness = evidence.get("checkpoint_readiness", {})
        blocking_paths = (
            checkpoint_readiness.get("blocking_paths", [])
            if isinstance(checkpoint_readiness, dict) and isinstance(checkpoint_readiness.get("blocking_paths"), list)
            else []
        )
        safe_paths = (
            checkpoint_readiness.get("safe_to_checkpoint_paths", [])
            if isinstance(checkpoint_readiness, dict)
            and isinstance(checkpoint_readiness.get("safe_to_checkpoint_paths"), list)
            else []
        )
        dirty_paths = (
            checkpoint_readiness.get("dirty_paths", [])
            if isinstance(checkpoint_readiness, dict) and isinstance(checkpoint_readiness.get("dirty_paths"), list)
            else []
        )
        if isinstance(checkpoint_readiness, dict) and checkpoint_readiness.get("blocking"):
            add(
                "checkpoint_blocking_paths" if blocking_paths else "checkpoint_not_ready",
                "high",
                str(checkpoint_readiness.get("recommended_action", "git checkpoint readiness is blocked")),
                "checkpoint_readiness",
            )
        elif safe_paths and dirty_paths:
            add(
                "checkpoint_pending",
                "low",
                f"{len(safe_paths)} safe-to-checkpoint path(s) remain dirty without blocking paths",
                "checkpoint_readiness.safe_to_checkpoint_paths",
            )
        if git.get("is_repository") and git.get("status_lines"):
            if blocking_paths or not isinstance(checkpoint_readiness, dict):
                add(
                    "dirty_git_state",
                    "medium",
                    f"{len(git.get('status_lines', []))} git status line(s) remain dirty",
                    "git.status_lines",
                )
        elif not git.get("is_repository"):
            add("missing_git_state", "low", "project root is not inside a git repository", "git")

        if next_task is None:
            pending_stage_count = int(continuation.get("pending_stage_count", 0) or 0)
            continuation_enabled = bool(continuation.get("enabled", False))
            self_iteration_enabled = bool(self_iteration.get("enabled", False))
            if stop_class == "queue_empty":
                add("roadmap_queue_empty", "medium", "no next roadmap task is available", "status_summary.next_task")
            if continuation_enabled and pending_stage_count > 0:
                add(
                    "pending_continuation_stage",
                    "medium",
                    f"{pending_stage_count} continuation stage(s) are not materialized",
                    "status_summary.continuation",
                )
            elif continuation_enabled and int(continuation.get("stage_count", 0) or 0) > 0:
                add(
                    "continuation_exhausted",
                    "medium",
                    "all configured continuation stages are materialized or exhausted",
                    "status_summary.continuation",
                )
            if not self_iteration_enabled:
                add(
                    "self_iteration_disabled",
                    "medium",
                    "self-iteration is not enabled for the empty queue",
                    "status_summary.self_iteration",
                )
            context_packs = evidence.get("self_iteration_context_packs", {})
            if self_iteration_enabled and int(context_packs.get("included_count", 0) or 0) == 0:
                add(
                    "self_iteration_context_not_refreshed",
                    "low",
                    "no previous self-iteration context pack is available for comparison",
                    "self_iteration_context_packs",
                )
        return risks

    def _goal_gap_next_stage_themes(
        self,
        risks: list[dict[str, Any]],
        evidence: dict[str, Any],
        task_counts: dict[str, int],
        stop_class: str,
        request_self_iteration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        themes: dict[str, dict[str, Any]] = {}

        def add(theme_id: str, title: str, source: str) -> None:
            item = themes.setdefault(theme_id, {"id": theme_id, "title": title, "source_risks": []})
            if source and source not in item["source_risks"]:
                item["source_risks"].append(source)

        risk_ids = {str(risk.get("id")) for risk in risks if isinstance(risk, dict)}
        if "failed_roadmap_tasks" in risk_ids or "blocked_roadmap_tasks" in risk_ids or "pending_approvals" in risk_ids:
            add("resolve-blockers", "Resolve failed, blocked, or approval-gated work", "blocked_or_failed")
        if "unresolved_isolated_failures" in risk_ids or "isolated_failure" in risk_ids:
            add("recover-isolated-failure", "Resolve isolated task failure before extending the roadmap", "failure_isolation")
        if task_counts.get("pending", 0) > 0 or stop_class == "budget_exhausted":
            add("drain-queued-tasks", "Drain remaining queued roadmap tasks under a renewed budget", "pending_work")
        if request_self_iteration.get("recommended"):
            add(
                "request-self-iteration",
                "Request a self-iteration stage from the local context evidence",
                "roadmap_queue_empty",
            )
        if "roadmap_queue_empty" in risk_ids and not request_self_iteration.get("recommended"):
            add("refresh-roadmap-plan", "Refresh continuation or self-iteration planning before the next drive", "queue_empty")
        if "missing_tests" in risk_ids:
            add("add-local-tests", "Add deterministic local tests before deeper unattended execution", "missing_tests")
        if "checkpoint_blocking_paths" in risk_ids or "dirty_git_state" in risk_ids:
            add("close-git-boundary", "Review and checkpoint or clean local git changes", "dirty_git_state")
        if "checkpoint_pending" in risk_ids:
            add(
                "checkpoint-pending",
                "Carry the protected checkpoint window without treating it as an unrelated blocker",
                "checkpoint_pending",
            )
        if "missing_task_manifests" in risk_ids:
            add("produce-manifest-evidence", "Run a local harness task to produce manifest evidence", "missing_task_manifests")
        if not themes:
            add(
                "monitor-next-drive",
                "Run the next drive and compare its local reports against this retrospective",
                "no_open_risk",
            )
        return [themes[key] for key in sorted(themes)]

    def _goal_gap_self_iteration_request(
        self,
        drive_payload: dict[str, Any],
        evidence: dict[str, Any],
        task_counts: dict[str, int],
        stop_class: str,
    ) -> dict[str, Any]:
        status_summary = evidence.get("status_summary", {})
        next_task = status_summary.get("next_task") if isinstance(status_summary, dict) else None
        continuation = status_summary.get("continuation", {}) if isinstance(status_summary, dict) else {}
        self_iteration = status_summary.get("self_iteration", {}) if isinstance(status_summary, dict) else {}
        approval_queue = evidence.get("approval_queue", {})
        pending_approvals = int(approval_queue.get("pending_count", 0) or 0) if isinstance(approval_queue, dict) else 0
        failure_isolation = evidence.get("failure_isolation", {})
        unresolved_isolated = (
            int(failure_isolation.get("unresolved_count", 0) or 0)
            if isinstance(failure_isolation, dict)
            else 0
        )
        blocked_by: list[str] = []
        recommended = False

        if not bool(self_iteration.get("enabled", False)):
            blocked_by.append("self_iteration_disabled")
            reason = "self-iteration is not enabled in the roadmap"
        elif unresolved_isolated > 0 or stop_class == "isolated_failure":
            blocked_by.append("unresolved_isolated_failure")
            reason = "resolve isolated task failure evidence before requesting another self-iteration"
        elif task_counts.get("failed", 0) or task_counts.get("blocked", 0) or stop_class in {"failed", "blocked"}:
            blocked_by.append("unresolved_task_blockers")
            reason = "resolve failed or blocked tasks before requesting another self-iteration"
        elif pending_approvals > 0:
            blocked_by.append("pending_approvals")
            reason = "approval gates must be handled before requesting another self-iteration"
        elif stop_class == "interrupted":
            blocked_by.append("interrupted_drive")
            reason = "resume or clear the interrupted drive state before requesting another self-iteration"
        elif stop_class == "budget_exhausted":
            blocked_by.append("budget_exhausted")
            if next_task is not None:
                blocked_by.append("pending_task_queue")
            reason = str(drive_payload.get("message", "drive budget was exhausted"))
        elif next_task is not None:
            blocked_by.append("pending_task_queue")
            reason = f"next task `{next_task.get('id')}` should run before self-iteration"
        elif int(continuation.get("pending_stage_count", 0) or 0) > 0:
            blocked_by.append("pending_continuation_stage")
            reason = "materialize the pending continuation stage before requesting self-iteration"
        elif stop_class == "queue_empty":
            recommended = True
            reason = "the roadmap queue is empty and self-iteration is enabled"
        else:
            reason = "current evidence does not require another self-iteration"

        return {
            "recommended": recommended,
            "reason": reason,
            "blocked_by": blocked_by,
            "evidence": {
                "stop_class": stop_class,
                "next_task": deepcopy(next_task),
                "pending_stage_count": int(continuation.get("pending_stage_count", 0) or 0),
                "self_iteration_enabled": bool(self_iteration.get("enabled", False)),
                "pending_approval_count": pending_approvals,
            },
        }

    def frontend_experience_plan(self) -> dict[str, Any]:
        profile = str(self.roadmap.get("profile", "") or "").strip().lower()
        project_kind = self._roadmap_project_kind()
        project_name = str(self.roadmap.get("project", self.project_root.name))
        goal_text = self._roadmap_goal_text()
        hint_values = [self._roadmap_hint_text(profile=profile, project_kind=project_kind)]
        experience = self.roadmap.get("experience")
        if isinstance(experience, dict):
            return annotate_explicit_domain_frontend_plan(
                experience,
                project_name=project_name,
                profile=profile,
                project_kind=project_kind,
                goal_text=goal_text,
                hint_values=hint_values,
                source="explicit",
            )
        if experience is not None:
            return {
                "source": "explicit-invalid",
                "derived": False,
                "recommendation": None,
                "kind": None,
                "required": True,
                "frontend_required": True,
                "rationale": ["roadmap declares an experience block, but it is not a mapping"],
            }

        return build_domain_frontend_plan(
            project_name=project_name,
            profile=profile,
            project_kind=project_kind,
            goal_text=goal_text,
            hint_values=hint_values,
            source="derived",
        )

    def _derive_default_experience_kind(self) -> tuple[str, list[str]]:
        profile = str(self.roadmap.get("profile", "") or "").strip().lower()
        project_kind = self._roadmap_project_kind()
        decision = derive_domain_frontend_decision(
            project_name=str(self.roadmap.get("project", self.project_root.name)),
            profile=profile,
            project_kind=project_kind,
            goal_text=self._roadmap_goal_text(),
            hint_values=[self._roadmap_hint_text(profile=profile, project_kind=project_kind)],
        )
        return str(decision.get("experience_kind", "dashboard")), list(decision.get("rationale", []))

    def _experience_rationale(
        self,
        *,
        profile: str,
        project_kind: str,
        matched: list[str],
        decision: str,
    ) -> list[str]:
        rationale = [decision]
        if profile:
            rationale.append(f"profile: {profile}")
        if project_kind:
            rationale.append(f"project kind: {project_kind}")
        if matched:
            rationale.append("matched hints: " + ", ".join(matched[:6]))
        return rationale

    def _roadmap_project_kind(self) -> str:
        for key in ("project_kind", "kind", "category"):
            value = self.roadmap.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        _, project_kind = guess_profile(self.project_root)
        return project_kind

    def _roadmap_goal_text(self) -> str:
        goal = self.roadmap.get("goal")
        if isinstance(goal, dict):
            return str(goal.get("text") or goal.get("goal") or "")
        if isinstance(goal, str):
            return goal
        continuation = self.roadmap.get("continuation")
        if isinstance(continuation, dict):
            return str(continuation.get("goal") or "")
        return ""

    def _roadmap_hint_text(self, *, profile: str, project_kind: str) -> str:
        values: list[str] = [profile, project_kind]

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
                return
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if isinstance(value, str):
                text = value.strip()
                if text:
                    values.append(text)

        visit(self.roadmap)
        return " ".join(values).lower()

    def _keyword_matches(self, text: str, keywords: tuple[str, ...]) -> list[str]:
        return domain_frontend_keyword_matches(text, keywords)

    def frontend_task_plan(
        self,
        *,
        milestone_id: str = FRONTEND_TASK_MILESTONE_ID,
    ) -> dict[str, Any]:
        experience = self.frontend_experience_plan()
        errors: list[str] = []
        self._validate_experience_payload(experience, errors=errors)
        if errors:
            return {
                "status": "error",
                "message": "frontend experience plan is invalid",
                "errors": errors,
                "materialized": False,
                "experience": experience,
                "domain_frontend": deepcopy(experience.get("decision_contract", {})),
                "milestone": None,
                "tasks": [],
            }

        existing_task_ids = {task.id for task in self.iter_tasks()}
        milestone = self._build_frontend_milestone(
            experience,
            milestone_id=milestone_id,
            existing_task_ids=existing_task_ids,
        )
        tasks = milestone.get("tasks", []) if isinstance(milestone.get("tasks"), list) else []
        return {
            "status": "proposed",
            "message": f"proposed {len(tasks)} frontend task(s) from {experience.get('source')} experience plan",
            "materialized": False,
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "roadmap": str(self.roadmap_path),
            "experience": experience,
            "domain_frontend": deepcopy(experience.get("decision_contract", {})),
            "milestone": milestone,
            "tasks": tasks,
            "tasks_added": 0,
        }

    def spec_backlog_plan(
        self,
        *,
        source_paths: list[str] | None = None,
        include_blueprint: bool = False,
        from_stage: int = 1,
    ) -> dict[str, Any]:
        return build_spec_backlog_plan(
            project_root=self.project_root,
            roadmap=self.roadmap,
            source_paths=source_paths,
            include_blueprint=include_blueprint,
            from_stage=from_stage,
        )

    def materialize_spec_backlog(
        self,
        *,
        source_paths: list[str] | None = None,
        include_blueprint: bool = False,
        from_stage: int = 1,
        reason: str = "manual_spec_backlog_materialization",
    ) -> dict[str, Any]:
        plan = self.spec_backlog_plan(
            source_paths=source_paths,
            include_blueprint=include_blueprint,
            from_stage=from_stage,
        )
        stages = plan.get("stages", []) if isinstance(plan.get("stages"), list) else []
        if not stages:
            plan["status"] = "up_to_date"
            plan["materialized"] = False
            plan["message"] = "no new specification backlog stages to materialize"
            return plan

        updated, added_stage_count = materialize_spec_backlog_plan(self.roadmap, stages)
        if added_stage_count <= 0:
            plan["status"] = "up_to_date"
            plan["materialized"] = False
            plan["message"] = "no new specification backlog stages to materialize"
            return plan

        self.roadmap = updated
        self.save_roadmap()
        added_stages = stages[:added_stage_count]
        added_tasks = sum(len(stage.get("tasks", [])) for stage in added_stages)
        event = {
            "at": utc_now(),
            "event": "spec_backlog_materialization",
            "reason": reason,
            "generator": SPEC_BACKLOG_GENERATOR_ID,
            "stage_count": added_stage_count,
            "task_count": added_tasks,
            "sources": plan.get("sources", []),
        }
        append_jsonl(self.decision_log_path, event)
        plan["status"] = "materialized"
        plan["materialized"] = True
        plan["message"] = f"materialized {added_stage_count} specification backlog stage(s)"
        plan["stage_count"] = added_stage_count
        plan["task_count"] = added_tasks
        plan["stages"] = added_stages
        return plan

    def materialize_frontend_tasks(
        self,
        *,
        milestone_id: str = FRONTEND_TASK_MILESTONE_ID,
        reason: str = "manual_frontend_task_generation",
    ) -> dict[str, Any]:
        milestones = self.roadmap.get("milestones")
        if milestones is not None and not isinstance(milestones, list):
            experience = self.frontend_experience_plan()
            return {
                "status": "error",
                "message": "`milestones` must be a list before frontend tasks can be materialized",
                "materialized": False,
                "experience": experience,
                "domain_frontend": deepcopy(experience.get("decision_contract", {})),
                "milestone": None,
                "tasks": [],
            }

        existing_milestone = None
        for milestone in milestones or []:
            if isinstance(milestone, dict) and str(milestone.get("id", "")) == milestone_id:
                existing_milestone = milestone
                break
        if existing_milestone is not None:
            tasks = existing_milestone.get("tasks", []) if isinstance(existing_milestone.get("tasks"), list) else []
            experience = self.frontend_experience_plan()
            return {
                "status": "skipped",
                "message": f"milestone `{milestone_id}` already exists",
                "materialized": False,
                "project": str(self.roadmap.get("project", self.project_root.name)),
                "roadmap": str(self.roadmap_path),
                "experience": experience,
                "domain_frontend": deepcopy(experience.get("decision_contract", {})),
                "milestone": existing_milestone,
                "tasks": tasks,
                "tasks_added": 0,
            }

        proposal = self.frontend_task_plan(milestone_id=milestone_id)
        if proposal["status"] != "proposed":
            return proposal

        if milestones is None:
            milestones = []
            self.roadmap["milestones"] = milestones
        milestone = proposal["milestone"]
        milestones.append(milestone)
        self.save_roadmap()
        tasks = milestone.get("tasks", []) if isinstance(milestone, dict) else []
        event = {
            "at": utc_now(),
            "event": "frontend_task_generation",
            "reason": reason,
            "milestone_id": milestone_id,
            "tasks_added": len(tasks),
            "experience_kind": proposal["experience"].get("kind"),
            "experience_source": proposal["experience"].get("source"),
            "domain_frontend": deepcopy(proposal["experience"].get("decision_contract", {})),
        }
        append_jsonl(self.decision_log_path, event)
        return {
            **proposal,
            "status": "materialized",
            "message": f"materialized {len(tasks)} frontend task(s)",
            "materialized": True,
            "tasks_added": len(tasks),
        }

    def _build_frontend_milestone(
        self,
        experience: dict[str, Any],
        *,
        milestone_id: str,
        existing_task_ids: set[str],
    ) -> dict[str, Any]:
        kind = str(experience.get("kind", "dashboard"))
        label = FRONTEND_KIND_LABELS.get(kind, kind.replace("-", " "))
        generated_at = utc_now()
        task_ids = set(existing_task_ids)
        tasks = [
            self._frontend_contract_task(
                experience,
                kind=kind,
                label=label,
                task_ids=task_ids,
                generated_at=generated_at,
            )
        ]
        for journey in experience.get("e2e_journeys", []):
            if isinstance(journey, dict):
                tasks.append(
                    self._frontend_journey_task(
                        experience,
                        journey,
                        kind=kind,
                        label=label,
                        task_ids=task_ids,
                        generated_at=generated_at,
                    )
                )
        return {
            "id": milestone_id,
            "title": "Frontend Visualization",
            "status": "planned",
            "objective": f"Create stack-neutral {label} tasks and E2E journey gates from the roadmap experience plan.",
            "generated_by": FRONTEND_TASK_GENERATOR,
            "generated_at": generated_at,
            "experience_kind": kind,
            "experience_source": experience.get("source"),
            "domain_frontend": deepcopy(experience.get("decision_contract", {})),
            "tasks": tasks,
        }

    def _frontend_contract_task(
        self,
        experience: dict[str, Any],
        *,
        kind: str,
        label: str,
        task_ids: set[str],
        generated_at: str,
    ) -> dict[str, Any]:
        task_id = self._unique_frontend_task_id(f"frontend-{kind}-experience-contract", task_ids)
        personas = self._string_items(experience.get("personas"))
        surfaces = self._string_items(experience.get("primary_surfaces"))
        journeys = [
            journey
            for journey in experience.get("e2e_journeys", [])
            if isinstance(journey, dict)
        ]
        required_terms = [kind, *personas, *surfaces, *[str(journey.get("id", "")) for journey in journeys]]
        return {
            "id": task_id,
            "title": f"Define {label} experience contract",
            "status": "pending",
            "max_attempts": 2,
            "max_task_iterations": 2,
            "manual_approval_required": False,
            "agent_approval_required": True,
            "file_scope": ["docs/**", "tests/**", "templates/**"],
            "implementation": [
                {
                    "name": "Draft stack-neutral experience contract",
                    "executor": "codex",
                    "prompt": self._frontend_contract_prompt(experience, label=label),
                    "timeout_seconds": 3600,
                    "sandbox": "workspace-write",
                }
            ],
            "acceptance": [
                {
                    "name": f"{label} experience contract is documented",
                    "command": self._content_check_command(
                        "docs/frontend-experience.md",
                        required_terms,
                        missing_label="missing frontend experience terms",
                    ),
                    "timeout_seconds": 60,
                }
            ],
            "e2e": [
                {
                    "name": f"{journey.get('id')} journey is represented in the experience contract",
                    "command": self._content_check_command(
                        "docs/frontend-experience.md",
                        [str(journey.get("id", "")), str(journey.get("persona", ""))],
                        missing_label="missing frontend journey terms",
                    ),
                    "timeout_seconds": 60,
                }
                for journey in journeys
            ],
            "frontend": self._frontend_task_metadata(experience, task_kind="experience-contract"),
            "generated_by": FRONTEND_TASK_GENERATOR,
            "generated_at": generated_at,
        }

    def _frontend_journey_task(
        self,
        experience: dict[str, Any],
        journey: dict[str, Any],
        *,
        kind: str,
        label: str,
        task_ids: set[str],
        generated_at: str,
    ) -> dict[str, Any]:
        guidance = FRONTEND_KIND_TASK_GUIDANCE[kind]
        journey_id = str(journey.get("id", "")).strip()
        journey_slug = self._slugify(journey_id or "journey")
        task_id = self._unique_frontend_task_id(f"frontend-{kind}-{journey_slug}", task_ids)
        persona = str(journey.get("persona", "")).strip()
        auth = experience.get("auth") if isinstance(experience.get("auth"), dict) else {}
        roles = self._string_items(auth.get("roles") if isinstance(auth, dict) else [])
        surfaces = self._string_items(experience.get("primary_surfaces"))
        acceptance_terms = [
            kind,
            journey_id,
            persona,
            *roles,
            *surfaces[:4],
            *self._string_items(guidance.get("acceptance_terms")),
        ]
        candidates = [str(pattern).format(slug=journey_slug) for pattern in guidance["journey_candidates"]]
        browser_gate = (
            browser_user_experience_gate(self.project_root, experience=experience, journey=journey)
            if is_browser_experience_kind(kind)
            else None
        )
        if browser_gate is not None:
            declaration_paths = browser_gate.get("route_form_role_declarations", [])
            if isinstance(declaration_paths, list):
                candidates = [*declaration_paths, *candidates]
            e2e_gate = {
                "name": f"{journey_id} browser user-experience gate passes",
                "command": browser_user_experience_command(journey_id),
                "guidance": (
                    "Declare the journey's local routes, expected forms, and expected accessibility roles, "
                    "then capture DOM or screenshot evidence under the configured browser E2E artifact path."
                ),
                "timeout_seconds": 1200,
                "user_experience_gate": browser_gate,
            }
        else:
            e2e_gate = {
                "name": f"{journey_id} e2e journey check exists",
                "command": self._candidate_content_check_command(
                    candidates,
                    [journey_id, persona],
                    missing_label="missing e2e journey check",
                ),
                "timeout_seconds": 120,
            }
        return {
            "id": task_id,
            "title": f"Add {label} journey check for {journey_id}",
            "status": "pending",
            "max_attempts": 2,
            "max_task_iterations": 2,
            "manual_approval_required": False,
            "agent_approval_required": True,
            "file_scope": list(guidance["file_scope"]),
            "implementation": [
                {
                    "name": f"Implement {journey_id} experience check",
                    "executor": "codex",
                    "prompt": self._frontend_journey_prompt(
                        experience,
                        journey,
                        label=label,
                        guidance=str(guidance["implementation_focus"]),
                        candidates=candidates,
                    ),
                    "timeout_seconds": 3600,
                    "sandbox": "workspace-write",
                }
            ],
            "acceptance": [
                {
                    "name": f"{label} acceptance criteria cover {journey_id}",
                    "command": self._content_check_command(
                        "docs/frontend-experience.md",
                        acceptance_terms,
                        missing_label="missing frontend acceptance terms",
                    ),
                    "timeout_seconds": 60,
                }
            ],
            "e2e": [e2e_gate],
            "frontend": self._frontend_task_metadata(
                experience,
                task_kind="journey-check",
                journey=journey,
                candidate_paths=candidates,
            ),
            "generated_by": FRONTEND_TASK_GENERATOR,
            "generated_at": generated_at,
        }

    def _frontend_contract_prompt(self, experience: dict[str, Any], *, label: str) -> str:
        return (
            "Create or update `docs/frontend-experience.md` as a stack-neutral experience contract.\n"
            f"Experience kind: {experience.get('kind')} ({label}).\n"
            f"Personas: {', '.join(self._string_items(experience.get('personas')))}.\n"
            f"Primary surfaces: {', '.join(self._string_items(experience.get('primary_surfaces')))}.\n"
            f"Auth: {json.dumps(experience.get('auth', {}), sort_keys=True)}.\n"
            f"E2E journeys: {json.dumps(experience.get('e2e_journeys', []), sort_keys=True)}.\n"
            "Document expected screens or non-browser surfaces, loading/empty/error states, role or auth boundaries, "
            "and the files or commands that will exercise each journey. Use the existing project conventions; "
            "do not introduce a frontend framework solely to satisfy this task."
        )

    def _frontend_journey_prompt(
        self,
        experience: dict[str, Any],
        journey: dict[str, Any],
        *,
        label: str,
        guidance: str,
        candidates: list[str],
    ) -> str:
        return (
            f"Implement or document the `{journey.get('id')}` {label} journey check using the project's existing stack.\n"
            f"Persona: {journey.get('persona')}.\n"
            f"Goal: {journey.get('goal')}.\n"
            f"Experience kind: {experience.get('kind')}.\n"
            f"Guidance: {guidance}\n"
            "Keep the work local and deterministic. Browser projects may use their existing browser test framework; "
            "when no local Playwright runner is installed, declare static HTML routes, expected forms, and roles for "
            "the harness browser smoke. API-only and CLI-only projects may use documented examples, API tests, CLI "
            "tests, or shell/Python checks.\n"
            f"Place journey evidence or executable checks in one of: {', '.join(candidates)}.\n"
            "For browser journeys, capture screenshot or DOM evidence under `artifacts/browser-e2e/`.\n"
            "Update `docs/frontend-experience.md` with the acceptance criteria and the selected E2E command. "
            "Do not require private services, live deployments, paid accounts, or a specific frontend framework."
        )

    def _frontend_task_metadata(
        self,
        experience: dict[str, Any],
        *,
        task_kind: str,
        journey: dict[str, Any] | None = None,
        candidate_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "task_kind": task_kind,
            "experience_kind": experience.get("kind"),
            "experience_source": experience.get("source"),
            "domain": experience.get("domain"),
            "surface_policy": experience.get("surface_policy"),
            "required": bool(experience.get("frontend_required", True)),
            "personas": self._string_items(experience.get("personas")),
            "primary_surfaces": self._string_items(experience.get("primary_surfaces")),
            "auth": experience.get("auth") if isinstance(experience.get("auth"), dict) else {},
            "decision_contract": deepcopy(experience.get("decision_contract", {})),
            "stack_policy": "use existing project conventions; no required frontend framework",
        }
        if journey is not None:
            payload["e2e_journey"] = {
                "id": str(journey.get("id", "")),
                "persona": str(journey.get("persona", "")),
                "goal": str(journey.get("goal", "")),
            }
            if is_browser_experience_kind(str(experience.get("kind", ""))):
                payload["browser_user_experience_gate"] = browser_user_experience_gate(
                    self.project_root,
                    experience=experience,
                    journey=journey,
                )
        if candidate_paths is not None:
            payload["candidate_check_paths"] = candidate_paths
        return payload

    def _unique_frontend_task_id(self, base: str, task_ids: set[str]) -> str:
        base = self._slugify(base)
        candidate = base
        counter = 2
        while candidate in task_ids:
            candidate = f"{base}-{counter}"
            counter += 1
        task_ids.add(candidate)
        return candidate

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return slug or "item"

    def _string_items(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]

    def _content_check_command(
        self,
        path: str,
        required_terms: list[str],
        *,
        missing_label: str,
    ) -> str:
        terms = [term for term in dict.fromkeys(required_terms) if str(term).strip()]
        encoded_terms = self._b64_json(terms)
        return (
            "python3 -c \"import base64,json; from pathlib import Path; "
            f"p=Path('{path}'); assert p.exists(), 'missing {path}'; "
            "text=p.read_text(encoding='utf-8', errors='ignore').lower(); "
            f"terms=json.loads(base64.b64decode('{encoded_terms}')); "
            "missing=[term for term in terms if str(term).lower() not in text]; "
            f"assert not missing, '{missing_label}: ' + ', '.join(missing)\""
        )

    def _candidate_content_check_command(
        self,
        candidates: list[str],
        required_terms: list[str],
        *,
        missing_label: str,
    ) -> str:
        encoded_candidates = self._b64_json(candidates)
        encoded_terms = self._b64_json([term for term in dict.fromkeys(required_terms) if str(term).strip()])
        return (
            "python3 -c \"import base64,json; from pathlib import Path; "
            f"candidates=json.loads(base64.b64decode('{encoded_candidates}')); "
            "paths=[Path(item) for item in candidates if Path(item).exists()]; "
            f"assert paths, '{missing_label}; expected one of: ' + ', '.join(candidates); "
            "text='\\n'.join(path.read_text(encoding='utf-8', errors='ignore').lower() for path in paths); "
            f"terms=json.loads(base64.b64decode('{encoded_terms}')); "
            "missing=[term for term in terms if str(term).lower() not in text]; "
            f"assert not missing, '{missing_label} terms: ' + ', '.join(missing)\""
        )

    def _b64_json(self, value: Any) -> str:
        return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")

    def manifest_index(self) -> dict[str, Any]:
        if self.manifest_index_path.exists():
            return load_mapping(self.manifest_index_path)
        return self._build_manifest_index()

    def manifest_index_summary(self) -> dict[str, Any]:
        index = self.manifest_index()
        return {
            "path": index["manifest_index_path"],
            "manifest_count": index["manifest_count"],
            "latest_manifest": index["latest_manifest"],
            "status_counts": index["status_counts"],
            "policy_decision_summary": index.get("policy_decision_summary", {}),
            "safety_audit": index.get("safety_audit", {}),
            "failure_isolation": index.get("failure_isolation", {}),
        }

    def rebuild_manifest_index(self) -> dict[str, Any]:
        index = self._build_manifest_index()
        write_json(self.manifest_index_path, index)
        return index

    def _build_manifest_index(self) -> dict[str, Any]:
        manifests = []
        for manifest_path in sorted(
            self.report_dir.rglob("*.json"),
            key=lambda path: self._project_relative_path(path),
        ):
            if manifest_path.resolve() == self.manifest_index_path.resolve():
                continue
            payload = load_mapping(manifest_path)
            if payload.get("kind") != "engineering-harness.task-run-manifest":
                continue
            manifests.append(self._manifest_index_entry(manifest_path, payload))

        manifests.sort(
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item.get("finished_at") or ""),
                str(item.get("task_id") or ""),
                int(item.get("attempt") or 0),
                str(item.get("manifest_path") or ""),
            )
        )
        status_counts: dict[str, int] = {}
        latest_by_task: dict[str, str] = {}
        for item in manifests:
            status = str(item.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            latest_by_task[str(item["task_id"])] = str(item["manifest_path"])

        latest_manifest = manifests[-1]["manifest_path"] if manifests else None
        latest_finished_at = max((str(item.get("finished_at") or "") for item in manifests), default="") or None
        policy_decision_summary = self._aggregate_policy_decision_summaries(manifests)
        safety_audit = self._aggregate_safety_audit_summaries(manifests)
        failure_isolation = self._manifest_index_failure_isolation_summary(manifests)
        return {
            "schema_version": 1,
            "kind": "engineering-harness.task-run-manifest-index",
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "project_root": str(self.project_root),
            "roadmap_path": self._project_relative_path(self.roadmap_path) if self.roadmap_path else None,
            "report_dir": self._project_relative_path(self.report_dir),
            "manifest_index_path": self._project_relative_path(self.manifest_index_path),
            "updated_at": latest_finished_at,
            "manifest_count": len(manifests),
            "status_counts": dict(sorted(status_counts.items())),
            "policy_decision_summary": policy_decision_summary,
            "safety_audit": safety_audit,
            "failure_isolation": failure_isolation,
            "latest_manifest": latest_manifest,
            "latest_by_task": dict(sorted(latest_by_task.items())),
            "manifests": manifests,
        }

    def _new_task_report_path(self, task: HarnessTask) -> Path:
        base = self.report_dir / f"{slug_now()}-{task.id}.md"
        candidate = base
        counter = 2
        while candidate.exists() or candidate.with_suffix(".json").exists():
            candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
            counter += 1
        return candidate

    def _manifest_index_entry(self, manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        task = manifest.get("task") if isinstance(manifest.get("task"), dict) else {}
        milestone = manifest.get("milestone") if isinstance(manifest.get("milestone"), dict) else {}
        runs = manifest.get("runs") if isinstance(manifest.get("runs"), list) else []
        git = manifest.get("git") if isinstance(manifest.get("git"), dict) else {}
        policy_decisions = manifest.get("policy_decisions") if isinstance(manifest.get("policy_decisions"), list) else []
        policy_decision_summary = (
            manifest.get("policy_decision_summary")
            if isinstance(manifest.get("policy_decision_summary"), dict)
            else self._policy_decision_summary(policy_decisions)
        )
        safety_audit = (
            manifest.get("safety_audit")
            if isinstance(manifest.get("safety_audit"), dict)
            else self._safety_audit_evidence(policy_decisions)
        )
        failure_isolation = (
            self._compact_failure_isolation(manifest["failure_isolation"])
            if isinstance(manifest.get("failure_isolation"), dict)
            else None
        )
        entry = {
            "manifest_path": str(manifest.get("manifest_path") or self._project_relative_path(manifest_path)),
            "report_path": str(manifest.get("report_path") or ""),
            "task_id": str(manifest.get("task_id") or task.get("id") or ""),
            "task_title": str(task.get("title") or ""),
            "milestone_id": str(manifest.get("milestone_id") or milestone.get("id") or ""),
            "milestone_title": str(milestone.get("title") or ""),
            "status": str(manifest.get("status") or "unknown"),
            "message": str(manifest.get("message") or ""),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "dry_run": bool(manifest.get("dry_run", False)),
            "attempt": manifest.get("attempt"),
            "run_count": len(runs),
            "policy_decision_summary": policy_decision_summary,
            "safety_audit": safety_audit,
            "runs": [
                {
                    "phase": str(run.get("phase") or ""),
                    "name": str(run.get("name") or ""),
                    "executor": str(run.get("executor") or ""),
                    "status": str(run.get("status") or "unknown"),
                    "returncode": run.get("returncode"),
                    "requested_capabilities": run.get("requested_capabilities", [])
                    if isinstance(run.get("requested_capabilities"), list)
                    else [],
                    "user_experience_gate": run.get("user_experience_gate")
                    if isinstance(run.get("user_experience_gate"), dict)
                    else {},
                    "executor_capabilities": run.get("executor_capabilities", [])
                    if isinstance(run.get("executor_capabilities"), list)
                    else [],
                    "executor_metadata": run.get("executor_metadata") if isinstance(run.get("executor_metadata"), dict) else {},
                    "executor_result": run.get("executor_result") if isinstance(run.get("executor_result"), dict) else {},
                }
                for run in runs
                if isinstance(run, dict)
            ],
            "git": {
                "is_repository": bool(git.get("is_repository", False)),
                "head": git.get("head"),
            },
        }
        if failure_isolation is not None:
            entry["failure_isolation"] = failure_isolation
        return entry

    def _aggregate_safety_audit_summaries(self, manifests: list[dict[str, Any]]) -> dict[str, Any]:
        unsafe_decision_count = 0
        unsafe_classes: set[str] = set()
        unsafe_capabilities: set[str] = set()
        latest: dict[str, Any] | None = None
        for manifest in manifests:
            audit = manifest.get("safety_audit") if isinstance(manifest.get("safety_audit"), dict) else {}
            if not audit:
                continue
            unsafe_decision_count += int(audit.get("unsafe_decision_count", 0) or 0)
            unsafe_classes.update(str(item) for item in audit.get("unsafe_classes", []) if str(item).strip())
            unsafe_capabilities.update(str(item) for item in audit.get("unsafe_capabilities", []) if str(item).strip())
            if int(audit.get("unsafe_decision_count", 0) or 0) > 0:
                latest = deepcopy(audit)
        return {
            "schema_version": SAFETY_AUDIT_SCHEMA_VERSION,
            "kind": "engineering-harness.safety-audit-summary",
            "deny_by_default": True,
            "unsafe_decision_count": unsafe_decision_count,
            "unsafe_classes": sorted(unsafe_classes),
            "unsafe_capabilities": sorted(unsafe_capabilities),
            "latest_unsafe_audit": latest,
        }

    def _aggregate_policy_decision_summaries(self, manifests: list[dict[str, Any]]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        by_effect: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        total = 0
        blocking: list[dict[str, Any]] = []
        requires_approval: list[dict[str, Any]] = []
        for manifest in manifests:
            summary = manifest.get("policy_decision_summary")
            if not isinstance(summary, dict):
                continue
            total += int(summary.get("total") or 0)
            for source, target in (
                (summary.get("by_kind"), by_kind),
                (summary.get("by_outcome"), by_outcome),
                (summary.get("by_effect"), by_effect),
                (summary.get("by_severity"), by_severity),
            ):
                if not isinstance(source, dict):
                    continue
                for key, value in source.items():
                    target[str(key)] = target.get(str(key), 0) + int(value or 0)
            for decision in summary.get("blocking", []):
                if isinstance(decision, dict):
                    blocking.append(self._manifest_policy_summary_item(manifest, decision))
            for decision in summary.get("requires_approval", []):
                if isinstance(decision, dict):
                    requires_approval.append(self._manifest_policy_summary_item(manifest, decision))
        return {
            "total": total,
            "by_kind": dict(sorted(by_kind.items())),
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_effect": dict(sorted(by_effect.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "blocking": blocking,
            "requires_approval": requires_approval,
        }

    def _manifest_index_failure_isolation_summary(self, manifests: list[dict[str, Any]]) -> dict[str, Any]:
        isolated = [
            deepcopy(item["failure_isolation"])
            for item in manifests
            if isinstance(item.get("failure_isolation"), dict)
        ]
        isolated.sort(
            key=lambda item: (
                str(item.get("finished_at") or ""),
                str(item.get("task_id") or ""),
                str(item.get("manifest_path") or ""),
            ),
            reverse=True,
        )
        return {
            "schema_version": FAILURE_ISOLATION_SCHEMA_VERSION,
            "isolated_count": len(isolated),
            "latest_isolated_failures": isolated[:FAILURE_ISOLATION_SUMMARY_LIMIT],
        }

    def latest_isolated_failures_summary(self, *, limit: int = FAILURE_ISOLATION_SUMMARY_LIMIT) -> dict[str, Any]:
        state_tasks = self.load_state().get("tasks", {})
        index = self.manifest_index()
        entries_by_path = {
            str(item.get("manifest_path")): item
            for item in index.get("manifests", [])
            if isinstance(item, dict) and item.get("manifest_path")
        }
        unresolved: list[dict[str, Any]] = []
        latest_by_task = index.get("latest_by_task", {})
        if not isinstance(latest_by_task, dict):
            latest_by_task = {}
        for task_id, manifest_path in sorted(latest_by_task.items()):
            entry = entries_by_path.get(str(manifest_path))
            if not isinstance(entry, dict):
                continue
            isolation = entry.get("failure_isolation")
            if not isinstance(isolation, dict):
                continue
            task_state = state_tasks.get(str(task_id), {}) if isinstance(state_tasks, dict) else {}
            state_status = (
                str(task_state.get("status"))
                if isinstance(task_state, dict) and task_state.get("status") is not None
                else str(entry.get("status") or isolation.get("status") or "unknown")
            )
            if state_status not in ISOLATED_FAILURE_STATUSES:
                continue
            compact = deepcopy(isolation)
            compact["state_status"] = state_status
            unresolved.append(compact)
        unresolved.sort(
            key=lambda item: (
                str(item.get("finished_at") or ""),
                str(item.get("task_id") or ""),
                str(item.get("manifest_path") or ""),
            ),
            reverse=True,
        )
        return {
            "schema_version": FAILURE_ISOLATION_SCHEMA_VERSION,
            "unresolved_count": len(unresolved),
            "has_unresolved": bool(unresolved),
            "latest_isolated_failures": unresolved[: max(0, limit)],
        }

    def drive_failure_isolation_summary(
        self,
        drive_payload: dict[str, Any],
        *,
        final_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status_summary = final_status or drive_payload.get("final_status") or self.status_summary()
        latest_summary = (
            status_summary.get("failure_isolation")
            if isinstance(status_summary.get("failure_isolation"), dict)
            else self.latest_isolated_failures_summary()
        )
        result_isolations = [
            deepcopy(result["failure_isolation"])
            for result in drive_payload.get("results", [])
            if isinstance(result, dict) and isinstance(result.get("failure_isolation"), dict)
        ]
        report_paths = {
            key: drive_payload[key]
            for key in ("drive_report", "drive_report_json")
            if drive_payload.get(key)
        }
        return {
            "schema_version": FAILURE_ISOLATION_SCHEMA_VERSION,
            "kind": "engineering-harness.drive-failure-isolation",
            "unresolved_count": int(latest_summary.get("unresolved_count", 0) or 0),
            "has_unresolved": bool(latest_summary.get("has_unresolved", False)),
            "latest_isolated_failures": deepcopy(latest_summary.get("latest_isolated_failures", [])),
            "result_isolations": result_isolations,
            "report_paths": report_paths,
        }

    def _manifest_policy_summary_item(self, manifest: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "manifest_path": manifest.get("manifest_path"),
            "task_id": manifest.get("task_id"),
            "manifest_status": manifest.get("status"),
            **decision,
        }

    def _project_relative_path(self, path: Path) -> str:
        if path.is_relative_to(self.project_root):
            return str(path.relative_to(self.project_root))
        return str(path)

    def _spec_kind_for_path(self, value: str | None) -> str | None:
        suffix = Path(str(value or "")).suffix.lower()
        if suffix in {".md", ".markdown"}:
            return "markdown"
        if suffix == ".json":
            return "json"
        if suffix in {".yaml", ".yml"}:
            return "yaml"
        return None

    def _spec_kind_family(self, kind: str | None, path: str | None = None) -> str | None:
        normalized = str(kind or "").strip().lower()
        if "markdown" in normalized or normalized in {"md", "text"}:
            return "markdown"
        if "json" in normalized or "yaml" in normalized or "yml" in normalized or "index" in normalized:
            return "structured"
        inferred = self._spec_kind_for_path(path)
        if inferred == "markdown":
            return "markdown"
        if inferred in {"json", "yaml"}:
            return "structured"
        return None

    def _resolve_project_config_path(
        self,
        value: Any,
        *,
        location: str,
        errors: list[str],
    ) -> Path | None:
        text = str(value).strip() if isinstance(value, str) else ""
        if not text:
            errors.append(f"{location} must be a non-empty string")
            return None
        candidate = Path(text)
        resolved = (candidate if candidate.is_absolute() else self.project_root / candidate).resolve()
        if not resolved.is_relative_to(self.project_root):
            errors.append(f"{location} must resolve inside the project root")
            return None
        return resolved

    def _load_spec_index_mapping(
        self,
        path: Path,
        *,
        location: str,
        errors: list[str],
    ) -> dict[str, Any] | None:
        if not path.exists():
            errors.append(f"{location} file does not exist: {self._project_relative_path(path)}")
            return None
        try:
            return load_mapping(path)
        except Exception as exc:
            errors.append(f"{location} file is not a readable mapping: {exc}")
            return None

    def _requirement_ids_from_index_payload(self, payload: Any) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            text = str(value).strip() if isinstance(value, str) else ""
            if not text or text in seen:
                return
            if not SPEC_REQUIREMENT_ID_RE.fullmatch(text):
                return
            seen.add(text)
            ids.append(text)

        def walk(value: Any) -> None:
            if isinstance(value, str):
                add(value)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return

            for key, item in value.items():
                add(key)
                walk(item)

        walk(payload)
        return ids

    def _requirement_ids_from_markdown(self, path: Path, *, errors: list[str]) -> list[str]:
        if not path.exists():
            errors.append(f"spec.path file does not exist: {self._project_relative_path(path)}")
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"spec.path file is not readable: {exc}")
            return []
        heading_ids = [match.group("id") for match in SPEC_MARKDOWN_HEADING_RE.finditer(text)]
        if heading_ids:
            return list(dict.fromkeys(heading_ids))
        return list(dict.fromkeys(SPEC_REQUIREMENT_ID_RE.findall(text)))

    def spec_index_summary(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        spec = self.roadmap.get("spec")
        payload: dict[str, Any] = {
            "schema_version": SPEC_COVERAGE_SCHEMA_VERSION,
            "configured": False,
            "path": None,
            "path_exists": False,
            "kind": None,
            "kind_source": None,
            "requirements_index": None,
            "requirements_source": None,
            "known_requirements": [],
            "known_requirement_count": 0,
            "errors": errors,
            "warnings": warnings,
        }
        if spec is None:
            return payload
        if not isinstance(spec, dict):
            errors.append("top-level `spec` must be a mapping")
            return payload

        spec_path_value = spec.get("path")
        spec_kind_value = spec.get("kind")
        requirements_index_value = spec.get("requirements_index")
        development_plan = spec.get("development_plan")
        if isinstance(development_plan, str) and development_plan.strip():
            payload["development_plan"] = development_plan.strip()

        spec_path: Path | None = None
        if spec_path_value is not None:
            spec_path = self._resolve_project_config_path(spec_path_value, location="spec.path", errors=errors)
            if spec_path is not None:
                payload["configured"] = True
                payload["path"] = self._project_relative_path(spec_path)
                payload["path_exists"] = spec_path.exists()
                if not spec_path.exists():
                    errors.append(f"spec.path file does not exist: {self._project_relative_path(spec_path)}")

        if spec_kind_value is None:
            inferred_kind = self._spec_kind_for_path(str(spec_path_value or ""))
            if inferred_kind:
                payload["kind"] = inferred_kind
                payload["kind_source"] = "inferred_from_path"
        else:
            kind = str(spec_kind_value).strip() if isinstance(spec_kind_value, str) else ""
            if not kind:
                errors.append("spec.kind must be a non-empty string")
            else:
                payload["kind"] = kind
                payload["kind_source"] = "roadmap"
                payload["configured"] = True

        if (spec_kind_value is not None or requirements_index_value is not None) and spec_path_value is None:
            errors.append("spec.path is required when spec.kind or spec.requirements_index is configured")

        known_requirements: list[str] = []
        if requirements_index_value is not None:
            payload["configured"] = True
            if isinstance(requirements_index_value, str):
                index_path = self._resolve_project_config_path(
                    requirements_index_value,
                    location="spec.requirements_index",
                    errors=errors,
                )
                if index_path is not None:
                    payload["requirements_index"] = self._project_relative_path(index_path)
                    index_payload = self._load_spec_index_mapping(
                        index_path,
                        location="spec.requirements_index",
                        errors=errors,
                    )
                    if index_payload is not None:
                        known_requirements = self._requirement_ids_from_index_payload(index_payload)
                        payload["requirements_source"] = "requirements_index"
            elif isinstance(requirements_index_value, (dict, list)):
                payload["requirements_index"] = "inline"
                known_requirements = self._requirement_ids_from_index_payload(requirements_index_value)
                payload["requirements_source"] = "requirements_index"
            else:
                errors.append("spec.requirements_index must be a path string, mapping, or list")

            if not known_requirements and not any(error.startswith("spec.requirements_index") for error in errors):
                errors.append("spec.requirements_index must define at least one requirement id")

        if not known_requirements and spec_path is not None and spec_path.exists():
            family = self._spec_kind_family(payload.get("kind"), payload.get("path"))
            if family == "markdown":
                known_requirements = self._requirement_ids_from_markdown(spec_path, errors=errors)
                payload["requirements_source"] = "spec.path"
            elif family == "structured":
                index_payload = self._load_spec_index_mapping(spec_path, location="spec.path", errors=errors)
                if index_payload is not None:
                    known_requirements = self._requirement_ids_from_index_payload(index_payload)
                    payload["requirements_source"] = "spec.path"

        payload["known_requirements"] = sorted(known_requirements)
        payload["known_requirement_count"] = len(payload["known_requirements"])
        return payload

    def _safe_context_slug(self, value: Any, *, fallback: str = "item") -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
        return slug[:80] or fallback

    def _agent_context_pack_refs(self, task: HarnessTask, command: AcceptanceCommand) -> tuple[list[str], bool]:
        refs: list[str] = []

        def add(values: Any) -> None:
            for ref in self._normalize_spec_refs(values):
                if ref not in refs:
                    refs.append(ref)

        add(list(task.spec_refs))
        add(list(command.spec_refs))
        for item in (*task.implementation, *task.repair, *task.acceptance, *task.e2e):
            add(list(item.spec_refs))
        limit = int(AGENT_CONTEXT_PACK_LIMITS["requirement_count"])
        return refs[:limit], len(refs) > limit

    def _agent_context_pack_command_payload(self, command: AcceptanceCommand) -> dict[str, Any]:
        payload = {
            "name": command.name,
            "executor": command.executor,
            "required": command.required,
            "timeout_seconds": command.timeout_seconds,
            "model": command.model,
            "sandbox": command.sandbox,
            "spec_refs": list(command.spec_refs),
            "requested_capabilities": list(command.requested_capabilities),
        }
        if command.command:
            payload["command_excerpt"] = self._truncate_text(
                command.command,
                int(AGENT_CONTEXT_PACK_LIMITS["prompt_chars"]),
            )
        if command.prompt:
            payload["prompt_excerpt"] = self._truncate_text(
                command.prompt,
                int(AGENT_CONTEXT_PACK_LIMITS["prompt_chars"]),
            )
        return payload

    def _agent_context_pack_verification_payload(self, task: HarnessTask) -> tuple[list[dict[str, Any]], bool]:
        commands: list[tuple[str, AcceptanceCommand]] = [
            *[("acceptance", command) for command in task.acceptance],
            *[("e2e", command) for command in task.e2e],
        ]
        limit = int(AGENT_CONTEXT_PACK_LIMITS["verification_command_count"])
        return (
            [
                {
                    "phase": phase,
                    **self._agent_context_pack_command_payload(command),
                }
                for phase, command in commands[:limit]
            ],
            len(commands) > limit,
        )

    def _agent_context_pack_command_outline(self, command: AcceptanceCommand) -> dict[str, Any]:
        return {
            "name": command.name,
            "executor": command.executor,
            "required": command.required,
            "timeout_seconds": command.timeout_seconds,
            "model": command.model,
            "sandbox": command.sandbox,
            "spec_refs": list(command.spec_refs),
            "requested_capabilities": list(command.requested_capabilities),
            "has_command": command.command is not None,
            "has_prompt": command.prompt is not None,
        }

    def _agent_context_pack_task_outline(self, task: HarnessTask, *, status: str | None = None) -> dict[str, Any]:
        command_limit = int(AGENT_CONTEXT_PACK_LIMITS["roadmap_command_count"])

        def group(commands: tuple[AcceptanceCommand, ...]) -> dict[str, Any]:
            included = commands[:command_limit]
            return {
                "count": len(commands),
                "included_count": len(included),
                "truncated": len(commands) > command_limit,
                "commands": [self._agent_context_pack_command_outline(command) for command in included],
            }

        return {
            "id": task.id,
            "title": task.title,
            "milestone_id": task.milestone_id,
            "milestone_title": task.milestone_title,
            "status": status if status is not None else task.status,
            "file_scope": list(task.file_scope),
            "spec_refs": list(task.spec_refs),
            "manual_approval_required": task.manual_approval_required,
            "agent_approval_required": task.agent_approval_required,
            "max_task_iterations": task.max_task_iterations,
            "implementation": group(task.implementation),
            "repair": group(task.repair),
            "acceptance": group(task.acceptance),
            "e2e": group(task.e2e),
        }

    def _agent_context_pack_roadmap_context(self, task: HarnessTask) -> dict[str, Any]:
        milestones = self.roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            milestones = []
        tasks = list(self.iter_tasks())
        state = self.load_state()
        task_states = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        task_status_counts: dict[str, int] = {}
        pending_tasks: list[dict[str, Any]] = []
        task_limit = int(AGENT_CONTEXT_PACK_LIMITS["roadmap_task_count"])
        for candidate in tasks:
            task_state = task_states.get(candidate.id, {}) if isinstance(task_states.get(candidate.id), dict) else {}
            status = str(task_state.get("status", candidate.status))
            task_status_counts[status] = task_status_counts.get(status, 0) + 1
            if status in COMPLETED_STATUSES or status in BLOCKED_STATUSES:
                continue
            if len(pending_tasks) < task_limit:
                pending_tasks.append(self._agent_context_pack_task_outline(candidate, status=status))

        goal = self.roadmap.get("goal") if isinstance(self.roadmap.get("goal"), dict) else {}
        generated_from = self.roadmap.get("generated_from") if isinstance(self.roadmap.get("generated_from"), dict) else {}
        continuation = self.roadmap.get("continuation") if isinstance(self.roadmap.get("continuation"), dict) else {}
        continuation_stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if not isinstance(continuation_stages, list):
            continuation_stages = []
        materialized_stage_ids = {
            str(milestone.get("id", "")).strip()
            for milestone in milestones
            if isinstance(milestone, dict)
        }
        pending_stages = [
            stage
            for stage in continuation_stages
            if isinstance(stage, dict) and str(stage.get("id", "")).strip() not in materialized_stage_ids
        ]
        stage_limit = int(AGENT_CONTEXT_PACK_LIMITS["continuation_stage_count"])
        pending_task_count = 0
        for candidate in tasks:
            task_state = task_states.get(candidate.id, {}) if isinstance(task_states.get(candidate.id), dict) else {}
            status = str(task_state.get("status", candidate.status))
            if status not in COMPLETED_STATUSES | BLOCKED_STATUSES:
                pending_task_count += 1
        current_task_state = task_states.get(task.id, {}) if isinstance(task_states.get(task.id), dict) else {}
        return {
            "path": self._project_relative_path(self.roadmap_path),
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "profile": self.roadmap.get("profile"),
            "generated_by": self.roadmap.get("generated_by"),
            "goal": {
                "text": self._truncate_text(str(goal.get("text") or generated_from.get("goal") or ""), 500),
                "blueprint": goal.get("blueprint") or generated_from.get("blueprint_path"),
                "constraints": self._string_items(goal.get("constraints")) if isinstance(goal, dict) else [],
            },
            "milestone_count": len(milestones),
            "task_count": len(tasks),
            "task_status_counts": dict(sorted(task_status_counts.items())),
            "current_task": self._agent_context_pack_task_outline(
                task,
                status=str(current_task_state.get("status", task.status)),
            ),
            "pending_task_count": pending_task_count,
            "pending_tasks": pending_tasks,
            "pending_tasks_truncated": len(pending_tasks) >= task_limit and len(pending_tasks) < pending_task_count,
            "continuation": {
                "enabled": bool(continuation.get("enabled", False)) if isinstance(continuation, dict) else False,
                "goal": self._truncate_text(str(continuation.get("goal") or ""), 500)
                if isinstance(continuation, dict)
                else "",
                "blueprint": continuation.get("blueprint") if isinstance(continuation, dict) else None,
                "stage_count": len(continuation_stages),
                "pending_stage_count": len(pending_stages),
                "stages": [self._self_iteration_stage_summary(stage) for stage in pending_stages[:stage_limit]],
                "stage_count_truncated": len(pending_stages) > stage_limit,
            },
            "self_iteration": self.self_iteration_summary(),
        }

    def _agent_context_pack_manifest_context(self) -> dict[str, Any]:
        return self._self_iteration_manifest_context(
            recent_manifest_count=int(AGENT_CONTEXT_PACK_LIMITS["recent_manifest_count"]),
            manifest_run_count=int(AGENT_CONTEXT_PACK_LIMITS["manifest_run_count"]),
        )

    def _agent_context_pack_test_context(self) -> dict[str, Any]:
        return self._self_iteration_test_inventory(
            test_file_count=int(AGENT_CONTEXT_PACK_LIMITS["test_file_count"]),
            test_name_count=int(AGENT_CONTEXT_PACK_LIMITS["test_name_count"]),
        )

    def _agent_context_pack_git_context(self) -> dict[str, Any]:
        return self._self_iteration_git_context(git_commit_count=int(AGENT_CONTEXT_PACK_LIMITS["git_commit_count"]))

    def _requirement_heading_title(self, raw_title: str | None) -> str:
        title = str(raw_title or "").strip()
        if title.startswith(":"):
            title = title[1:].strip()
        return title

    def _requirement_excerpts_from_markdown(
        self,
        path: Path,
        refs: list[str],
        *,
        errors: list[str],
    ) -> dict[str, dict[str, Any]]:
        targets = set(refs)
        if not targets:
            return {}
        if not path.exists():
            errors.append(f"spec.path file does not exist: {self._project_relative_path(path)}")
            return {}
        limit = int(AGENT_CONTEXT_PACK_LIMITS["requirement_excerpt_chars"])
        found: dict[str, dict[str, Any]] = {}
        active_id: str | None = None
        active_title = ""
        active_level = 0
        active_lines: list[str] = []
        active_chars = 0
        capture_limit = limit + 1

        def finish_active() -> None:
            nonlocal active_id, active_title, active_level, active_lines, active_chars
            if active_id is not None:
                found[active_id] = {
                    "id": active_id,
                    "title": active_title,
                    "source_kind": "markdown",
                    "source_path": self._project_relative_path(path),
                    "excerpt": self._truncate_text("\n".join(active_lines).strip(), limit),
                }
            active_id = None
            active_title = ""
            active_level = 0
            active_lines = []
            active_chars = 0

        try:
            with path.open(encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.rstrip("\n")
                    heading = SPEC_MARKDOWN_ANY_HEADING_RE.match(line)
                    requirement_heading = SPEC_MARKDOWN_REQUIREMENT_HEADING_RE.match(line)
                    if active_id is not None and heading is not None and len(heading.group("marks")) <= active_level:
                        finish_active()
                    if requirement_heading is not None:
                        requirement_id = requirement_heading.group("id")
                        if requirement_id in targets:
                            active_id = requirement_id
                            active_title = self._requirement_heading_title(requirement_heading.group("title"))
                            active_level = len(requirement_heading.group("marks"))
                            active_lines = [line.strip()]
                            active_chars = len(active_lines[0])
                            continue
                    if active_id is not None:
                        if active_chars < capture_limit:
                            remaining = capture_limit - active_chars
                            piece = line[:remaining]
                            active_lines.append(piece)
                            active_chars += len(piece) + 1
            finish_active()
        except OSError as exc:
            errors.append(f"spec.path file is not readable: {exc}")
        return found

    def _structured_requirement_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return ""
        parts: list[str] = []
        for key in ("summary", "description", "text", "requirement", "body", "acceptance", "acceptance_evidence"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, list):
                parts.extend(str(entry).strip() for entry in item if str(entry).strip())
        return "\n".join(parts)

    def _requirement_excerpts_from_structured_payload(
        self,
        payload: Any,
        refs: list[str],
        *,
        source_path: str,
    ) -> dict[str, dict[str, Any]]:
        targets = set(refs)
        if not targets:
            return {}
        limit = int(AGENT_CONTEXT_PACK_LIMITS["requirement_excerpt_chars"])
        found: dict[str, dict[str, Any]] = {}

        def add(requirement_id: str, value: Any) -> None:
            if requirement_id not in targets:
                return
            title = ""
            if isinstance(value, dict):
                title = str(value.get("title") or value.get("name") or "").strip()
            text = self._structured_requirement_text(value)
            found[requirement_id] = {
                "id": requirement_id,
                "title": title,
                "source_kind": "structured",
                "source_path": source_path,
                "excerpt": self._truncate_text(text, limit) if text else "",
            }

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return
            direct_id = value.get("id") or value.get("requirement_id")
            if isinstance(direct_id, str) and SPEC_REQUIREMENT_ID_RE.fullmatch(direct_id.strip()):
                add(direct_id.strip(), value)
            for key, item in value.items():
                if isinstance(key, str) and SPEC_REQUIREMENT_ID_RE.fullmatch(key.strip()):
                    add(key.strip(), item)
                walk(item)

        walk(payload)
        return found

    def _requirement_excerpts_for_refs(self, refs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        if not refs:
            return [], []
        errors: list[str] = []
        found: dict[str, dict[str, Any]] = {}
        spec = self.roadmap.get("spec") if isinstance(self.roadmap.get("spec"), dict) else {}
        spec_path: Path | None = None
        spec_path_value = spec.get("path") if isinstance(spec, dict) else None
        if spec_path_value is not None:
            spec_path = self._resolve_project_config_path(spec_path_value, location="spec.path", errors=errors)
            if spec_path is not None and spec_path.exists():
                family = self._spec_kind_family(str(spec.get("kind") or ""), self._project_relative_path(spec_path))
                if family == "markdown":
                    found.update(self._requirement_excerpts_from_markdown(spec_path, refs, errors=errors))
                elif family == "structured":
                    index_payload = self._load_spec_index_mapping(spec_path, location="spec.path", errors=errors)
                    if index_payload is not None:
                        found.update(
                            self._requirement_excerpts_from_structured_payload(
                                index_payload,
                                refs,
                                source_path=self._project_relative_path(spec_path),
                            )
                        )

        requirements_index_value = spec.get("requirements_index") if isinstance(spec, dict) else None
        if requirements_index_value is not None:
            index_payload: Any | None = None
            source_path = "inline"
            if isinstance(requirements_index_value, str):
                index_path = self._resolve_project_config_path(
                    requirements_index_value,
                    location="spec.requirements_index",
                    errors=errors,
                )
                if index_path is not None:
                    source_path = self._project_relative_path(index_path)
                    index_payload = self._load_spec_index_mapping(
                        index_path,
                        location="spec.requirements_index",
                        errors=errors,
                    )
            elif isinstance(requirements_index_value, (dict, list)):
                index_payload = requirements_index_value
            if index_payload is not None:
                structured = self._requirement_excerpts_from_structured_payload(
                    index_payload,
                    refs,
                    source_path=source_path,
                )
                for requirement_id, entry in structured.items():
                    existing = found.get(requirement_id)
                    if existing is None:
                        found[requirement_id] = entry
                        continue
                    if not existing.get("title") and entry.get("title"):
                        existing["title"] = entry["title"]
                    if not existing.get("excerpt") and entry.get("excerpt"):
                        existing["excerpt"] = entry["excerpt"]

        requirements: list[dict[str, Any]] = []
        for ref in refs:
            entry = found.get(ref)
            if entry is None:
                requirements.append(
                    {
                        "id": ref,
                        "title": "",
                        "source_kind": None,
                        "source_path": None,
                        "excerpt": "",
                        "message": "No bounded requirement excerpt found for this spec ref.",
                    }
                )
            else:
                requirements.append(entry)
        return requirements, errors

    def _write_agent_context_pack(
        self,
        task: HarnessTask,
        command: AcceptanceCommand,
        *,
        phase: str | None,
        executor_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        refs, refs_truncated = self._agent_context_pack_refs(task, command)
        requirements, requirement_errors = self._requirement_excerpts_for_refs(refs)
        verification, verification_truncated = self._agent_context_pack_verification_payload(task)
        roadmap_context = self._agent_context_pack_roadmap_context(task)
        manifest_context = self._agent_context_pack_manifest_context()
        test_context = self._agent_context_pack_test_context()
        git_context = self._agent_context_pack_git_context()
        context_dir = self.report_dir / AGENT_CONTEXT_PACK_DIRNAME
        fingerprint = hashlib.sha256(
            f"{task.id}\0{phase or ''}\0{command.name}\0{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:10]
        context_path = context_dir / (
            f"{slug_now()}-{self._safe_context_slug(task.id, fallback='task')}-"
            f"{self._safe_context_slug(phase or 'phase', fallback='phase')}-"
            f"{self._safe_context_slug(command.name, fallback='command')}-{fingerprint}.json"
        )
        spec_summary = self.spec_index_summary()
        summary = {
            "spec_ref_count": len(refs),
            "spec_refs_truncated": refs_truncated,
            "requirement_count": len(requirements),
            "requirement_error_count": len(requirement_errors),
            "verification_command_count": len(verification),
            "verification_truncated": verification_truncated,
            "roadmap_task_count": roadmap_context.get("task_count", 0),
            "pending_task_count": roadmap_context.get("pending_task_count", 0),
            "manifest_count": manifest_context.get("index", {}).get("manifest_count", 0),
            "recent_manifest_count": len(manifest_context.get("recent_task_manifests", [])),
            "test_file_count": test_context.get("included_count", 0),
            "git_is_repository": git_context.get("is_repository", False),
            "git_status_line_count": len(git_context.get("status_lines", [])),
            "recent_commit_count": len(git_context.get("recent_commits", [])),
        }
        payload = {
            "schema_version": AGENT_CONTEXT_PACK_SCHEMA_VERSION,
            "kind": "engineering-harness.agent-context-pack",
            "created_at": utc_now(),
            "project": {
                "name": str(self.roadmap.get("project", self.project_root.name)),
                "root": str(self.project_root),
                "profile": self.roadmap.get("profile"),
                "roadmap_path": self._project_relative_path(self.roadmap_path),
            },
            "task": {
                "id": task.id,
                "title": task.title,
                "milestone_id": task.milestone_id,
                "milestone_title": task.milestone_title,
                "file_scope": list(task.file_scope),
                "spec_refs": list(task.spec_refs),
            },
            "phase": phase,
            "command": self._agent_context_pack_command_payload(command),
            "executor": executor_metadata,
            "spec_refs": refs,
            "spec_refs_truncated": refs_truncated,
            "summary": summary,
            "spec": {
                "configured": spec_summary.get("configured", False),
                "path": spec_summary.get("path"),
                "kind": spec_summary.get("kind"),
                "requirements_index": spec_summary.get("requirements_index"),
                "requirements_source": spec_summary.get("requirements_source"),
                "known_requirement_count": spec_summary.get("known_requirement_count", 0),
            },
            "requirements": requirements,
            "requirement_errors": requirement_errors,
            "verification": verification,
            "verification_truncated": verification_truncated,
            "roadmap": roadmap_context,
            "tests": test_context,
            "manifests": manifest_context,
            "git": git_context,
            "limits": dict(AGENT_CONTEXT_PACK_LIMITS),
        }
        redacted_payload = self._redact_context_value(payload)
        write_json(context_path, redacted_payload)
        encoded = context_path.read_bytes()
        relative_path = self._project_relative_path(context_path)
        summary = {
            "schema_version": AGENT_CONTEXT_PACK_SCHEMA_VERSION,
            "kind": "engineering-harness.agent-context-pack",
            "path": relative_path,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "spec_refs": refs,
            "spec_refs_truncated": refs_truncated,
            "requirement_count": len(requirements),
            "requirement_error_count": len(requirement_errors),
            "summary": summary,
            "limits": dict(AGENT_CONTEXT_PACK_LIMITS),
            "requirements": [
                {
                    "id": requirement.get("id"),
                    "title": requirement.get("title"),
                    "source_path": requirement.get("source_path"),
                    "excerpt": requirement.get("excerpt"),
                    "message": requirement.get("message"),
                }
                for requirement in requirements
            ],
        }
        return self._redact_context_value(summary)

    def _roadmap_spec_ref_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        materialized_stage_ids = {
            str(milestone.get("id", "")).strip()
            for milestone in self.roadmap.get("milestones", [])
            if isinstance(milestone, dict)
        }

        def add_refs(value: Any, *, location: str, scope: str, task_id: str | None = None) -> None:
            for ref in self._normalize_spec_refs(value):
                entries.append(
                    {
                        "id": ref,
                        "location": location,
                        "scope": scope,
                        "task_id": task_id,
                    }
                )

        def visit_task(task: Any, *, stage_location: str) -> None:
            if not isinstance(task, dict):
                return
            task_id = str(task.get("id", "")).strip()
            task_location = f"{stage_location} task `{task_id or '<missing>'}`"
            add_refs(task.get("spec_refs"), location=task_location, scope="task", task_id=task_id or None)
            for group_name in ("implementation", "repair", "acceptance", "e2e"):
                group = task.get(group_name, [])
                if not isinstance(group, list):
                    continue
                for command_index, item in enumerate(group):
                    if not isinstance(item, dict):
                        continue
                    command_name = str(item.get("name") or item.get("command") or item.get("prompt") or command_index)
                    add_refs(
                        item.get("spec_refs"),
                        location=f"{task_location} {group_name}[{command_index}] `{command_name}`",
                        scope=f"{group_name}_command",
                        task_id=task_id or None,
                    )

        for milestone in self.roadmap.get("milestones", []):
            if not isinstance(milestone, dict):
                continue
            milestone_id = str(milestone.get("id", "")).strip()
            tasks = milestone.get("tasks", [])
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                visit_task(task, stage_location=f"milestone `{milestone_id or '<missing>'}`")

        continuation = self.roadmap.get("continuation") if isinstance(self.roadmap.get("continuation"), dict) else {}
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                stage_id = str(stage.get("id", "")).strip()
                if stage_id in materialized_stage_ids:
                    continue
                tasks = stage.get("tasks", [])
                if not isinstance(tasks, list):
                    continue
                for task in tasks:
                    visit_task(task, stage_location=f"continuation stage `{stage_id or '<missing>'}`")

        return entries

    def spec_coverage_summary(self) -> dict[str, Any]:
        index = self.spec_index_summary()
        entries = self._roadmap_spec_ref_entries()
        known = set(index.get("known_requirements", []))
        referenced = {str(entry.get("id")) for entry in entries if entry.get("id")}
        unknown = sorted(referenced - known) if known else []
        unreferenced = sorted(known - referenced) if known else []
        task_ids_with_refs = sorted({str(entry.get("task_id")) for entry in entries if entry.get("task_id")})
        command_entries = [entry for entry in entries if str(entry.get("scope", "")).endswith("_command")]
        if index.get("errors"):
            status = "invalid"
        elif not index.get("configured"):
            status = "unconfigured"
        elif not known:
            status = "unindexed"
        elif unknown:
            status = "unknown_refs"
        else:
            status = "ok"
        coverage_ratio = None
        if known:
            coverage_ratio = round((len(known) - len(unreferenced)) / len(known), 4)
        return {
            "schema_version": SPEC_COVERAGE_SCHEMA_VERSION,
            "status": status,
            "configured": bool(index.get("configured")),
            "path": index.get("path"),
            "kind": index.get("kind"),
            "kind_source": index.get("kind_source"),
            "requirements_index": index.get("requirements_index"),
            "requirements_source": index.get("requirements_source"),
            "known_requirement_count": len(known),
            "referenced_requirement_count": len(referenced),
            "covered_requirement_count": len(known & referenced) if known else 0,
            "unknown_requirement_count": len(unknown),
            "unreferenced_requirement_count": len(unreferenced),
            "task_with_spec_refs_count": len(task_ids_with_refs),
            "command_with_spec_refs_count": len(command_entries),
            "reference_count": len(entries),
            "coverage_ratio": coverage_ratio,
            "referenced_requirements": sorted(referenced),
            "unknown_requirements": unknown[:25],
            "unreferenced_requirements": unreferenced[:25],
            "errors": list(index.get("errors", [])),
            "warnings": list(index.get("warnings", [])),
        }

    def validate_roadmap(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        spec_index = self.spec_index_summary()
        errors.extend(spec_index.get("errors", []))
        warnings.extend(spec_index.get("warnings", []))
        known_requirement_ids = set(spec_index.get("known_requirements", []))

        if int(self.roadmap.get("version", 0) or 0) <= 0:
            warnings.append("top-level `version` is missing or not positive")
        if not str(self.roadmap.get("project", "")).strip():
            errors.append("top-level `project` is required")
        if not str(self.roadmap.get("profile", "")).strip():
            warnings.append("top-level `profile` is missing")

        self._validate_experience_payload(self.roadmap.get("experience"), errors=errors)

        milestones = self.roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            errors.append("`milestones` must be a list")
            milestones = []

        seen_task_ids: set[str] = set()
        materialized_stage_ids: set[str] = set()
        for milestone_index, milestone in enumerate(milestones):
            if not isinstance(milestone, dict):
                errors.append(f"milestones[{milestone_index}] must be a mapping")
                continue
            milestone_id = str(milestone.get("id", "")).strip()
            if not milestone_id:
                errors.append(f"milestones[{milestone_index}].id is required")
                milestone_id = f"milestone-{milestone_index}"
            materialized_stage_ids.add(milestone_id)
            tasks = milestone.get("tasks", [])
            if not isinstance(tasks, list):
                errors.append(f"milestone `{milestone_id}` tasks must be a list")
                continue
            for task_index, task in enumerate(tasks):
                self._validate_task_payload(
                    task,
                    location=f"milestone `{milestone_id}` task[{task_index}]",
                    seen_task_ids=seen_task_ids,
                    known_requirement_ids=known_requirement_ids,
                    errors=errors,
                    warnings=warnings,
                )

        continuation = self.roadmap.get("continuation")
        if continuation is not None:
            if not isinstance(continuation, dict):
                errors.append("`continuation` must be a mapping")
            else:
                stages = continuation.get("stages", [])
                if stages is None:
                    stages = []
                if not isinstance(stages, list):
                    errors.append("`continuation.stages` must be a list")
                else:
                    seen_stage_ids: set[str] = set()
                    for stage_index, stage in enumerate(stages):
                        if not isinstance(stage, dict):
                            errors.append(f"continuation.stages[{stage_index}] must be a mapping")
                            continue
                        stage_id = str(stage.get("id", "")).strip()
                        if not stage_id:
                            errors.append(f"continuation.stages[{stage_index}].id is required")
                            stage_id = f"stage-{stage_index}"
                        elif stage_id in seen_stage_ids:
                            errors.append(f"duplicate continuation stage id: {stage_id}")
                        seen_stage_ids.add(stage_id)
                        tasks = stage.get("tasks", [])
                        if not isinstance(tasks, list) or not tasks:
                            errors.append(f"continuation stage `{stage_id}` must define at least one task")
                            continue
                        task_ids_for_stage = set() if stage_id in materialized_stage_ids else seen_task_ids
                        for task_index, task in enumerate(tasks):
                            self._validate_task_payload(
                                task,
                                location=f"continuation stage `{stage_id}` task[{task_index}]",
                                seen_task_ids=task_ids_for_stage,
                                known_requirement_ids=known_requirement_ids,
                                errors=errors,
                                warnings=warnings,
                            )

        status = "passed" if not errors else "failed"
        return {
            "status": status,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
            "spec": self.spec_coverage_summary(),
        }

    def _validate_experience_payload(
        self,
        experience: Any,
        *,
        errors: list[str],
    ) -> None:
        if experience is None:
            return
        if not isinstance(experience, dict):
            errors.append("top-level `experience` must be a mapping")
            return

        kind = str(experience.get("kind", "")).strip()
        if not kind:
            errors.append("experience.kind is required")
        elif kind not in EXPERIENCE_KINDS:
            allowed = ", ".join(sorted(EXPERIENCE_KINDS))
            errors.append(f"experience.kind `{kind}` is not supported; expected one of: {allowed}")

        decision_contract = experience.get("decision_contract")
        if decision_contract is not None:
            if not isinstance(decision_contract, dict):
                errors.append("experience.decision_contract must be a mapping")
            else:
                contract_kind = str(decision_contract.get("kind", "")).strip()
                if contract_kind and contract_kind != DOMAIN_FRONTEND_DECISION_KIND:
                    errors.append(
                        "experience.decision_contract.kind "
                        f"`{contract_kind}` is not supported; expected {DOMAIN_FRONTEND_DECISION_KIND}"
                    )
                contract_experience_kind = str(decision_contract.get("experience_kind", "")).strip()
                if kind and contract_experience_kind and contract_experience_kind != kind:
                    errors.append(
                        "experience.decision_contract.experience_kind must match experience.kind"
                    )
                if decision_contract.get("status") not in {None, "required"}:
                    errors.append("experience.decision_contract.status must be `required` when provided")

        personas = self._validate_string_list(
            experience.get("personas"),
            location="experience.personas",
            errors=errors,
            required=True,
            non_empty=True,
        )
        persona_names = set(personas)

        self._validate_string_list(
            experience.get("primary_surfaces"),
            location="experience.primary_surfaces",
            errors=errors,
            required=True,
            non_empty=True,
        )

        auth = experience.get("auth")
        if auth is None:
            errors.append("experience.auth is required")
        elif not isinstance(auth, dict):
            errors.append("experience.auth must be a mapping")
        else:
            auth_required = auth.get("required", False)
            if not isinstance(auth_required, bool):
                errors.append("experience.auth.required must be true or false")
            roles = self._validate_string_list(
                auth.get("roles"),
                location="experience.auth.roles",
                errors=errors,
                required=True,
                non_empty=False,
            )
            if auth_required is True and not roles:
                errors.append("experience.auth.roles must include at least one role when auth.required is true")

        journeys = experience.get("e2e_journeys")
        if journeys is None:
            errors.append("experience.e2e_journeys is required")
            return
        if not isinstance(journeys, list):
            errors.append("experience.e2e_journeys must be a list")
            return
        if not journeys:
            errors.append("experience.e2e_journeys must define at least one journey")
            return

        seen_journey_ids: set[str] = set()
        for journey_index, journey in enumerate(journeys):
            location = f"experience.e2e_journeys[{journey_index}]"
            if not isinstance(journey, dict):
                errors.append(f"{location} must be a mapping")
                continue
            journey_id = str(journey.get("id", "")).strip()
            if not journey_id:
                errors.append(f"{location}.id is required")
            elif journey_id in seen_journey_ids:
                errors.append(f"duplicate experience e2e journey id: {journey_id}")
            seen_journey_ids.add(journey_id)

            persona = str(journey.get("persona", "")).strip()
            if not persona:
                errors.append(f"{location}.persona is required")
            elif persona_names and persona not in persona_names:
                errors.append(f"{location}.persona `{persona}` must match one of experience.personas")

            if not str(journey.get("goal", "")).strip():
                errors.append(f"{location}.goal is required")

    def _validate_string_list(
        self,
        value: Any,
        *,
        location: str,
        errors: list[str],
        required: bool,
        non_empty: bool,
    ) -> list[str]:
        if value is None:
            if required:
                errors.append(f"{location} is required")
            return []
        if not isinstance(value, list):
            errors.append(f"{location} must be a list")
            return []
        if non_empty and not value:
            errors.append(f"{location} must include at least one item")
        items: list[str] = []
        seen_items: set[str] = set()
        for item_index, item in enumerate(value):
            text = str(item).strip() if isinstance(item, str) else ""
            if not text:
                errors.append(f"{location}[{item_index}] must be a non-empty string")
                continue
            if text in seen_items:
                errors.append(f"{location} contains duplicate item `{text}`")
            seen_items.add(text)
            items.append(text)
        return items

    def _known_capability_names(self) -> set[str]:
        names = set(LOCAL_CAPABILITY_VOCABULARY)
        for executor_id in self.executor_registry.ids():
            metadata = self.executor_registry.metadata_for(executor_id)
            capabilities = metadata.get("capabilities", [])
            if not isinstance(capabilities, list):
                continue
            names.update(str(capability) for capability in capabilities if str(capability).strip())
        return names

    def _requested_capability_field(self, item: dict[str, Any]) -> str | None:
        for field_name in COMMAND_CAPABILITY_REQUEST_FIELDS:
            if field_name in item:
                return field_name
        return None

    def _normalize_requested_capabilities(self, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        capabilities: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip() if isinstance(item, str) else ""
            if not text or text in seen:
                continue
            seen.add(text)
            capabilities.append(text)
        return tuple(capabilities)

    def _normalize_spec_refs(self, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        spec_refs: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip() if isinstance(item, str) else ""
            if not text or text in seen:
                continue
            seen.add(text)
            spec_refs.append(text)
        return tuple(spec_refs)

    def _validate_spec_refs(
        self,
        value: Any,
        *,
        location: str,
        known_requirement_ids: set[str] | None = None,
        errors: list[str],
    ) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            errors.append(f"{location} must be a non-empty list")
            return
        if not value:
            errors.append(f"{location} must be a non-empty list")
            return
        seen: set[str] = set()
        for ref_index, ref in enumerate(value):
            text = str(ref).strip() if isinstance(ref, str) else ""
            if not text:
                errors.append(f"{location}[{ref_index}] must be a non-empty string")
                continue
            if text in seen:
                errors.append(f"{location} contains duplicate spec ref `{text}`")
            seen.add(text)
            if known_requirement_ids and text not in known_requirement_ids:
                errors.append(f"{location}[{ref_index}] references unknown requirement id `{text}`")

    def _validate_requested_capabilities(
        self,
        item: dict[str, Any],
        *,
        location: str,
        errors: list[str],
    ) -> None:
        field_name = self._requested_capability_field(item)
        if field_name is None:
            return
        if all(field in item for field in COMMAND_CAPABILITY_REQUEST_FIELDS):
            errors.append(f"{location} must use only one capability request field")
        value = item.get(field_name)
        if not isinstance(value, list):
            errors.append(f"{location}.{field_name} must be a non-empty list")
            return
        if not value:
            errors.append(f"{location}.{field_name} must be a non-empty list")
            return
        known = self._known_capability_names()
        seen: set[str] = set()
        for capability_index, capability in enumerate(value):
            text = str(capability).strip() if isinstance(capability, str) else ""
            if not text:
                errors.append(f"{location}.{field_name}[{capability_index}] must be a non-empty string")
                continue
            if text in seen:
                errors.append(f"{location}.{field_name} contains duplicate capability `{text}`")
            seen.add(text)
            if text not in known:
                allowed = ", ".join(sorted(known))
                errors.append(f"{location}.{field_name}[{capability_index}] has unknown capability `{text}`; expected one of: {allowed}")

    def _validate_task_payload(
        self,
        task: Any,
        *,
        location: str,
        seen_task_ids: set[str],
        known_requirement_ids: set[str] | None,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        if not isinstance(task, dict):
            errors.append(f"{location} must be a mapping")
            return
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            errors.append(f"{location}.id is required")
            task_id = location
        elif task_id in seen_task_ids:
            errors.append(f"duplicate task id: {task_id}")
        seen_task_ids.add(task_id)
        if not str(task.get("title", task_id)).strip():
            warnings.append(f"task `{task_id}` title is empty")
        self._validate_spec_refs(
            task.get("spec_refs"),
            location=f"task `{task_id}` spec_refs",
            known_requirement_ids=known_requirement_ids,
            errors=errors,
        )
        file_scope = task.get("file_scope", [])
        if file_scope is not None and not isinstance(file_scope, list):
            errors.append(f"task `{task_id}` file_scope must be a list")
        for group_name in ("implementation", "repair", "acceptance", "e2e"):
            group = task.get(group_name, [])
            if group_name == "acceptance" and not group:
                errors.append(f"task `{task_id}` must define at least one acceptance command")
            if group is None:
                group = []
            if not isinstance(group, list):
                errors.append(f"task `{task_id}` {group_name} must be a list")
                continue
            for command_index, item in enumerate(group):
                self._validate_command_payload(
                    item,
                    location=f"task `{task_id}` {group_name}[{command_index}]",
                    known_requirement_ids=known_requirement_ids,
                    errors=errors,
                    warnings=warnings,
                )
        try:
            if int(task.get("max_task_iterations", 1)) < 1:
                errors.append(f"task `{task_id}` max_task_iterations must be positive")
        except (TypeError, ValueError):
            errors.append(f"task `{task_id}` max_task_iterations must be an integer")

    def _validate_command_payload(
        self,
        item: Any,
        *,
        location: str,
        known_requirement_ids: set[str] | None,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        if not isinstance(item, dict):
            errors.append(f"{location} must be a mapping")
            return
        self._validate_spec_refs(
            item.get("spec_refs"),
            location=f"{location}.spec_refs",
            known_requirement_ids=known_requirement_ids,
            errors=errors,
        )
        user_experience_gate = item.get("user_experience_gate")
        if user_experience_gate is not None:
            if not isinstance(user_experience_gate, dict):
                errors.append(f"{location}.user_experience_gate must be a mapping")
            else:
                gate_kind = str(user_experience_gate.get("kind", "")).strip()
                if gate_kind and gate_kind != BROWSER_USER_EXPERIENCE_GATE_KIND:
                    errors.append(
                        f"{location}.user_experience_gate.kind `{gate_kind}` is not supported; "
                        f"expected {BROWSER_USER_EXPERIENCE_GATE_KIND}"
                    )
        self._validate_requested_capabilities(item, location=location, errors=errors)
        executor = str(item.get("executor", "shell"))
        executor_adapter = self.executor_registry.get(executor)
        if executor_adapter is None:
            errors.append(f"{location} has unknown executor `{executor}`")
            return
        executor_metadata = executor_adapter.metadata
        if executor_metadata.input_mode == "command":
            command = item.get("command")
            if not str(command or "").strip():
                errors.append(f"{location} {executor} command is required")
            elif executor_metadata.uses_command_policy:
                allowed, reason = self.command_allowed(str(command))
                if not allowed:
                    warnings.append(f"{location} command is not currently allowlisted: {reason}")
        if executor_metadata.input_mode == "prompt" and not str(item.get("prompt", "") or item.get("command", "")).strip():
            errors.append(f"{location} {executor} prompt is required")
        safety_classification = self._command_safety_classification(
            AcceptanceCommand(
                name=str(item.get("name") or item.get("command") or item.get("prompt") or location),
                command=str(item["command"]) if item.get("command") is not None else None,
                prompt=str(item["prompt"]) if item.get("prompt") is not None else None,
                timeout_seconds=int(item.get("timeout_seconds", self.default_timeout))
                if str(item.get("timeout_seconds", self.default_timeout)).isdigit()
                else self.default_timeout,
                executor=executor,
                sandbox=str(item.get("sandbox", "workspace-write")),
                requested_capabilities=self._normalize_requested_capabilities(
                    item.get(self._requested_capability_field(item))
                    if self._requested_capability_field(item) is not None
                    else None
                ),
            )
        )
        if safety_classification.get("unsafe"):
            warnings.append(
                f"{location} command declares or implies unsafe capability classes: "
                f"{', '.join(safety_classification.get('unsafe_classes', []))}"
            )
        try:
            if int(item.get("timeout_seconds", self.default_timeout)) <= 0:
                errors.append(f"{location} timeout_seconds must be positive")
        except (TypeError, ValueError):
            errors.append(f"{location} timeout_seconds must be an integer")
        no_progress_value = item.get("no_progress_timeout_seconds", item.get("no_progress_seconds"))
        if no_progress_value is not None:
            try:
                if int(no_progress_value) < 0:
                    errors.append(f"{location} no_progress_timeout_seconds must be non-negative")
            except (TypeError, ValueError):
                errors.append(f"{location} no_progress_timeout_seconds must be an integer")

    def task_payload(self, task: HarnessTask | None) -> dict[str, Any] | None:
        if task is None:
            return None
        def command_payload(command: AcceptanceCommand) -> dict[str, Any]:
            payload = {
                "name": command.name,
                "timeout_seconds": command.timeout_seconds,
                "required": command.required,
                "executor": command.executor,
            }
            if command.command is not None:
                payload["command"] = command.command
            if command.prompt is not None:
                payload["prompt"] = command.prompt
            if command.model is not None:
                payload["model"] = command.model
            if command.no_progress_timeout_seconds is not None:
                payload["no_progress_timeout_seconds"] = command.no_progress_timeout_seconds
            if command.requested_capabilities:
                payload["requested_capabilities"] = list(command.requested_capabilities)
            if command.user_experience_gate:
                payload["user_experience_gate"] = deepcopy(command.user_experience_gate)
            if command.spec_refs:
                payload["spec_refs"] = list(command.spec_refs)
            payload["safety_classification"] = self._command_safety_classification(command)
            return payload

        return {
            "id": task.id,
            "title": task.title,
            "milestone_id": task.milestone_id,
            "milestone_title": task.milestone_title,
            "file_scope": list(task.file_scope),
            "manual_approval_required": task.manual_approval_required,
            "agent_approval_required": task.agent_approval_required,
            "max_task_iterations": task.max_task_iterations,
            "spec_refs": list(task.spec_refs),
            "implementation": [command_payload(command) for command in task.implementation],
            "repair": [command_payload(command) for command in task.repair],
            "acceptance": [command_payload(command) for command in task.acceptance],
            "e2e": [command_payload(command) for command in task.e2e],
        }

    def _policy_input(
        self,
        task: HarnessTask,
        *,
        phase: str = "task",
        command: AcceptanceCommand | None = None,
        safety: dict[str, Any] | None = None,
        git_context: dict[str, Any] | None = None,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
        executor_metadata: dict[str, Any] | None = None,
    ) -> PolicyInput:
        safety = safety or {}
        git_context = git_context or self._git_context(safety)
        git_preflight = safety.get("git_preflight", {})
        file_scope_guard = safety.get("file_scope_guard", {})
        executor_contract = executor_metadata
        if executor_contract is None and command is not None:
            executor_contract = self.executor_registry.metadata_for(command.executor)
        command_payload: dict[str, Any] | None = None
        live_matches: list[str] = []
        if command is not None:
            live_matches = self._live_policy_matches(command.command)
            safety_classification = self._command_safety_classification(command)
            command_payload = {
                "name": command.name,
                "command": command.command,
                "prompt": command.prompt,
                "required": command.required,
                "timeout_seconds": command.timeout_seconds,
                "model": command.model,
                "sandbox": command.sandbox,
                "executor": command.executor,
                "requested_capabilities": list(command.requested_capabilities),
                "user_experience_gate": deepcopy(command.user_experience_gate),
                "spec_refs": list(command.spec_refs),
                "safety_classification": safety_classification,
            }
        roadmap_path = (
            str(self.roadmap_path.relative_to(self.project_root))
            if self.roadmap_path and self.roadmap_path.is_relative_to(self.project_root)
            else str(self.roadmap_path)
        )
        return PolicyInput(
            project={
                "name": str(self.roadmap.get("project", self.project_root.name)),
                "root": str(self.project_root),
                "profile": self.roadmap.get("profile"),
                "roadmap_path": roadmap_path,
            },
            task={
                "id": task.id,
                "title": task.title,
                "milestone_id": task.milestone_id,
                "milestone_title": task.milestone_title,
                "status": task.status,
                "manual_approval_required": task.manual_approval_required,
                "agent_approval_required": task.agent_approval_required,
                "max_task_iterations": task.max_task_iterations,
                "spec_refs": list(task.spec_refs),
            },
            phase=phase,
            command=command_payload,
            executor=executor_contract,
            git={
                "is_repository": git_context.get("is_repository", False),
                "root": git_context.get("root"),
                "branch": git_context.get("branch"),
                "head": git_context.get("head"),
                "short_head": git_context.get("short_head"),
                "refs": git_context.get("refs", {}),
            },
            worktree={
                "git_preflight_status": git_preflight.get("status", "unknown"),
                "git_preflight_message": git_preflight.get("message", ""),
                "file_scope_guard_status": file_scope_guard.get("status", "unknown"),
                "file_scope_guard_message": file_scope_guard.get("message", ""),
                "dirty_before_paths": git_preflight.get("dirty_before_paths", []),
                "dirty_after_paths": file_scope_guard.get("dirty_after_paths", []),
                "dirty_before_out_of_scope_paths": git_preflight.get("dirty_before_out_of_scope_paths", []),
                "new_dirty_paths": file_scope_guard.get("new_dirty_paths", []),
                "changed_preexisting_dirty_paths": file_scope_guard.get("changed_preexisting_dirty_paths", []),
                "new_or_changed_dirty_paths": file_scope_guard.get("new_or_changed_dirty_paths", []),
                "file_scope_violations": file_scope_guard.get("violations", []),
                "status_short": git_context.get("status_short", ""),
            },
            file_scope={
                "patterns": list(task.file_scope),
                "dirty_before_out_of_scope_paths": git_preflight.get("dirty_before_out_of_scope_paths", []),
                "violations": file_scope_guard.get("violations", []),
            },
            approvals={
                "allow_manual": allow_manual,
                "allow_agent": allow_agent,
                "manual_required": task.manual_approval_required,
                "agent_required": task.agent_approval_required,
                "executor_agent_required": bool((executor_contract or {}).get("requires_agent_approval")),
            },
            live={
                "allow_live": allow_live,
                "requires_live_flag_patterns": list(self.command_policy.get("requires_live_flag_patterns", [])),
                "matched_patterns": live_matches,
                "detected": bool(live_matches),
            },
            context={
                "policy_profile": self.command_policy.get("profile") or self.roadmap.get("profile"),
                "command_policy_version": self.command_policy.get("version"),
                "default_timeout_seconds": self.default_timeout,
            },
        )

    def _live_policy_matches(self, command: str | None) -> list[str]:
        if command is None:
            return []
        stripped = command.strip()
        return [
            str(pattern)
            for pattern in self.command_policy.get("requires_live_flag_patterns", [])
            if str(pattern) in stripped
        ]

    def _command_safety_classification(self, command: AcceptanceCommand) -> dict[str, Any]:
        command_text = command.command or ""
        matches_by_class: dict[str, list[dict[str, Any]]] = {}
        for class_name, patterns in COMMAND_UNSAFE_OPERATION_PATTERNS.items():
            for pattern_id, pattern in patterns:
                match = pattern.search(command_text)
                if match is None:
                    continue
                start = max(0, match.start() - 40)
                end = min(len(command_text), match.end() + 40)
                matches_by_class.setdefault(class_name, []).append(
                    {
                        "id": pattern_id,
                        "evidence": redact(command_text[start:end]),
                    }
                )

        sandbox_mode = str(command.sandbox or "workspace-write").strip()
        sandbox_key = sandbox_mode.lower()
        sandbox: dict[str, Any] = {
            "mode": sandbox_mode,
            "allowed_modes": sorted(SAFE_SANDBOX_MODES),
            "unsafe_modes": sorted(UNSAFE_SANDBOX_MODES),
            "classification": "workspace" if sandbox_key in SAFE_SANDBOX_MODES else "unsafe",
            "unsafe": False,
            "reason": "sandbox mode is locally constrained",
        }
        if sandbox_key in UNSAFE_SANDBOX_MODES:
            sandbox.update(
                {
                    "unsafe": True,
                    "reason": f"sandbox mode `{sandbox_mode}` disables local isolation",
                }
            )
        elif sandbox_key not in SAFE_SANDBOX_MODES:
            sandbox.update(
                {
                    "unsafe": True,
                    "reason": f"sandbox mode `{sandbox_mode}` is not in the local safety allowlist",
                }
            )
        if sandbox["unsafe"]:
            matches_by_class.setdefault("filesystem", []).append(
                {
                    "id": "unsafe_sandbox_mode",
                    "evidence": sandbox["reason"],
                }
            )

        unsafe_classes = sorted(matches_by_class)
        detected_capabilities: list[str] = []
        for class_name in unsafe_classes:
            detected_capabilities.extend(UNSAFE_CAPABILITY_CLASSES.get(class_name, ()))
        detected_capabilities = sorted(dict.fromkeys(detected_capabilities))
        requested_capabilities = list(command.requested_capabilities)
        return {
            "schema_version": COMMAND_SAFETY_CLASSIFICATION_SCHEMA_VERSION,
            "deny_by_default": True,
            "unsafe": bool(unsafe_classes),
            "unsafe_classes": unsafe_classes,
            "detected_capabilities": detected_capabilities,
            "requested_capabilities": requested_capabilities,
            "requested_capability_classifications": classify_capabilities(requested_capabilities),
            "matches": matches_by_class,
            "sandbox": sandbox,
        }

    def _command_policy_match(self, command: str | None, *, allow_live: bool = False) -> tuple[str, str, dict[str, Any]]:
        if command is None:
            return "denied", "shell command is missing", {}
        stripped = command.strip()
        for pattern in self.command_policy.get("blocked_patterns", []):
            if pattern in stripped:
                return "denied", f"blocked pattern matched: {pattern}", {"blocked_pattern": pattern}
        live_matches = self._live_policy_matches(command)
        if live_matches and not allow_live:
            return (
                "requires_approval",
                f"live command requires --allow-live: {live_matches[0]}",
                {"approval_flag": "--allow-live", "matched_live_patterns": live_matches},
            )
        prefixes = tuple(str(prefix) for prefix in self.command_policy.get("allowed_prefixes", []))
        if prefixes and not stripped.startswith(prefixes):
            return "denied", "command prefix is not allowlisted", {"allowed_prefixes": list(prefixes)}
        return "allowed", "allowed", {}

    def command_allowed(self, command: str | None, allow_live: bool = False) -> tuple[bool, str]:
        outcome, reason, _metadata = self._command_policy_match(command, allow_live=allow_live)
        return outcome == "allowed", reason

    def _approval_policy_decision(
        self,
        policy_input: PolicyInput,
        *,
        kind: str,
        required_key: str,
        allowed_key: str,
        label: str,
        approval_flag: str,
    ) -> PolicyDecision:
        required = bool(policy_input.approvals.get(required_key))
        allowed = bool(policy_input.approvals.get(allowed_key))
        if not required:
            return PolicyDecision(
                kind=kind,
                scope="task",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=f"{label} not required",
                policy_input=policy_input,
            )
        if allowed:
            return PolicyDecision(
                kind=kind,
                scope="task",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=f"{label} satisfied",
                policy_input=policy_input,
                approval_flag=approval_flag,
            )
        return PolicyDecision(
            kind=kind,
            scope="task",
            outcome="requires_approval",
            effect="requires_approval",
            severity="approval",
            reason=f"{label} required",
            policy_input=policy_input,
            requires_approval=True,
            approval_flag=approval_flag,
        )

    def _manual_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        return self._approval_policy_decision(
            policy_input,
            kind="manual_approval",
            required_key="manual_required",
            allowed_key="allow_manual",
            label="manual approval",
            approval_flag="--allow-manual",
        )

    def _agent_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        return self._approval_policy_decision(
            policy_input,
            kind="agent_approval",
            required_key="agent_required",
            allowed_key="allow_agent",
            label="agent approval",
            approval_flag="--allow-agent",
        )

    def _executor_policy_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        command = policy_input.command or {}
        executor = policy_input.executor or {}
        executor_id = str(command.get("executor") or executor.get("id") or "")
        if not executor_id or executor.get("kind") == "unknown":
            return PolicyDecision(
                kind="executor_policy",
                scope="command",
                outcome="denied",
                effect="deny",
                severity="error",
                reason=f"unknown executor: {executor_id}",
                policy_input=policy_input,
                status="unknown",
            )
        return PolicyDecision(
            kind="executor_policy",
            scope="command",
            outcome="allowed",
            effect="allow",
            severity="info",
            reason="registered executor",
            policy_input=policy_input,
        )

    def _capability_policy_decision(self, policy_input: PolicyInput) -> PolicyDecision | None:
        command = policy_input.command or {}
        requested = self._normalize_requested_capabilities(command.get("requested_capabilities"))
        safety_classification = (
            command.get("safety_classification")
            if isinstance(command.get("safety_classification"), dict)
            else {}
        )
        detected = self._normalize_requested_capabilities(safety_classification.get("detected_capabilities"))
        executor = policy_input.executor or {}
        executor_id = str(command.get("executor") or executor.get("id") or "")
        command_policy_blocks_detected = False
        command_policy_metadata: dict[str, Any] = {}
        if detected and not requested and executor.get("uses_command_policy"):
            command_outcome, command_reason, command_metadata = self._command_policy_match(
                command.get("command"),
                allow_live=bool(policy_input.live.get("allow_live")),
            )
            if command_outcome == "denied":
                command_policy_blocks_detected = True
                command_policy_metadata = {
                    "outcome": command_outcome,
                    "reason": command_reason,
                    **command_metadata,
                }
        executor_capabilities = [
            str(capability)
            for capability in executor.get("capabilities", [])
            if str(capability).strip()
        ]
        executor_capability_set = set(executor_capabilities)
        executor_unsafe = [
            capability
            for capability in executor_capabilities
            if capability in UNSAFE_EXECUTOR_CAPABILITIES
        ]
        executor_unsafe_classes = capability_core_classes(executor_unsafe)
        effective_requested = tuple(dict.fromkeys([*requested, *detected]))
        unsafe = [capability for capability in effective_requested if capability in UNSAFE_EXECUTOR_CAPABILITIES]
        unsafe_classes = sorted(
            dict.fromkeys(
                [
                    *[
                        str(class_name)
                        for class_name in safety_classification.get("unsafe_classes", [])
                        if str(class_name).strip()
                    ],
                    *capability_core_classes(unsafe),
                ]
            )
        )
        unsupported = [
            capability
            for capability in requested
            if capability not in executor_capability_set and capability not in unsafe
        ]
        metadata = {
            "schema_version": CAPABILITY_POLICY_SCHEMA_VERSION,
            "requested_capabilities": list(requested),
            "detected_capabilities": list(detected),
            "effective_requested_capabilities": list(effective_requested),
            "executor_capabilities": executor_capabilities,
            "unsupported_capabilities": unsupported,
            "unsafe_capabilities": unsafe,
            "unsafe_classes": unsafe_classes,
            "executor_unsafe_capabilities": executor_unsafe,
            "executor_unsafe_classes": executor_unsafe_classes,
            "operation_classification": safety_classification,
            "command_policy_blocked_detected_capabilities": command_policy_blocks_detected,
            "command_policy_block": command_policy_metadata,
            "unsafe_capability_classifications": classify_capabilities(unsafe),
            "effective_requested_capability_classifications": classify_capabilities(effective_requested),
            "executor_capability_classifications": executor.get("capability_classifications")
            if isinstance(executor.get("capability_classifications"), dict)
            else classify_capabilities(executor_capabilities),
            "known_capabilities": sorted(self._known_capability_names()),
        }
        if not requested and not detected and executor_unsafe:
            return PolicyDecision(
                kind="capability_policy",
                scope="command",
                outcome="warning",
                effect="warn",
                severity="warning",
                reason=(
                    f"executor `{executor_id}` declares unsafe capabilities that require explicit "
                    f"executor configuration: {', '.join(executor_unsafe)}"
                ),
                policy_input=policy_input,
                metadata=metadata,
            )
        if not requested and not detected:
            return None
        if unsafe:
            if command_policy_blocks_detected:
                return PolicyDecision(
                    kind="capability_policy",
                    scope="command",
                    outcome="warning",
                    effect="warn",
                    severity="warning",
                    reason=(
                        "unsafe detected command behavior is also denied by command policy: "
                        f"{', '.join(unsafe)}"
                    ),
                    policy_input=policy_input,
                    metadata=metadata,
                )
            source = "detected command behavior" if detected else "requested executor capabilities"
            return PolicyDecision(
                kind="capability_policy",
                scope="command",
                outcome="denied",
                effect="deny",
                severity="error",
                reason=(
                    f"unsafe {source} is denied by default and is not locally approvable: "
                    f"{', '.join(unsafe)}"
                ),
                policy_input=policy_input,
                metadata=metadata,
            )
        if unsupported:
            return PolicyDecision(
                kind="capability_policy",
                scope="command",
                outcome="denied",
                effect="deny",
                severity="error",
                reason=f"executor `{executor_id}` does not support requested capabilities: {', '.join(unsupported)}",
                policy_input=policy_input,
                metadata=metadata,
            )
        return PolicyDecision(
            kind="capability_policy",
            scope="command",
            outcome="allowed",
            effect="allow",
            severity="info",
            reason="requested executor capabilities are supported",
            policy_input=policy_input,
            metadata=metadata,
        )

    def _executor_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        command = policy_input.command or {}
        executor = policy_input.executor or {}
        executor_id = str(command.get("executor") or executor.get("id") or "")
        if not executor_id or executor.get("kind") == "unknown":
            return PolicyDecision(
                kind="executor_approval",
                scope="command",
                outcome="warning",
                effect="warn",
                severity="warning",
                reason=f"executor approval not evaluated for unknown executor: {executor_id}",
                policy_input=policy_input,
                status="unknown",
            )
        if executor.get("requires_agent_approval"):
            if not policy_input.approvals.get("allow_agent"):
                return PolicyDecision(
                    kind="executor_approval",
                    scope="command",
                    outcome="requires_approval",
                    effect="requires_approval",
                    severity="approval",
                    reason=f"{executor_id} executor requires --allow-agent",
                    policy_input=policy_input,
                    requires_approval=True,
                    approval_flag="--allow-agent",
                )
            return PolicyDecision(
                kind="executor_approval",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason="agent approval satisfied",
                policy_input=policy_input,
                approval_flag="--allow-agent",
            )
        return PolicyDecision(
            kind="executor_approval",
            scope="command",
            outcome="allowed",
            effect="allow",
            severity="info",
            reason="executor approval not required",
            policy_input=policy_input,
        )

    def _command_policy_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        command = policy_input.command or {}
        outcome, reason, metadata = self._command_policy_match(
            command.get("command"),
            allow_live=bool(policy_input.live.get("allow_live")),
        )
        if outcome == "allowed":
            return PolicyDecision(
                kind="command_policy",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=reason,
                policy_input=policy_input,
            )
        if outcome == "requires_approval":
            return PolicyDecision(
                kind="command_policy",
                scope="command",
                outcome="requires_approval",
                effect="requires_approval",
                severity="approval",
                reason=reason,
                policy_input=policy_input,
                requires_approval=True,
                approval_flag=str(metadata.get("approval_flag", "--allow-live")),
                metadata=metadata,
            )
        return PolicyDecision(
            kind="command_policy",
            scope="command",
            outcome="denied",
            effect="deny",
            severity="error",
            reason=reason,
            policy_input=policy_input,
            metadata=metadata,
        )

    def _live_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        live = policy_input.live or {}
        matches = list(live.get("matched_patterns", []))
        if not live.get("detected"):
            return PolicyDecision(
                kind="live_approval",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason="live approval not required",
                policy_input=policy_input,
            )
        metadata = {"matched_live_patterns": matches}
        if live.get("allow_live"):
            return PolicyDecision(
                kind="live_approval",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason="live approval satisfied",
                policy_input=policy_input,
                approval_flag="--allow-live",
                metadata=metadata,
            )
        return PolicyDecision(
            kind="live_approval",
            scope="command",
            outcome="requires_approval",
            effect="requires_approval",
            severity="approval",
            reason="live command requires --allow-live",
            policy_input=policy_input,
            requires_approval=True,
            approval_flag="--allow-live",
            metadata=metadata,
        )

    def _git_preflight_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        status = str(policy_input.worktree.get("git_preflight_status", "unknown"))
        reason = str(policy_input.worktree.get("git_preflight_message", ""))
        if status == "clean":
            return PolicyDecision(
                kind="git_preflight",
                scope="worktree",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=reason,
                policy_input=policy_input,
                status=status,
            )
        return PolicyDecision(
            kind="git_preflight",
            scope="worktree",
            outcome="warning",
            effect="warn",
            severity="warning",
            reason=reason,
            policy_input=policy_input,
            status=status,
            metadata={
                "dirty_before_paths": policy_input.worktree.get("dirty_before_paths", []),
                "dirty_before_out_of_scope_paths": policy_input.worktree.get("dirty_before_out_of_scope_paths", []),
            },
        )

    def _file_scope_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        status = str(policy_input.worktree.get("file_scope_guard_status", "unknown"))
        reason = str(policy_input.worktree.get("file_scope_guard_message", ""))
        if status == "failed":
            return PolicyDecision(
                kind="file_scope_guard",
                scope="worktree",
                outcome="denied",
                effect="deny",
                severity="error",
                reason=reason,
                policy_input=policy_input,
                status=status,
                metadata={"violations": policy_input.file_scope.get("violations", [])},
            )
        if status == "passed":
            return PolicyDecision(
                kind="file_scope_guard",
                scope="worktree",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=reason,
                policy_input=policy_input,
                status=status,
            )
        return PolicyDecision(
            kind="file_scope_guard",
            scope="worktree",
            outcome="warning",
            effect="warn",
            severity="warning",
            reason=reason,
            policy_input=policy_input,
            status=status,
        )

    def _git_status_entries(self) -> list[dict[str, Any]]:
        if not self._is_git_repo():
            return []
        result = self._git(["status", "--porcelain", "--untracked-files=all"])
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
            normalized = self._normalize_repo_path(path)
            if normalized:
                index_status = status[0]
                worktree_status = status[1]
                is_untracked = status == "??"
                is_staged = not is_untracked and index_status not in {" ", "?"}
                is_deleted = index_status == "D" or worktree_status == "D"
                is_modified = (
                    index_status in {"M", "A", "R", "C", "T"}
                    or worktree_status in {"M", "T"}
                )
                states = []
                if is_staged:
                    states.append("staged")
                if is_modified:
                    states.append("modified")
                if is_deleted:
                    states.append("deleted")
                if is_untracked:
                    states.append("untracked")
                entries.append(
                    {
                        "path": normalized,
                        "status": status,
                        "states": states,
                        "staged": is_staged,
                        "modified": is_modified,
                        "deleted": is_deleted,
                        "untracked": is_untracked,
                    }
                )
        entries.sort(key=lambda item: str(item["path"]))
        return entries

    def _git_status_paths(self) -> list[str]:
        return sorted(dict.fromkeys(str(entry["path"]) for entry in self._git_status_entries()))

    def _materialization_dirty_paths(self, paths: list[str] | set[str] | tuple[str, ...]) -> list[str]:
        materialization_paths = set(self._roadmap_materialization_paths())
        return sorted(
            dict.fromkeys(
                self._normalize_repo_path(str(path))
                for path in paths
                if str(path).strip() and self._normalize_repo_path(str(path)) in materialization_paths
            )
        )

    def _non_materialization_dirty_paths(self, paths: list[str] | set[str] | tuple[str, ...]) -> list[str]:
        materialization_paths = set(self._roadmap_materialization_paths())
        return sorted(
            dict.fromkeys(
                self._normalize_repo_path(str(path))
                for path in paths
                if str(path).strip() and self._normalize_repo_path(str(path)) not in materialization_paths
            )
        )

    def checkpoint_readiness(self, task: HarnessTask | None = None) -> dict[str, Any]:
        base = {
            "schema_version": CHECKPOINT_READINESS_SCHEMA_VERSION,
            "kind": "engineering-harness.checkpoint-readiness",
            "is_repository": self._is_git_repo(),
            "ready": False,
            "blocking": False,
            "reason": "not_git_repository",
            "dirty_paths": [],
            "blocking_paths": [],
            "safe_to_checkpoint_paths": [],
            "recommended_action": (
                "Initialize a local git repository if unattended git checkpoints are required."
            ),
            "materialization_paths": self._roadmap_materialization_paths(),
            "task": self.task_payload(task) if task is not None else None,
            "dirty_path_states": [],
            "classifications": {
                "harness_materialization": [],
                "task_scope": [],
                "unrelated_user": [],
            },
        }
        if not base["is_repository"]:
            return base

        status_result = self._git(["status", "--porcelain", "--untracked-files=all"])
        if status_result["returncode"] != 0:
            return {
                **base,
                "is_repository": True,
                "blocking": True,
                "reason": "git_status_failed",
                "recommended_action": "Resolve the local git status error before unattended checkpointing.",
                "stderr": status_result["stderr"],
            }

        entries = self._git_status_entries()
        dirty_paths = [str(entry["path"]) for entry in entries]
        materialization_paths = set(base["materialization_paths"])
        task_scope = task.file_scope if task is not None else ()
        classifications: dict[str, list[str]] = {
            "harness_materialization": [],
            "task_scope": [],
            "unrelated_user": [],
        }
        path_states: list[dict[str, Any]] = []
        for entry in entries:
            path = str(entry["path"])
            if path in materialization_paths:
                classification = "harness_materialization"
            elif task is not None and self._path_in_scope(path, task_scope):
                classification = "task_scope"
            else:
                classification = "unrelated_user"
            classifications[classification].append(path)
            path_states.append({**entry, "classification": classification})

        safe_paths = sorted(
            dict.fromkeys(
                [
                    *classifications["harness_materialization"],
                    *classifications["task_scope"],
                ]
            )
        )
        blocking_paths = sorted(dict.fromkeys(classifications["unrelated_user"]))
        payload = {
            **base,
            "is_repository": True,
            "dirty_paths": dirty_paths,
            "blocking_paths": blocking_paths,
            "safe_to_checkpoint_paths": safe_paths,
            "dirty_path_states": path_states,
            "classifications": {key: sorted(dict.fromkeys(value)) for key, value in classifications.items()},
        }
        if not dirty_paths:
            payload.update(
                {
                    "ready": True,
                    "blocking": False,
                    "reason": "clean",
                    "recommended_action": (
                        "Run the unattended drive or workspace dispatch normally; no local git "
                        "checkpoint blockers are present."
                    ),
                }
            )
            return payload
        if blocking_paths:
            reason = "mixed_unrelated_user_dirty" if safe_paths else "unrelated_user_dirty"
            payload.update(
                {
                    "ready": False,
                    "blocking": True,
                    "reason": reason,
                    "recommended_action": (
                        "Review, commit, stash, or move the blocking user paths yourself, then rerun "
                        "status or workspace dispatch. The harness will not commit or clean them."
                    ),
                }
            )
            return payload
        if classifications["harness_materialization"] and not classifications["task_scope"]:
            reason = "harness_materialization_dirty"
            action = (
                "Checkpoint the harness-owned roadmap/materialization paths locally or let the "
                "rolling materialization checkpoint handle them before dispatching unrelated work."
            )
        elif classifications["task_scope"] and not classifications["harness_materialization"]:
            reason = "task_scope_dirty"
            task_id = task.id if task is not None else "the current task"
            action = (
                f"Review and checkpoint the in-scope task paths for `{task_id}` before starting "
                "unrelated work."
            )
        else:
            reason = "checkpointable_dirty_paths"
            action = "Checkpoint the listed safe paths before dispatching unrelated work."
        payload.update(
            {
                "ready": True,
                "blocking": False,
                "reason": reason,
                "recommended_action": action,
            }
        )
        return payload

    def _normalize_repo_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _path_in_scope(self, path: str, scopes: tuple[str, ...]) -> bool:
        normalized = self._normalize_repo_path(path)
        normalized_scopes = tuple(self._normalize_repo_path(scope) for scope in scopes if str(scope).strip())
        if not normalized_scopes or any(scope in {"**", "**/*"} for scope in normalized_scopes):
            return True
        for scope in normalized_scopes:
            if scope.endswith("/**"):
                prefix = scope[:-3].rstrip("/")
                if normalized == prefix or normalized.startswith(f"{prefix}/"):
                    return True
            if fnmatch.fnmatchcase(normalized, scope):
                return True
            try:
                if PurePosixPath(normalized).match(scope):
                    return True
            except ValueError:
                continue
        return False

    def _scope_violations(self, paths: list[str] | set[str], scopes: tuple[str, ...]) -> list[str]:
        return sorted(path for path in paths if not self._path_in_scope(path, scopes))

    def _git_safety_preflight(self, task: HarnessTask) -> dict[str, Any]:
        if not self._is_git_repo():
            return {
                "status": "skipped",
                "message": "project root is not inside a git repository",
                "dirty_before_paths": [],
                "dirty_before_fingerprints": {},
                "dirty_before_out_of_scope_paths": [],
            }
        dirty_before = self._git_status_paths()
        out_of_scope = self._scope_violations(dirty_before, task.file_scope)
        return {
            "status": "dirty" if dirty_before else "clean",
            "message": "worktree has pre-existing changes" if dirty_before else "worktree is clean",
            "dirty_before_paths": dirty_before,
            "dirty_before_fingerprints": self._file_fingerprints(dirty_before),
            "dirty_before_out_of_scope_paths": out_of_scope,
        }

    def _file_fingerprints(self, paths: list[str] | set[str]) -> dict[str, str]:
        fingerprints: dict[str, str] = {}
        for path in paths:
            normalized = self._normalize_repo_path(path)
            file_path = self.project_root / normalized
            if not file_path.exists():
                fingerprints[normalized] = "<missing>"
                continue
            if file_path.is_dir():
                fingerprints[normalized] = "<directory>"
                continue
            try:
                fingerprints[normalized] = hashlib.sha256(file_path.read_bytes()).hexdigest()
            except OSError:
                fingerprints[normalized] = "<unreadable>"
        return fingerprints

    def _file_scope_guard(
        self,
        task: HarnessTask,
        *,
        dirty_before_paths: list[str],
        dirty_before_fingerprints: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self._is_git_repo():
            return {
                "status": "skipped",
                "message": "project root is not inside a git repository",
                "dirty_after_paths": [],
                "new_dirty_paths": [],
                "changed_preexisting_dirty_paths": [],
                "new_or_changed_dirty_paths": [],
                "violations": [],
            }
        dirty_before = set(dirty_before_paths)
        dirty_after = set(self._git_status_paths())
        after_fingerprints = self._file_fingerprints(dirty_after)
        new_dirty = sorted(dirty_after - dirty_before)
        before_fingerprints = dirty_before_fingerprints or {}
        changed_preexisting = sorted(
            path
            for path in dirty_after & dirty_before
            if after_fingerprints.get(path) != before_fingerprints.get(path)
        )
        new_or_changed = sorted(dict.fromkeys([*new_dirty, *changed_preexisting]))
        violations = self._scope_violations(new_or_changed, task.file_scope)
        status = "passed" if not violations else "failed"
        message = (
            "new or changed task paths are inside file_scope"
            if not violations
            else f"new or changed task paths outside file_scope: {', '.join(violations[:8])}"
        )
        return {
            "status": status,
            "message": message,
            "dirty_after_paths": sorted(dirty_after),
            "new_dirty_paths": new_dirty,
            "changed_preexisting_dirty_paths": changed_preexisting,
            "new_or_changed_dirty_paths": new_or_changed,
            "violations": violations,
        }

    def _roadmap_materialization_paths(self) -> list[str]:
        paths: list[str] = []
        if self.roadmap_path and self.roadmap_path.is_relative_to(self.project_root):
            paths.append(str(self.roadmap_path.relative_to(self.project_root)))
        return sorted(dict.fromkeys(self._normalize_repo_path(path) for path in paths if str(path).strip()))

    def roadmap_materialization_checkpoint_intent(
        self,
        *,
        reason: str,
        push: bool = False,
        remote: str = "origin",
        branch: str | None = None,
    ) -> dict[str, Any]:
        is_repository = self._is_git_repo()
        dirty_before = self._git_status_paths() if is_repository else []
        checkpoint_readiness = self.checkpoint_readiness()
        blocking_paths = (
            list(checkpoint_readiness.get("blocking_paths", []))
            if isinstance(checkpoint_readiness, dict)
            else []
        )
        safe_paths = (
            list(checkpoint_readiness.get("safe_to_checkpoint_paths", []))
            if isinstance(checkpoint_readiness, dict)
            else []
        )
        intent = {
            "schema_version": MATERIALIZATION_CHECKPOINT_SCHEMA_VERSION,
            "kind": "roadmap_materialization_checkpoint",
            "status": "pending" if is_repository else "skipped",
            "message": (
                "roadmap materialization checkpoint requested"
                if is_repository
                else "project root is not inside a git repository"
            ),
            "reason": reason,
            "push": push,
            "remote": remote,
            "branch": branch,
            "is_repository": is_repository,
            "dirty_before_paths": dirty_before,
            "dirty_before_harness_paths": self._materialization_dirty_paths(dirty_before),
            "dirty_before_blocking_paths": sorted(
                dict.fromkeys(str(path) for path in blocking_paths if str(path).strip())
            ),
            "safe_to_checkpoint_paths": sorted(
                dict.fromkeys(str(path) for path in safe_paths if str(path).strip())
            ),
            "materialization_paths": self._roadmap_materialization_paths(),
            "checkpoint_readiness": checkpoint_readiness,
        }
        append_jsonl(
            self.decision_log_path,
            {
                "at": utc_now(),
                "event": "roadmap_materialization_checkpoint_intent",
                **intent,
            },
        )
        return intent

    def git_checkpoint_roadmap_materialization(
        self,
        intent: dict[str, Any],
        continuation: dict[str, Any],
        *,
        push: bool = False,
        remote: str = "origin",
        branch: str | None = None,
    ) -> dict[str, Any]:
        materialization_paths = sorted(
            dict.fromkeys(
                self._normalize_repo_path(path)
                for path in intent.get("materialization_paths", self._roadmap_materialization_paths())
                if str(path).strip()
            )
        )
        dirty_before = sorted(
            dict.fromkeys(
                self._normalize_repo_path(path)
                for path in intent.get("dirty_before_paths", [])
                if str(path).strip()
            )
        )
        intent_readiness = (
            deepcopy(intent.get("checkpoint_readiness"))
            if isinstance(intent.get("checkpoint_readiness"), dict)
            else self.checkpoint_readiness()
        )
        dirty_before_harness = self._materialization_dirty_paths(dirty_before)
        dirty_before_non_harness = self._non_materialization_dirty_paths(dirty_before)
        dirty_before_blocking = sorted(
            dict.fromkeys(
                [
                    *dirty_before_non_harness,
                    *[
                        self._normalize_repo_path(str(path))
                        for path in intent_readiness.get("blocking_paths", [])
                        if str(path).strip()
                    ],
                ]
            )
        )
        payload_base = {
            "schema_version": MATERIALIZATION_CHECKPOINT_SCHEMA_VERSION,
            "kind": "roadmap_materialization_checkpoint",
            "intent_status": intent.get("status"),
            "reason": intent.get("reason"),
            "push": push,
            "remote": remote,
            "branch": branch,
            "materialization_paths": materialization_paths,
            "dirty_before_paths": dirty_before,
            "dirty_before_harness_paths": dirty_before_harness,
            "dirty_before_blocking_paths": dirty_before_blocking,
            "checkpoint_readiness": intent_readiness,
            "continuation_status": continuation.get("status"),
            "milestones_added": continuation.get("milestones_added", []),
            "tasks_added": continuation.get("tasks_added", 0),
        }

        def finish(payload: dict[str, Any]) -> dict[str, Any]:
            result = {**payload_base, **payload}
            append_jsonl(
                self.decision_log_path,
                {
                    "at": utc_now(),
                    "event": "roadmap_materialization_checkpoint",
                    **result,
                },
            )
            return result

        if continuation.get("status") != "advanced":
            return finish({
                "status": "skipped",
                "message": "no roadmap materialization was produced",
            })
        if not self._is_git_repo():
            return finish({
                "status": "skipped",
                "message": "project root is not inside a git repository",
            })
        if dirty_before_blocking:
            return finish({
                "status": "deferred",
                "reason": "preexisting_unrelated_dirty_paths",
                "message": (
                    "roadmap materialization checkpoint deferred because unrelated dirty paths "
                    "existed before materialization"
                ),
                "unrelated_dirty_paths": dirty_before_blocking,
            })
        if not materialization_paths:
            return finish({
                "status": "skipped",
                "message": "no roadmap materialization path is inside the project root",
            })

        current_paths = self._git_status_paths()
        allowed_paths = set(materialization_paths)
        materialization_dirty = sorted(path for path in current_paths if path in allowed_paths)
        unrelated_dirty = sorted(path for path in current_paths if path not in allowed_paths)
        if unrelated_dirty:
            return finish({
                "status": "deferred",
                "reason": "unrelated_dirty_paths",
                "message": (
                    "roadmap materialization checkpoint deferred because unrelated dirty paths "
                    "appeared before generated tasks"
                ),
                "unrelated_dirty_paths": unrelated_dirty,
                "dirty_after_paths": current_paths,
            })
        if not materialization_dirty:
            return finish({
                "status": "skipped",
                "message": "no roadmap materialization changes to commit",
                "dirty_after_paths": current_paths,
            })

        add_result = self._git(["add", "-A", "--", *materialization_paths])
        if add_result["returncode"] != 0:
            return finish({
                "status": "failed",
                "reason": "git_add_failed",
                "message": "git add failed for roadmap materialization checkpoint",
                "stderr": add_result["stderr"],
                "dirty_after_paths": current_paths,
            })

        staged = self._git(["diff", "--cached", "--quiet", "--", *materialization_paths])
        if staged["returncode"] == 0:
            return finish({
                "status": "skipped",
                "message": "no staged roadmap materialization changes to commit",
                "dirty_after_paths": current_paths,
            })
        if staged["returncode"] not in (0, 1):
            return finish({
                "status": "failed",
                "reason": "staged_diff_failed",
                "message": "could not inspect staged roadmap materialization diff",
                "stderr": staged["stderr"],
                "dirty_after_paths": current_paths,
            })

        milestone_ids = [
            str(item.get("id"))
            for item in continuation.get("milestones_added", [])
            if isinstance(item, dict) and item.get("id")
        ]
        suffix = f": {', '.join(milestone_ids)}" if milestone_ids else ""
        message = f"chore(engineering): materialize roadmap continuation{suffix}"
        commit_result = self._git(["commit", "-m", message])
        if commit_result["returncode"] != 0:
            return finish({
                "status": "failed",
                "reason": "git_commit_failed",
                "message": "git commit failed for roadmap materialization checkpoint",
                "stderr": commit_result["stderr"],
                "dirty_after_paths": self._git_status_paths(),
            })

        commit_sha = self._git(["rev-parse", "HEAD"])
        result: dict[str, Any] = {
            "status": "committed",
            "message": message,
            "commit": commit_sha["stdout"].strip() if commit_sha["returncode"] == 0 else None,
            "checkpointed_paths": materialization_dirty,
            "dirty_after_paths": self._git_status_paths(),
        }

        if push:
            target_branch = branch or self._current_branch()
            if not target_branch:
                result.update({
                    "status": "failed",
                    "reason": "push_branch_unresolved",
                    "push_status": "failed",
                    "stderr": "could not resolve current branch",
                })
                return finish(result)
            push_result = self._git(["push", remote, f"HEAD:{target_branch}"])
            result["push_status"] = "pushed" if push_result["returncode"] == 0 else "failed"
            result["push_remote"] = remote
            result["push_branch"] = target_branch
            result["push_stdout"] = push_result["stdout"]
            result["push_stderr"] = push_result["stderr"]
            if push_result["returncode"] != 0:
                result["status"] = "failed"
                result["reason"] = "git_push_failed"
        return finish(result)

    def defer_roadmap_materialization_checkpoint(
        self,
        intent: dict[str, Any],
        *,
        reason: str,
        message: str,
    ) -> dict[str, Any]:
        materialization_paths = sorted(
            dict.fromkeys(
                self._normalize_repo_path(str(path))
                for path in intent.get("materialization_paths", self._roadmap_materialization_paths())
                if str(path).strip()
            )
        )
        dirty_before = sorted(
            dict.fromkeys(
                self._normalize_repo_path(str(path))
                for path in intent.get("dirty_before_paths", [])
                if str(path).strip()
            )
        )
        readiness = (
            deepcopy(intent.get("checkpoint_readiness"))
            if isinstance(intent.get("checkpoint_readiness"), dict)
            else self.checkpoint_readiness()
        )
        blocking_paths = sorted(
            dict.fromkeys(
                self._normalize_repo_path(str(path))
                for path in readiness.get("blocking_paths", [])
                if str(path).strip()
            )
        )
        result = {
            "schema_version": MATERIALIZATION_CHECKPOINT_SCHEMA_VERSION,
            "kind": "roadmap_materialization_checkpoint",
            "intent_status": intent.get("status"),
            "status": "deferred",
            "reason": reason,
            "message": message,
            "push": bool(intent.get("push", False)),
            "remote": intent.get("remote"),
            "branch": intent.get("branch"),
            "materialization_paths": materialization_paths,
            "dirty_before_paths": dirty_before,
            "dirty_before_harness_paths": self._materialization_dirty_paths(dirty_before),
            "dirty_before_blocking_paths": blocking_paths or self._non_materialization_dirty_paths(dirty_before),
            "unrelated_dirty_paths": blocking_paths or self._non_materialization_dirty_paths(dirty_before),
            "checkpoint_readiness": readiness,
            "continuation_status": "not_materialized",
            "milestones_added": [],
            "tasks_added": 0,
            "dirty_after_paths": self._git_status_paths() if self._is_git_repo() else [],
        }
        append_jsonl(
            self.decision_log_path,
            {
                "at": utc_now(),
                "event": "roadmap_materialization_checkpoint",
                **result,
            },
        )
        return result

    def defer_git_checkpoint(
        self,
        task: HarnessTask,
        *,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state()
        checkpoint_metadata = {
            "defer_message": reason,
            "deferred_by": "roadmap_materialization_checkpoint",
            **(metadata or {}),
        }
        self._record_phase_state(
            state,
            task,
            phase="checkpoint-intent",
            event="before",
            status="running",
            persist=True,
            metadata=checkpoint_metadata,
        )
        payload = {
            "status": "deferred",
            "reason": "roadmap_materialization_checkpoint_deferred",
            "message": reason,
            **checkpoint_metadata,
        }
        self._record_phase_state(
            state,
            task,
            phase="checkpoint-intent",
            event="after",
            status="deferred",
            message=reason,
            persist=True,
            metadata=payload,
        )
        return payload

    def run_task(
        self,
        task: HarnessTask,
        *,
        dry_run: bool = False,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
    ) -> dict[str, Any]:
        started_at = utc_now()
        report_path = self._new_task_report_path(task)
        state = self.load_state()
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        task_state["attempts"] = int(task_state.get("attempts", 0)) + (0 if dry_run else 1)
        task_state["last_started_at"] = started_at
        task_state["last_dry_run"] = dry_run
        if not dry_run:
            self._heartbeat_drive_control_in_state(
                state,
                activity="task-execution",
                message=f"starting task {task.id}",
                task=task,
            )
            self.save_state(state)
        safety: dict[str, Any] = {
            "git_preflight": self._git_safety_preflight(task),
            "file_scope_guard": {
                "status": "not_run",
                "message": "task did not reach post-run scope guard",
                "dirty_after_paths": [],
                "new_dirty_paths": [],
                "violations": [],
            },
        }
        task_state["last_dirty_before_paths"] = safety["git_preflight"].get("dirty_before_paths", [])
        effective_allow_manual = allow_manual or self._approval_is_approved(
            task,
            decision_kind="manual_approval",
            phase="task",
            state=state,
            mutate_stale=not dry_run,
        )
        effective_allow_agent = allow_agent or self._approval_is_approved(
            task,
            decision_kind="agent_approval",
            phase="task",
            state=state,
            mutate_stale=not dry_run,
        )

        manual_decision = self._manual_approval_decision(
            self._policy_input(
                task,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        )
        if manual_decision.blocks_execution():
            return self._finish_task(
                state,
                task,
                report_path,
                started_at,
                [],
                "blocked",
                "manual approval required",
                not dry_run,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        agent_decision = self._agent_approval_decision(
            self._policy_input(
                task,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        )
        if agent_decision.blocks_execution():
            return self._finish_task(
                state,
                task,
                report_path,
                started_at,
                [],
                "blocked",
                "agent implementation requires --allow-agent",
                not dry_run,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        if not task.acceptance:
            return self._finish_task(
                state,
                task,
                report_path,
                started_at,
                [],
                "blocked",
                "task has no acceptance",
                not dry_run,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )

        runs: list[CommandRun] = []
        replay_guard = self._new_replay_guard_summary(task)
        implementation_replay = None
        if not dry_run:
            implementation_replay = self._phase_replay_guard_decision(
                state,
                task,
                phase="implementation",
                commands=task.implementation,
            )
            self._append_replay_guard_decision(replay_guard, implementation_replay)
        if implementation_replay is not None and implementation_replay.get("action") == "reuse":
            implementation_status, message = self._reuse_command_group_from_replay_guard(
                state,
                task,
                phase="implementation",
                commands=task.implementation,
                runs=runs,
                decision=implementation_replay,
                persist_state=not dry_run,
            )
        else:
            implementation_status, message = self._run_command_group(
                task.implementation,
                phase="implementation",
                runs=runs,
                dry_run=dry_run,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
                task=task,
                state=state,
                persist_state=not dry_run,
            )
        overall_status = implementation_status

        if overall_status == "passed":
            acceptance_reused = False
            if not dry_run:
                for iteration in range(task.max_task_iterations, 0, -1):
                    acceptance_phase = f"acceptance-{iteration}"
                    acceptance_replay = self._phase_replay_guard_decision(
                        state,
                        task,
                        phase=acceptance_phase,
                        commands=task.acceptance,
                    )
                    if acceptance_replay.get("action") == "reuse":
                        self._append_replay_guard_decision(replay_guard, acceptance_replay)
                        acceptance_status, message = self._reuse_command_group_from_replay_guard(
                            state,
                            task,
                            phase=acceptance_phase,
                            commands=task.acceptance,
                            runs=runs,
                            decision=acceptance_replay,
                            persist_state=True,
                        )
                        overall_status = acceptance_status
                        if acceptance_status == "passed":
                            message = "All required acceptance commands passed by replay guard."
                        acceptance_reused = True
                        break
                    if iteration == 1:
                        self._append_replay_guard_decision(replay_guard, acceptance_replay)
            if not acceptance_reused:
                for iteration in range(task.max_task_iterations):
                    acceptance_status, message = self._run_command_group(
                        task.acceptance,
                        phase=f"acceptance-{iteration + 1}",
                        runs=runs,
                        dry_run=dry_run,
                        allow_live=allow_live,
                        allow_manual=effective_allow_manual,
                        allow_agent=effective_allow_agent,
                        task=task,
                        state=state,
                        persist_state=not dry_run,
                    )
                    overall_status = acceptance_status
                    if acceptance_status == "passed":
                        message = "All required acceptance commands passed."
                        break
                    if acceptance_status == "blocked" or iteration + 1 >= task.max_task_iterations or not task.repair:
                        break
                    repair_status, message = self._run_command_group(
                        task.repair,
                        phase=f"repair-{iteration + 1}",
                        runs=runs,
                        dry_run=dry_run,
                        allow_live=allow_live,
                        allow_manual=effective_allow_manual,
                        allow_agent=effective_allow_agent,
                        task=task,
                        state=state,
                        persist_state=not dry_run,
                    )
                    overall_status = repair_status
                    if repair_status != "passed":
                        break

        if overall_status == "passed" and task.e2e:
            e2e_status, message = self._run_command_group(
                task.e2e,
                phase="e2e",
                runs=runs,
                dry_run=dry_run,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
                task=task,
                state=state,
                persist_state=not dry_run,
            )
            overall_status = e2e_status
            if e2e_status == "passed":
                message = "All required acceptance and e2e commands passed."

        if not dry_run:
            self._record_phase_state(
                state,
                task,
                phase="file-scope-guard",
                event="before",
                status="running",
                persist=True,
                metadata={
                    "file_scope": list(task.file_scope),
                    "dirty_before_paths": list(safety["git_preflight"].get("dirty_before_paths", [])),
                },
            )
            safety["file_scope_guard"] = self._file_scope_guard(
                task,
                dirty_before_paths=list(safety["git_preflight"].get("dirty_before_paths", [])),
                dirty_before_fingerprints=dict(safety["git_preflight"].get("dirty_before_fingerprints", {})),
            )
            task_state["last_dirty_after_paths"] = safety["file_scope_guard"].get("dirty_after_paths", [])
            task_state["last_new_dirty_paths"] = safety["file_scope_guard"].get("new_dirty_paths", [])
            task_state["last_file_scope_violations"] = safety["file_scope_guard"].get("violations", [])
            file_scope_decision = self._file_scope_decision(
                self._policy_input(
                    task,
                    safety=safety,
                    allow_live=allow_live,
                    allow_manual=effective_allow_manual,
                    allow_agent=effective_allow_agent,
                )
            )
            if overall_status == "passed" and file_scope_decision.blocks_execution():
                overall_status = "failed"
                message = safety["file_scope_guard"]["message"]
            self._record_phase_state(
                state,
                task,
                phase="file-scope-guard",
                event="after",
                status=str(safety["file_scope_guard"].get("status", "unknown")),
                message=str(safety["file_scope_guard"].get("message", "")),
                persist=True,
                metadata={
                    "dirty_after_paths": safety["file_scope_guard"].get("dirty_after_paths", []),
                    "new_dirty_paths": safety["file_scope_guard"].get("new_dirty_paths", []),
                    "changed_preexisting_dirty_paths": safety["file_scope_guard"].get(
                        "changed_preexisting_dirty_paths",
                        [],
                    ),
                    "new_or_changed_dirty_paths": safety["file_scope_guard"].get("new_or_changed_dirty_paths", []),
                    "violations": safety["file_scope_guard"].get("violations", []),
                },
            )

        status = "dry-run" if dry_run and overall_status == "passed" else overall_status
        return self._finish_task(
            state,
            task,
            report_path,
            started_at,
            runs,
            status,
            message,
            not dry_run,
            safety=safety,
            allow_live=allow_live,
            allow_manual=effective_allow_manual,
            allow_agent=effective_allow_agent,
            replay_guard=self._finalize_replay_guard_summary(replay_guard),
        )

    def _run_command_group(
        self,
        commands: tuple[AcceptanceCommand, ...],
        *,
        phase: str,
        runs: list[CommandRun],
        dry_run: bool,
        allow_live: bool,
        allow_manual: bool,
        allow_agent: bool,
        task: HarnessTask,
        state: dict[str, Any] | None = None,
        persist_state: bool = False,
    ) -> tuple[str, str]:
        state_payload = state if state is not None else (self.load_state() if persist_state else {})
        self._record_phase_state(
            state_payload,
            task,
            phase=phase,
            event="before",
            status="running",
            persist=persist_state,
            metadata=self._command_group_state_metadata(commands),
        )
        run_start_index = len(runs)

        def finish(status: str, message: str) -> tuple[str, str]:
            phase_runs = runs[run_start_index:]
            self._record_phase_state(
                state_payload,
                task,
                phase=phase,
                event="after",
                status=status,
                message=message,
                persist=persist_state,
                metadata={
                    **self._command_group_state_metadata(commands),
                    "command_count": len(commands),
                    "run_count": len(phase_runs),
                    "required_failures": [
                        run.name
                        for run in phase_runs
                        if any(command.name == run.name and command.required for command in commands)
                        and (run.status == "blocked" or run.returncode != 0)
                    ],
                },
                runs=phase_runs,
            )
            return status, message

        if not commands:
            return finish("passed", f"No {phase} commands configured.")
        for command in commands:
            if persist_state:
                self._heartbeat_drive_control_in_state(
                    state_payload,
                    activity=f"{phase}:command",
                    message=f"starting {phase} command: {command.name}",
                    task=task,
                    phase=phase,
                )
                self.save_state(state_payload)
            approval_phase = self._approval_phase_key(phase)
            command_allow_live = allow_live or self._approval_is_approved(
                task,
                decision_kind="live_approval",
                phase=approval_phase,
                name=command.name,
                executor=command.executor,
                command=command,
                state=state_payload if persist_state else None,
                mutate_stale=persist_state,
            )
            command_allow_agent = allow_agent or self._approval_is_approved(
                task,
                decision_kind="executor_approval",
                phase=approval_phase,
                name=command.name,
                executor=command.executor,
                command=command,
                state=state_payload if persist_state else None,
                mutate_stale=persist_state,
            )
            executor_metadata = self.executor_registry.metadata_for(command.executor)
            policy_input = self._policy_input(
                task,
                phase=phase,
                command=command,
                allow_live=command_allow_live,
                allow_manual=allow_manual,
                allow_agent=command_allow_agent,
                executor_metadata=executor_metadata,
            )
            executor_decision = self._executor_policy_decision(policy_input)
            executor = self.executor_registry.get(command.executor)
            if executor_decision.blocks_execution():
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "blocked",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        executor_decision.reason,
                        executor=command.executor,
                        executor_metadata=executor_metadata,
                    )
                )
                return finish("blocked", executor_decision.reason)
            capability_decision = self._capability_policy_decision(policy_input)
            if capability_decision is not None and capability_decision.blocks_execution():
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "blocked",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        capability_decision.reason,
                        executor=command.executor,
                        executor_metadata=executor_metadata,
                    )
                )
                return finish("blocked", capability_decision.reason)
            executor_approval_decision = self._executor_approval_decision(policy_input)
            if executor_approval_decision.blocks_execution():
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "blocked",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        executor_approval_decision.reason,
                        executor=command.executor,
                        executor_metadata=executor_metadata,
                    )
                )
                return finish("blocked", executor_approval_decision.reason)
            if executor is None:
                return finish("blocked", executor_decision.reason)
            if executor.metadata.uses_command_policy:
                command_decision = self._command_policy_decision(policy_input)
                if command_decision.blocks_execution():
                    runs.append(
                        CommandRun(
                            phase,
                            command.name,
                            self._display_command(command, task),
                            "blocked",
                            None,
                            utc_now(),
                            utc_now(),
                            "",
                            command_decision.reason,
                            executor=command.executor,
                            executor_metadata=executor.metadata.as_contract(),
                        )
                    )
                    return finish("blocked", command_decision.reason)
            if dry_run:
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "dry-run",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        "",
                        executor=command.executor,
                        executor_metadata=executor.metadata.as_contract(),
                    )
                )
                continue
            run = self._run_command(command, phase=phase, task=task, state=state_payload, persist_state=persist_state)
            runs.append(run)
            if persist_state:
                self._heartbeat_drive_control_in_state(
                    state_payload,
                    activity=f"{phase}:command",
                    message=f"finished {phase} command {command.name}: {run.status}",
                    task=task,
                    phase=phase,
                )
                self.save_state(state_payload)
            if command.required and run.status == "blocked":
                return finish("blocked", run.stderr or f"Required {phase} command blocked: {command.name}")
            if command.required and run.status in EXECUTOR_WATCHDOG_FAILURE_STATUSES:
                return finish("failed", f"Required {phase} command {run.status}: {command.name}")
            if command.required and run.returncode != 0:
                if self._command_is_user_experience_gate(command) or self._run_has_user_experience_marker(run):
                    return finish("failed", f"Required user-experience gate failed: {command.name}")
                return finish("failed", f"Required {phase} command failed: {command.name}")
        return finish("passed", f"All required {phase} commands passed.")

    def _command_is_user_experience_gate(self, command: AcceptanceCommand) -> bool:
        gate = command.user_experience_gate if isinstance(command.user_experience_gate, dict) else {}
        if str(gate.get("kind") or "").strip() == BROWSER_USER_EXPERIENCE_GATE_KIND:
            return True
        command_text = str(command.command or "")
        return "engineering_harness.browser_e2e" in command_text

    def _run_has_user_experience_marker(self, run: CommandRun) -> bool:
        text = f"{run.stdout}\n{run.stderr}"
        return (
            BROWSER_USER_EXPERIENCE_FAILURE_MARKER in text
            or "BROWSER_USER_EXPERIENCE_GATE_FAILED" in text
        )

    def git_checkpoint(
        self,
        task: HarnessTask,
        *,
        push: bool = False,
        remote: str = "origin",
        branch: str | None = None,
        message_template: str = "chore(engineering): complete {task_id}",
    ) -> dict[str, Any]:
        state = self.load_state()
        self._record_phase_state(
            state,
            task,
            phase="checkpoint-intent",
            event="before",
            status="running",
            persist=True,
            metadata={
                "push": push,
                "remote": remote,
                "branch": branch,
                "message_template": message_template,
                "checkpoint_readiness": self.checkpoint_readiness(task),
            },
        )

        def finish(payload: dict[str, Any]) -> dict[str, Any]:
            self._record_phase_state(
                state,
                task,
                phase="checkpoint-intent",
                event="after",
                status=str(payload.get("status", "unknown")),
                message=str(payload.get("message", "")),
                persist=True,
                metadata={
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "stdout",
                        "stderr",
                        "push_stdout",
                        "push_stderr",
                    }
                },
            )
            return payload

        if not self._is_git_repo():
            return finish({"status": "skipped", "message": "project root is not inside a git repository"})

        task_state = state.get("tasks", {}).get(task.id, {})
        dirty_before = [str(path) for path in task_state.get("last_dirty_before_paths", [])]
        dirty_before_harness = self._materialization_dirty_paths(dirty_before)
        dirty_before_blocking = self._non_materialization_dirty_paths(dirty_before)
        checkpoint_readiness = self.checkpoint_readiness(task)
        if dirty_before_blocking:
            return finish({
                "status": "skipped",
                "message": "dirty worktree existed before the task; refusing to checkpoint mixed changes",
                "dirty_before_paths": dirty_before,
                "dirty_before_harness_paths": dirty_before_harness,
                "dirty_before_blocking_paths": dirty_before_blocking,
                "checkpoint_readiness": checkpoint_readiness,
            })

        status_before = self._git(["status", "--porcelain"])
        if status_before["returncode"] != 0:
            return finish({
                "status": "failed",
                "message": "could not inspect git status",
                "stderr": status_before["stderr"],
            })
        current_paths = self._git_status_paths()
        allowed_accumulated_paths = set(dirty_before_harness)
        scope_violations = sorted(
            path
            for path in current_paths
            if path not in allowed_accumulated_paths and not self._path_in_scope(path, task.file_scope)
        )
        if scope_violations:
            return finish({
                "status": "skipped",
                "message": "dirty files are outside task file_scope; refusing checkpoint",
                "violations": scope_violations,
                "dirty_before_paths": dirty_before,
                "dirty_before_harness_paths": dirty_before_harness,
                "checkpoint_readiness": checkpoint_readiness,
            })
        if not status_before["stdout"].strip():
            return finish({"status": "skipped", "message": "no git changes to commit"})

        checkpoint_paths = sorted(dict.fromkeys(current_paths))
        add_result = self._git(["add", "-A", "--", *checkpoint_paths])
        if add_result["returncode"] != 0:
            return finish({"status": "failed", "message": "git add failed", "stderr": add_result["stderr"]})

        staged = self._git(["diff", "--cached", "--quiet", "--", *checkpoint_paths])
        if staged["returncode"] == 0:
            return finish({"status": "skipped", "message": "no staged git changes to commit"})
        if staged["returncode"] not in (0, 1):
            return finish(
                {"status": "failed", "message": "could not inspect staged git diff", "stderr": staged["stderr"]}
            )

        message = message_template.format(
            task_id=task.id,
            task_title=task.title,
            milestone_id=task.milestone_id,
            milestone_title=task.milestone_title,
        )
        commit_result = self._git(["commit", "-m", message])
        if commit_result["returncode"] != 0:
            return finish({"status": "failed", "message": "git commit failed", "stderr": commit_result["stderr"]})

        commit_sha = self._git(["rev-parse", "HEAD"])
        payload: dict[str, Any] = {
            "status": "committed",
            "message": message,
            "commit": commit_sha["stdout"].strip() if commit_sha["returncode"] == 0 else None,
            "checkpointed_paths": checkpoint_paths,
            "dirty_before_paths": dirty_before,
            "dirty_before_harness_paths": dirty_before_harness,
            "checkpoint_readiness": checkpoint_readiness,
            "dirty_after_paths": self._git_status_paths(),
        }

        if push:
            target_branch = branch or self._current_branch()
            if not target_branch:
                payload.update({"status": "failed", "push_status": "failed", "stderr": "could not resolve current branch"})
                return finish(payload)
            push_result = self._git(["push", remote, f"HEAD:{target_branch}"])
            payload["push_status"] = "pushed" if push_result["returncode"] == 0 else "failed"
            payload["push_remote"] = remote
            payload["push_branch"] = target_branch
            payload["push_stdout"] = push_result["stdout"]
            payload["push_stderr"] = push_result["stderr"]
            if push_result["returncode"] != 0:
                payload["status"] = "failed"
        return finish(payload)

    def _is_git_repo(self) -> bool:
        result = self._git(["rev-parse", "--is-inside-work-tree"])
        return result["returncode"] == 0 and result["stdout"].strip() == "true"

    def _current_branch(self) -> str | None:
        result = self._git(["branch", "--show-current"])
        branch = result["stdout"].strip()
        return branch or None

    def _git(self, args: list[str]) -> dict[str, Any]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.project_root,
            text=True,
            capture_output=True,
        )
        return {
            "returncode": completed.returncode,
            "stdout": redact(completed.stdout[-8000:]),
            "stderr": redact(completed.stderr[-8000:]),
        }

    def _display_command(self, command: AcceptanceCommand, task: HarnessTask) -> str:
        executor = self.executor_registry.get(command.executor)
        if executor is None:
            return command.command or command.prompt or command.executor
        return executor.display_command(self._executor_invocation(command, task))

    def _executor_invocation(
        self,
        command: AcceptanceCommand,
        task: HarnessTask,
        *,
        phase: str | None = None,
        progress_callback: Any = None,
        context_pack: dict[str, Any] | None = None,
    ) -> ExecutorInvocation:
        invocation = ExecutorInvocation(
            project_root=self.project_root,
            task_id=task.id,
            name=command.name,
            command=command.command,
            prompt=command.prompt,
            timeout_seconds=command.timeout_seconds,
            model=command.model,
            sandbox=command.sandbox,
            phase=phase,
            no_progress_timeout_seconds=self._executor_no_progress_timeout_seconds(phase, command),
            context_pack=context_pack,
            progress_callback=progress_callback,
        )
        executor = self.executor_registry.get(command.executor)
        if executor is None:
            return invocation
        prepare_invocation = getattr(executor, "prepare_invocation", None)
        if prepare_invocation is None:
            return invocation
        return prepare_invocation(
            invocation,
            self._executor_task_context(task, command=command, phase=phase, context_pack=context_pack),
        )

    def _executor_progress_callback(
        self,
        state: dict[str, Any],
        *,
        task: HarnessTask,
        phase: str,
        command: AcceptanceCommand,
        executor_id: str,
        persist: bool,
    ) -> Any:
        last_saved_at = 0.0

        def callback(event: dict[str, Any]) -> None:
            nonlocal last_saved_at
            if not persist:
                return
            event_name = str(event.get("event") or event.get("status") or "progress")
            now = time.monotonic()
            if event_name == "output" and now - last_saved_at < 1.0:
                return
            last_saved_at = now
            watchdog = {
                key: deepcopy(value)
                for key, value in event.items()
                if key
                in {
                    "schema_version",
                    "event",
                    "status",
                    "reason",
                    "message",
                    "phase",
                    "executor_id",
                    "command_name",
                    "pid",
                    "started_at",
                    "finished_at",
                    "runtime_seconds",
                    "timeout_seconds",
                    "no_progress_timeout_seconds",
                    "threshold_seconds",
                    "last_progress_at",
                    "last_output_at",
                    "stdout_bytes",
                    "stderr_bytes",
                    "stream",
                    "executor_event",
                    "termination",
                    "process_returncode",
                }
            }
            watchdog.setdefault("phase", phase)
            watchdog.setdefault("executor_id", executor_id)
            watchdog.setdefault("command_name", command.name)
            message = str(watchdog.get("message") or f"{phase} command {command.name} {event_name}")
            control = self._heartbeat_drive_control_in_state(
                state,
                activity=f"{phase}:executor",
                message=message,
                task=task,
                phase=phase,
                executor_watchdog=watchdog,
            )
            if control is not None:
                executor_event = watchdog.get("executor_event")
                if isinstance(executor_event, dict):
                    compact_event = deepcopy(executor_event)
                    control["latest_executor_event"] = compact_event
                    control["executor_event_count"] = int(control.get("executor_event_count", 0) or 0) + 1
                    history = control.setdefault("executor_event_history", [])
                    if not isinstance(history, list):
                        history = []
                        control["executor_event_history"] = history
                    history.append(compact_event)
                    del history[:-10]
                self.save_state(state)

        return callback

    def _executor_task_context(
        self,
        task: HarnessTask,
        *,
        command: AcceptanceCommand | None = None,
        phase: str | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> ExecutorTaskContext:
        def task_command(command: AcceptanceCommand) -> ExecutorTaskCommand:
            return ExecutorTaskCommand(
                name=command.name,
                command=command.command,
                prompt=command.prompt,
                executor=command.executor,
                spec_refs=command.spec_refs,
            )

        current_command = task_command(command) if command is not None else None
        relevant_spec_refs = tuple(context_pack.get("spec_refs", [])) if isinstance(context_pack, dict) else task.spec_refs
        requirement_excerpts = (
            tuple(context_pack.get("requirements", []))
            if isinstance(context_pack, dict) and isinstance(context_pack.get("requirements"), list)
            else ()
        )
        return ExecutorTaskContext(
            project_root=self.project_root,
            task_id=task.id,
            title=task.title,
            milestone_id=task.milestone_id,
            milestone_title=task.milestone_title,
            spec_refs=task.spec_refs,
            file_scope=task.file_scope,
            acceptance=tuple(task_command(item) for item in task.acceptance),
            e2e=tuple(task_command(item) for item in task.e2e),
            phase=phase,
            current_command=current_command,
            relevant_spec_refs=relevant_spec_refs,
            requirement_excerpts=requirement_excerpts,
            context_pack=context_pack,
        )

    def _run_command(
        self,
        acceptance: AcceptanceCommand,
        *,
        phase: str,
        task: HarnessTask,
        state: dict[str, Any] | None = None,
        persist_state: bool = False,
    ) -> CommandRun:
        executor = self.executor_registry.get(acceptance.executor)
        if executor is None:
            return CommandRun(
                phase,
                acceptance.name,
                self._display_command(acceptance, task),
                "failed",
                None,
                utc_now(),
                utc_now(),
                "",
                f"unknown executor: {acceptance.executor}",
                executor=acceptance.executor,
                executor_metadata=self.executor_registry.metadata_for(acceptance.executor),
            )
        state_payload = state if state is not None else {}
        progress_callback = self._executor_progress_callback(
            state_payload,
            task=task,
            phase=phase,
            command=acceptance,
            executor_id=executor.metadata.id,
            persist=persist_state,
        )
        context_pack = None
        if executor.metadata.kind == "agent" and executor.metadata.input_mode == "prompt":
            context_pack = self._write_agent_context_pack(
                task,
                acceptance,
                phase=phase,
                executor_metadata=executor.metadata.as_contract(),
            )
        invocation = self._executor_invocation(
            acceptance,
            task,
            phase=phase,
            progress_callback=progress_callback,
            context_pack=context_pack,
        )
        display_command = executor.display_command(invocation)
        result = executor.execute(invocation)
        return CommandRun(
            phase,
            acceptance.name,
            display_command,
            result.status,
            result.returncode,
            result.started_at,
            result.finished_at,
            result.stdout,
            result.stderr,
            executor=executor.metadata.id,
            executor_metadata=executor.metadata.as_contract(),
            result_metadata=result.metadata,
            context_pack=context_pack or {},
        )

    def _finish_task(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        report_path: Path,
        started_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
        persist: bool,
        *,
        safety: dict[str, Any] | None = None,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
        replay_guard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        finished_at = utc_now()
        manifest_path = report_path.with_suffix(".json")
        safety_payload = safety or {}
        git_context = self._git_context(safety_payload)
        policy_input = self._policy_input(
            task,
            safety=safety_payload,
            git_context=git_context,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )
        policy_decisions = self._policy_decisions(
            task,
            safety=safety_payload,
            git_context=git_context,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
            approval_state=state if persist else None,
            mutate_stale=persist,
        )
        policy_decision_summary = self._policy_decision_summary(policy_decisions)
        safety_audit = self._safety_audit_evidence(policy_decisions)
        report_relative = str(report_path.relative_to(self.project_root))
        manifest_relative = str(manifest_path.relative_to(self.project_root))
        attempt = int(state.get("tasks", {}).get(task.id, {}).get("attempts", 0))
        failure_isolation = self._failure_isolation_block(
            task,
            status=status,
            message=message,
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
            report_relative=report_relative,
            manifest_relative=manifest_relative,
            runs=runs,
            safety=safety_payload,
            policy_decision_summary=policy_decision_summary,
        )
        queued_approvals: list[dict[str, Any]] = []
        if persist:
            queued_approvals = self._queue_required_approvals(state, task, policy_decisions)
            if status in COMPLETED_STATUSES:
                self._consume_task_approvals(state, task, status=status)
        approval_blocked = status == "blocked" and bool(queued_approvals)
        approval_queue_summary = self._approval_queue_summary_from_state(state, status_filter=None)
        self._record_phase_state(
            state,
            task,
            phase="manifest-writing",
            event="before",
            status="running",
            persist=persist,
            metadata={
                "report_path": report_relative,
                "manifest_path": manifest_relative,
                "run_count": len(runs),
                "result_status": status,
            },
        )
        self._write_report(
            report_path,
            task,
            started_at,
            finished_at,
            runs,
            status,
            message,
            safety=safety_payload,
            policy_decisions=policy_decisions,
            policy_decision_summary=policy_decision_summary,
            safety_audit=safety_audit,
            failure_isolation=failure_isolation,
            approval_queue=approval_queue_summary,
            replay_guard=replay_guard,
        )
        self._write_task_manifest(
            manifest_path,
            report_path,
            task,
            started_at,
            finished_at,
            runs,
            status,
            message,
            persist,
            attempt,
            safety=safety,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
            git_context=git_context,
            policy_input=policy_input.as_contract(),
            policy_decisions=policy_decisions,
            policy_decision_summary=policy_decision_summary,
            safety_audit=safety_audit,
            failure_isolation=failure_isolation,
            approval_queue=approval_queue_summary,
            replay_guard=replay_guard,
        )
        self._record_phase_state(
            state,
            task,
            phase="manifest-writing",
            event="after",
            status="passed",
            message="task report and manifest were written",
            persist=persist,
            metadata={
                "report_path": report_relative,
                "manifest_path": manifest_relative,
                "report_exists": report_path.exists(),
                "manifest_exists": manifest_path.exists(),
            },
        )
        self.rebuild_manifest_index()
        if persist:
            self._record_phase_state(
                state,
                task,
                phase="final-result",
                event="before",
                status="running",
                persist=True,
                metadata={
                    "status": status,
                    "message": message,
                    "report_path": report_relative,
                    "manifest_path": manifest_relative,
                },
            )
            task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
            task_state["status"] = status
            if approval_blocked:
                task_state["blocked_on_approval"] = True
            task_state["last_finished_at"] = finished_at
            task_state["last_report"] = report_relative
            task_state["last_manifest"] = manifest_relative
            if failure_isolation:
                task_state["failure_isolation"] = failure_isolation
            elif status in COMPLETED_STATUSES:
                task_state.pop("failure_isolation", None)
            self._record_phase_state(
                state,
                task,
                phase="final-result",
                event="after",
                status=status,
                message=message,
                persist=True,
                metadata={
                    "report_path": report_relative,
                    "manifest_path": manifest_relative,
                    "run_count": len(runs),
                },
            )
            if approval_blocked:
                state.setdefault("tasks", {}).setdefault(task.id, {})["attempts"] = max(
                    0,
                    int(state.get("tasks", {}).get(task.id, {}).get("attempts", 0)) - 1,
                )
            self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": finished_at,
                "event": "task_run",
                "task_id": task.id,
                "milestone_id": task.milestone_id,
                "status": status,
                "dry_run": not persist,
                "report": str(report_path.relative_to(self.project_root)),
                "manifest": str(manifest_path.relative_to(self.project_root)),
                "policy_decision_summary": policy_decision_summary,
                "safety_audit": safety_audit,
            },
        )
        return {
            "task": redact_evidence(self.task_payload(task)),
            "status": status,
            "message": message,
            "report": str(report_path.relative_to(self.project_root)),
            "manifest": str(manifest_path.relative_to(self.project_root)),
            "runs": [
                self._command_run_result_payload(task, run)
                for run in runs
            ],
            "safety": redact_evidence(safety or {}),
            "approval_queue": approval_queue_summary,
            **({"replay_guard": replay_guard} if replay_guard is not None else {}),
            **({"failure_isolation": failure_isolation} if failure_isolation else {}),
        }

    def _write_task_manifest(
        self,
        manifest_path: Path,
        report_path: Path,
        task: HarnessTask,
        started_at: str,
        finished_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
        persist: bool,
        attempt: int,
        *,
        safety: dict[str, Any] | None = None,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
        git_context: dict[str, Any] | None = None,
        policy_input: dict[str, Any] | None = None,
        policy_decisions: list[dict[str, Any]] | None = None,
        policy_decision_summary: dict[str, Any] | None = None,
        safety_audit: dict[str, Any] | None = None,
        failure_isolation: dict[str, Any] | None = None,
        approval_queue: dict[str, Any] | None = None,
        replay_guard: dict[str, Any] | None = None,
    ) -> None:
        manifest_relative = str(manifest_path.relative_to(self.project_root))
        report_relative = str(report_path.relative_to(self.project_root))
        safety_payload = safety or {}
        git_payload = git_context or self._git_context(safety_payload)
        policy_input_payload = policy_input or self._policy_input(
            task,
            safety=safety_payload,
            git_context=git_payload,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        ).as_contract()
        policy_decision_payload = policy_decisions or self._policy_decisions(
            task,
            safety=safety_payload,
            git_context=git_payload,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )
        policy_decision_summary_payload = policy_decision_summary or self._policy_decision_summary(
            policy_decision_payload
        )
        safety_audit_payload = safety_audit or self._safety_audit_evidence(policy_decision_payload)
        context_pack_artifacts = self._context_pack_artifacts_from_runs(runs)
        payload = {
            "schema_version": 1,
            "kind": "engineering-harness.task-run-manifest",
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "project_root": str(self.project_root),
            "profile": self.roadmap.get("profile"),
            "roadmap_path": str(self.roadmap_path.relative_to(self.project_root))
            if self.roadmap_path and self.roadmap_path.is_relative_to(self.project_root)
            else str(self.roadmap_path),
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "task": self.task_payload(task),
            "milestone": {
                "id": task.milestone_id,
                "title": task.milestone_title,
            },
            "status": status,
            "message": message,
            "started_at": started_at,
            "finished_at": finished_at,
            "dry_run": not persist,
            "attempt": attempt,
            "report_path": report_relative,
            "manifest_path": manifest_relative,
            "artifacts": [
                {"kind": "markdown_report", "path": report_relative},
                {"kind": "json_manifest", "path": manifest_relative},
                *context_pack_artifacts,
            ],
            "runs": [self._command_run_manifest(task, run) for run in runs],
            "safety": safety_payload,
            "policy_input": policy_input_payload,
            "policy_decisions": policy_decision_payload,
            "policy_decision_summary": policy_decision_summary_payload,
            "safety_audit": safety_audit_payload,
            "git": git_payload,
        }
        if approval_queue is not None:
            payload["approval_queue"] = approval_queue
        if replay_guard is not None:
            payload["replay_guard"] = replay_guard
        if failure_isolation is not None:
            payload["failure_isolation"] = failure_isolation
        write_json(manifest_path, redact_evidence(payload))

    def _context_pack_artifacts_from_runs(self, runs: list[CommandRun]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for run in runs:
            context_pack = run.context_pack
            if not isinstance(context_pack, dict):
                continue
            path = str(context_pack.get("path") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            artifact = {
                "kind": "agent_context_pack",
                "path": path,
            }
            if context_pack.get("sha256"):
                artifact["sha256"] = context_pack.get("sha256")
            artifacts.append(artifact)
        return artifacts

    def _command_run_manifest(self, task: HarnessTask, run: CommandRun) -> dict[str, Any]:
        metadata = self._configured_command_metadata(task, run)
        stdout_summary = self._stream_summary(run.stdout)
        stderr_summary = self._stream_summary(run.stderr)
        executor_metadata = (
            run.executor_metadata
            or metadata.get("executor_metadata")
            or self.executor_registry.metadata_for(metadata["executor"])
        )
        payload = {
            "phase": run.phase,
            "name": run.name,
            "executor": metadata["executor"],
            "command": redact(run.command),
            "status": run.status,
            "returncode": run.returncode,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "required": metadata.get("required"),
            "timeout_seconds": metadata.get("timeout_seconds"),
            "no_progress_timeout_seconds": self._command_run_no_progress_timeout_seconds(task, run, metadata),
            "model": metadata.get("model"),
            "sandbox": metadata.get("sandbox"),
            "requested_capabilities": metadata.get("requested_capabilities", []),
            "spec_refs": metadata.get("spec_refs", []),
            "safety_classification": self._command_run_safety_classification(task, run, metadata),
            "user_experience_gate": deepcopy(metadata.get("user_experience_gate", {})),
            "executor_capabilities": executor_metadata.get("capabilities", []) if isinstance(executor_metadata, dict) else [],
            "stdout": stdout_summary,
            "stderr": stderr_summary,
            "executor_metadata": executor_metadata,
            "executor_result": self._executor_result_contract(
                run,
                stdout_summary=stdout_summary,
                stderr_summary=stderr_summary,
            ),
        }
        context_pack = run.context_pack
        if isinstance(context_pack, dict) and context_pack.get("path"):
            payload["context_pack"] = context_pack
        return payload

    def _command_run_result_payload(self, task: HarnessTask, run: CommandRun) -> dict[str, Any]:
        manifest_payload = self._command_run_manifest(task, run)
        return {
            "phase": manifest_payload["phase"],
            "name": manifest_payload["name"],
            "command": manifest_payload["command"],
            "status": manifest_payload["status"],
            "returncode": manifest_payload["returncode"],
            "executor": manifest_payload["executor"],
            "requested_capabilities": manifest_payload.get("requested_capabilities", []),
            "spec_refs": manifest_payload.get("spec_refs", []),
            "safety_classification": manifest_payload.get("safety_classification", {}),
            "user_experience_gate": manifest_payload.get("user_experience_gate", {}),
            "executor_capabilities": manifest_payload.get("executor_capabilities", []),
            "executor_metadata": manifest_payload.get("executor_metadata", {}),
            "executor_result": manifest_payload.get("executor_result", {}),
            **({"context_pack": manifest_payload.get("context_pack")} if manifest_payload.get("context_pack") else {}),
        }

    def _executor_result_contract(
        self,
        run: CommandRun,
        *,
        stdout_summary: dict[str, Any] | None = None,
        stderr_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": EXECUTOR_RESULT_CONTRACT_VERSION,
            "status": run.status,
            "returncode": run.returncode,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "stdout": stdout_summary or self._stream_summary(run.stdout),
            "stderr": stderr_summary or self._stream_summary(run.stderr),
            "metadata": run.result_metadata,
        }

    def _command_run_no_progress_timeout_seconds(
        self,
        task: HarnessTask,
        run: CommandRun,
        metadata: dict[str, Any],
    ) -> int | None:
        watchdog = run.result_metadata.get("watchdog") if isinstance(run.result_metadata, dict) else None
        if isinstance(watchdog, dict):
            value = watchdog.get("no_progress_timeout_seconds")
            if value is not None:
                return self._coerce_optional_nonnegative_seconds(value)
        configured = self._coerce_optional_nonnegative_seconds(metadata.get("no_progress_timeout_seconds"))
        if configured is not None and configured > 0:
            return configured
        for command in (*task.implementation, *task.repair, *task.acceptance, *task.e2e):
            if command.name == run.name and self._display_command(command, task) == run.command:
                return self._executor_no_progress_timeout_seconds(run.phase, command)
        return self._executor_no_progress_timeout_seconds(run.phase)

    def _command_run_safety_classification(
        self,
        task: HarnessTask,
        run: CommandRun,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        for command in (*task.implementation, *task.repair, *task.acceptance, *task.e2e):
            if command.name == run.name and self._display_command(command, task) == run.command:
                return self._command_safety_classification(command)
        return {
            "schema_version": COMMAND_SAFETY_CLASSIFICATION_SCHEMA_VERSION,
            "deny_by_default": True,
            "unsafe": False,
            "unsafe_classes": [],
            "detected_capabilities": [],
            "requested_capabilities": metadata.get("requested_capabilities", []),
            "requested_capability_classifications": classify_capabilities(metadata.get("requested_capabilities", [])),
            "matches": {},
            "sandbox": {
                "mode": metadata.get("sandbox"),
                "allowed_modes": sorted(SAFE_SANDBOX_MODES),
                "unsafe_modes": sorted(UNSAFE_SANDBOX_MODES),
                "classification": "unknown",
                "unsafe": False,
                "reason": "command definition was not available",
            },
        }

    def _configured_command_metadata(self, task: HarnessTask, run: CommandRun) -> dict[str, Any]:
        for command in (*task.implementation, *task.repair, *task.acceptance, *task.e2e):
            if command.name == run.name and self._display_command(command, task) == run.command:
                return {
                    "executor": command.executor,
                    "required": command.required,
                    "timeout_seconds": command.timeout_seconds,
                    "no_progress_timeout_seconds": command.no_progress_timeout_seconds,
                    "model": command.model,
                    "sandbox": command.sandbox,
                    "requested_capabilities": list(command.requested_capabilities),
                    "user_experience_gate": deepcopy(command.user_experience_gate),
                    "spec_refs": list(command.spec_refs),
                    "executor_metadata": self.executor_registry.metadata_for(command.executor),
                }
        return {
            "executor": run.executor or ("codex" if run.command.startswith("codex exec ") else "shell"),
            "required": None,
            "timeout_seconds": None,
            "no_progress_timeout_seconds": None,
            "model": None,
            "sandbox": None,
            "requested_capabilities": [],
            "user_experience_gate": {},
            "spec_refs": [],
            "executor_metadata": run.executor_metadata
            or self.executor_registry.metadata_for(
                run.executor or ("codex" if run.command.startswith("codex exec ") else "shell")
            ),
        }

    def _stream_summary(self, text: str) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        return {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest() if text else None,
        }

    def _policy_decisions(
        self,
        task: HarnessTask,
        *,
        safety: dict[str, Any],
        git_context: dict[str, Any] | None = None,
        allow_live: bool,
        allow_manual: bool,
        allow_agent: bool,
        approval_state: dict[str, Any] | None = None,
        mutate_stale: bool = True,
    ) -> list[dict[str, Any]]:
        task_allow_manual = allow_manual or self._approval_is_approved(
            task,
            decision_kind="manual_approval",
            phase="task",
            state=approval_state,
            mutate_stale=mutate_stale,
        )
        task_allow_agent = allow_agent or self._approval_is_approved(
            task,
            decision_kind="agent_approval",
            phase="task",
            state=approval_state,
            mutate_stale=mutate_stale,
        )
        base_input = self._policy_input(
            task,
            safety=safety,
            git_context=git_context,
            allow_live=allow_live,
            allow_manual=task_allow_manual,
            allow_agent=task_allow_agent,
        )
        decisions: list[PolicyDecision] = [
            self._manual_approval_decision(base_input),
            self._agent_approval_decision(base_input),
        ]
        for phase, commands in (
            ("implementation", task.implementation),
            ("repair", task.repair),
            ("acceptance", task.acceptance),
            ("e2e", task.e2e),
        ):
            for command in commands:
                command_allow_live = allow_live or self._approval_is_approved(
                    task,
                    decision_kind="live_approval",
                    phase=phase,
                    name=command.name,
                    executor=command.executor,
                    command=command,
                    state=approval_state,
                    mutate_stale=mutate_stale,
                )
                command_allow_agent = task_allow_agent or self._approval_is_approved(
                    task,
                    decision_kind="executor_approval",
                    phase=phase,
                    name=command.name,
                    executor=command.executor,
                    command=command,
                    state=approval_state,
                    mutate_stale=mutate_stale,
                )
                executor_metadata = self.executor_registry.metadata_for(command.executor)
                command_input = self._policy_input(
                    task,
                    phase=phase,
                    command=command,
                    safety=safety,
                    git_context=git_context,
                    allow_live=command_allow_live,
                    allow_manual=task_allow_manual,
                    allow_agent=command_allow_agent,
                    executor_metadata=executor_metadata,
                )
                executor_decision = self._executor_policy_decision(command_input)
                decisions.append(executor_decision)
                if executor_decision.blocks_execution():
                    continue
                capability_decision = self._capability_policy_decision(command_input)
                if capability_decision is not None:
                    decisions.append(capability_decision)
                    if capability_decision.blocks_execution():
                        continue
                decisions.append(self._executor_approval_decision(command_input))
                executor = self.executor_registry.get(command.executor)
                if executor is not None and executor.metadata.uses_command_policy:
                    decisions.append(self._command_policy_decision(command_input))
                    decisions.append(self._live_approval_decision(command_input))

        decisions.extend(
            [
                self._git_preflight_decision(base_input),
                self._file_scope_decision(base_input),
            ]
        )
        return [decision.as_contract() for decision in decisions]

    def _policy_decision_summary(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        by_effect: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        blocking: list[dict[str, Any]] = []
        requires_approval: list[dict[str, Any]] = []
        for decision in decisions:
            kind = str(decision.get("kind") or "unknown")
            outcome = str(decision.get("outcome") or "unknown")
            effect = str(decision.get("effect") or "unknown")
            severity = str(decision.get("severity") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            by_effect[effect] = by_effect.get(effect, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1
            if effect in {"deny", "requires_approval"}:
                blocking.append(self._compact_policy_decision(decision))
            if decision.get("requires_approval"):
                requires_approval.append(self._compact_policy_decision(decision))
        return {
            "total": len(decisions),
            "by_kind": dict(sorted(by_kind.items())),
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_effect": dict(sorted(by_effect.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "blocking": blocking,
            "requires_approval": requires_approval,
        }

    def _compact_policy_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        policy_input = decision.get("input") if isinstance(decision.get("input"), dict) else {}
        command = policy_input.get("command") if isinstance(policy_input.get("command"), dict) else {}
        compact = {
            "kind": decision.get("kind"),
            "scope": decision.get("scope"),
            "outcome": decision.get("outcome"),
            "effect": decision.get("effect"),
            "severity": decision.get("severity"),
            "reason": decision.get("reason"),
            "phase": decision.get("phase") or policy_input.get("phase"),
            "name": decision.get("name") or command.get("name"),
            "executor": decision.get("executor"),
            "approval_flag": decision.get("approval_flag"),
            "status": decision.get("status"),
        }
        return {key: value for key, value in compact.items() if value is not None}

    def _safety_audit_evidence(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        unsafe_decisions: list[dict[str, Any]] = []
        unsafe_classes: set[str] = set()
        unsafe_capabilities: set[str] = set()
        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("kind") != "capability_policy":
                continue
            metadata = decision.get("metadata") if isinstance(decision.get("metadata"), dict) else {}
            operation = (
                metadata.get("operation_classification")
                if isinstance(metadata.get("operation_classification"), dict)
                else {}
            )
            classes = [str(item) for item in metadata.get("unsafe_classes", []) if str(item).strip()]
            executor_classes = [
                str(item)
                for item in metadata.get("executor_unsafe_classes", [])
                if str(item).strip()
            ]
            capabilities = [
                str(item)
                for item in metadata.get("unsafe_capabilities", [])
                if str(item).strip()
            ]
            executor_capabilities = [
                str(item)
                for item in metadata.get("executor_unsafe_capabilities", [])
                if str(item).strip()
            ]
            unsafe_classes.update([*classes, *executor_classes])
            unsafe_capabilities.update([*capabilities, *executor_capabilities])
            if decision.get("effect") in {"deny", "requires_approval"} or operation.get("unsafe") or executor_capabilities:
                unsafe_decisions.append(
                    {
                        **self._compact_policy_decision(decision),
                        "unsafe_classes": classes,
                        "unsafe_capabilities": capabilities,
                        "executor_unsafe_classes": executor_classes,
                        "executor_unsafe_capabilities": executor_capabilities,
                        "detected_capabilities": metadata.get("detected_capabilities", []),
                        "requested_capabilities": metadata.get("requested_capabilities", []),
                        "operation_classification": operation,
                    }
                )
        return redact_evidence(
            {
                "schema_version": SAFETY_AUDIT_SCHEMA_VERSION,
                "kind": "engineering-harness.safety-audit",
                "deny_by_default": True,
                "unsafe_decision_count": len(unsafe_decisions),
                "unsafe_classes": sorted(unsafe_classes),
                "unsafe_capabilities": sorted(unsafe_capabilities),
                "unsafe_decisions": unsafe_decisions,
            }
        )

    def _failure_isolation_block(
        self,
        task: HarnessTask,
        *,
        status: str,
        message: str,
        attempt: int,
        started_at: str,
        finished_at: str,
        report_relative: str,
        manifest_relative: str,
        runs: list[CommandRun],
        safety: dict[str, Any],
        policy_decision_summary: dict[str, Any],
    ) -> dict[str, Any] | None:
        if status not in ISOLATED_FAILURE_STATUSES:
            return None
        file_scope_guard = safety.get("file_scope_guard", {}) if isinstance(safety, dict) else {}
        file_scope_violations = [
            str(path)
            for path in file_scope_guard.get("violations", [])
            if str(path).strip()
        ]
        blocking = [
            deepcopy(decision)
            for decision in policy_decision_summary.get("blocking", [])
            if isinstance(decision, dict)
        ]
        phase = self._failure_isolation_phase(
            status=status,
            runs=runs,
            file_scope_violations=file_scope_violations,
            blocking_policy_decisions=blocking,
        )
        failure_kind = self._failure_isolation_kind(
            task=task,
            status=status,
            phase=phase,
            runs=runs,
            file_scope_violations=file_scope_violations,
            blocking_policy_decisions=blocking,
        )
        retry_exhaustion = self._failure_isolation_retry_exhaustion(
            task,
            status=status,
            phase=phase,
            attempt=attempt,
            runs=runs,
        )
        executor_watchdog = self._failure_isolation_executor_watchdog(runs, phase=phase)
        payload = {
            "schema_version": FAILURE_ISOLATION_SCHEMA_VERSION,
            "kind": "engineering-harness.task-failure-isolation",
            "status": status,
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "phase": phase,
            "failure_kind": failure_kind,
            "message": message,
            "started_at": started_at,
            "finished_at": finished_at,
            "attempt": attempt,
            "retry_exhausted": bool(retry_exhaustion["exhausted"]),
            "retry_exhaustion": retry_exhaustion,
            "report_paths": {
                "task_report": report_relative,
                "task_manifest": manifest_relative,
            },
            "relevant_report_paths": [report_relative, manifest_relative],
            "blocking_policy_decisions": blocking,
            "file_scope_violations": file_scope_violations,
            "local_next_action": self._failure_isolation_next_action(
                task,
                phase=phase,
                failure_kind=failure_kind,
                report_relative=report_relative,
                file_scope_violations=file_scope_violations,
                blocking_policy_decisions=blocking,
            ),
            "resolved": False,
        }
        if executor_watchdog is not None:
            payload["executor_watchdog"] = executor_watchdog
        return payload

    def _failure_isolation_phase(
        self,
        *,
        status: str,
        runs: list[CommandRun],
        file_scope_violations: list[str],
        blocking_policy_decisions: list[dict[str, Any]],
    ) -> str:
        if file_scope_violations:
            return "file-scope-guard"
        for run in reversed(runs):
            if run.status in EXECUTOR_WATCHDOG_FAILURE_STATUSES:
                return run.phase
            if run.status == "blocked":
                return run.phase
            if run.returncode not in (None, 0):
                return run.phase
        if blocking_policy_decisions:
            return str(blocking_policy_decisions[0].get("phase") or "task")
        return "task" if status == "blocked" else "unknown"

    def _failure_isolation_kind(
        self,
        *,
        task: HarnessTask,
        status: str,
        phase: str,
        runs: list[CommandRun],
        file_scope_violations: list[str],
        blocking_policy_decisions: list[dict[str, Any]],
    ) -> str:
        if file_scope_violations or phase == "file-scope-guard":
            return "file_scope_violation"
        if status == "blocked" and blocking_policy_decisions:
            return "policy_block"
        if status == "blocked":
            return "task_blocked"
        for run in reversed(runs):
            if run.phase == phase and run.status == "no_progress":
                return "executor_no_progress"
            if run.phase == phase and run.status == "timeout":
                return "executor_timeout"
            if run.phase == phase and (
                self._run_has_user_experience_marker(run)
                or self._run_is_user_experience_gate(task, run)
            ):
                return "user_experience_gate_failure"
        phase_root = phase.split("-", 1)[0]
        if phase_root in {"implementation", "acceptance", "repair", "e2e"}:
            return f"{phase_root}_failure"
        return "task_failure"

    def _run_is_user_experience_gate(self, task: HarnessTask, run: CommandRun) -> bool:
        metadata = self._configured_command_metadata(task, run)
        gate = metadata.get("user_experience_gate") if isinstance(metadata, dict) else {}
        if isinstance(gate, dict) and str(gate.get("kind") or "").strip() == BROWSER_USER_EXPERIENCE_GATE_KIND:
            return True
        return "engineering_harness.browser_e2e" in str(run.command or "")

    def _failure_isolation_executor_watchdog(
        self,
        runs: list[CommandRun],
        *,
        phase: str,
    ) -> dict[str, Any] | None:
        for run in reversed(runs):
            if run.phase != phase or run.status not in EXECUTOR_WATCHDOG_FAILURE_STATUSES:
                continue
            watchdog = run.result_metadata.get("watchdog") if isinstance(run.result_metadata, dict) else {}
            if not isinstance(watchdog, dict):
                watchdog = {}
            return {
                "schema_version": EXECUTOR_WATCHDOG_CONTRACT_VERSION,
                "status": run.status,
                "phase": run.phase,
                "executor": run.executor,
                "executor_metadata": run.executor_metadata,
                "command_name": run.name,
                "pid": watchdog.get("pid"),
                "started_at": watchdog.get("started_at") or run.started_at,
                "finished_at": watchdog.get("finished_at") or run.finished_at,
                "last_progress_at": watchdog.get("last_progress_at"),
                "last_output_at": watchdog.get("last_output_at"),
                "timeout_seconds": watchdog.get("timeout_seconds"),
                "no_progress_timeout_seconds": watchdog.get("no_progress_timeout_seconds"),
                "threshold_seconds": watchdog.get("threshold_seconds"),
                "reason": watchdog.get("reason") or run.status,
                "message": watchdog.get("message") or run.stderr,
                "termination": deepcopy(watchdog.get("termination")) if isinstance(watchdog.get("termination"), dict) else {},
            }
        return None

    def _failure_isolation_retry_exhaustion(
        self,
        task: HarnessTask,
        *,
        status: str,
        phase: str,
        attempt: int,
        runs: list[CommandRun],
    ) -> dict[str, Any]:
        acceptance_attempts = sum(1 for run in runs if run.phase.startswith("acceptance-"))
        repair_attempts = sum(1 for run in runs if run.phase.startswith("repair-"))
        task_attempt_exhausted = status == "failed" and attempt >= task.max_attempts
        repair_iteration_exhausted = (
            status == "failed"
            and phase.startswith("acceptance-")
            and acceptance_attempts >= task.max_task_iterations
        )
        return {
            "exhausted": bool(task_attempt_exhausted or repair_iteration_exhausted),
            "task_attempt_exhausted": bool(task_attempt_exhausted),
            "repair_iteration_exhausted": bool(repair_iteration_exhausted),
            "attempt": attempt,
            "max_attempts": task.max_attempts,
            "acceptance_attempts": acceptance_attempts,
            "repair_attempts": repair_attempts,
            "max_task_iterations": task.max_task_iterations,
        }

    def _failure_isolation_next_action(
        self,
        task: HarnessTask,
        *,
        phase: str,
        failure_kind: str,
        report_relative: str,
        file_scope_violations: list[str],
        blocking_policy_decisions: list[dict[str, Any]],
    ) -> str:
        if failure_kind == "file_scope_violation" or file_scope_violations:
            return (
                f"Inspect the file-scope violations for task {task.id}, keep changes within file_scope, "
                "then rerun the task."
            )
        if failure_kind == "policy_block" or blocking_policy_decisions:
            return (
                f"Review the blocking policy decisions for task {task.id}, approve the required local gate "
                "or adjust the task command, then rerun the task."
            )
        if failure_kind == "executor_no_progress":
            return (
                f"Inspect the executor watchdog evidence for phase {phase} in {report_relative}, "
                "fix the silent or hung local command, then rerun the task."
            )
        if failure_kind == "executor_timeout":
            return (
                f"Inspect the executor timeout evidence for phase {phase} in {report_relative}, "
                "shorten, repair, or raise the local timeout for the command, then rerun the task."
            )
        if failure_kind == "user_experience_gate_failure":
            return (
                f"Inspect the browser user-experience gate evidence for phase {phase} in {report_relative}, "
                "repair the declared routes/forms/roles or frontend behavior locally, then rerun the task."
            )
        return (
            f"Inspect phase {phase} in {report_relative}, apply a local fix within the task file_scope, "
            "then rerun the task."
        )

    def _compact_failure_isolation(self, failure_isolation: dict[str, Any]) -> dict[str, Any]:
        retry_exhaustion = (
            failure_isolation.get("retry_exhaustion")
            if isinstance(failure_isolation.get("retry_exhaustion"), dict)
            else {}
        )
        report_paths = (
            failure_isolation.get("report_paths")
            if isinstance(failure_isolation.get("report_paths"), dict)
            else {}
        )
        blocking = failure_isolation.get("blocking_policy_decisions")
        violations = failure_isolation.get("file_scope_violations")
        executor_watchdog = (
            failure_isolation.get("executor_watchdog")
            if isinstance(failure_isolation.get("executor_watchdog"), dict)
            else None
        )
        compact = {
            "schema_version": failure_isolation.get("schema_version", FAILURE_ISOLATION_SCHEMA_VERSION),
            "task_id": failure_isolation.get("task_id"),
            "milestone_id": failure_isolation.get("milestone_id"),
            "status": failure_isolation.get("status"),
            "phase": failure_isolation.get("phase"),
            "failure_kind": failure_isolation.get("failure_kind"),
            "retry_exhausted": bool(failure_isolation.get("retry_exhausted", False)),
            "attempt": failure_isolation.get("attempt"),
            "max_attempts": retry_exhaustion.get("max_attempts"),
            "finished_at": failure_isolation.get("finished_at"),
            "report_path": report_paths.get("task_report"),
            "manifest_path": report_paths.get("task_manifest"),
            "blocking_policy_decision_count": len(blocking) if isinstance(blocking, list) else 0,
            "file_scope_violation_count": len(violations) if isinstance(violations, list) else 0,
            "local_next_action": failure_isolation.get("local_next_action"),
        }
        if executor_watchdog is not None:
            compact["executor_watchdog"] = {
                "schema_version": executor_watchdog.get("schema_version", EXECUTOR_WATCHDOG_CONTRACT_VERSION),
                "status": executor_watchdog.get("status"),
                "phase": executor_watchdog.get("phase"),
                "executor": executor_watchdog.get("executor"),
                "command_name": executor_watchdog.get("command_name"),
                "pid": executor_watchdog.get("pid"),
                "threshold_seconds": executor_watchdog.get("threshold_seconds"),
                "timeout_seconds": executor_watchdog.get("timeout_seconds"),
                "no_progress_timeout_seconds": executor_watchdog.get("no_progress_timeout_seconds"),
                "last_progress_at": executor_watchdog.get("last_progress_at"),
                "last_output_at": executor_watchdog.get("last_output_at"),
                "reason": executor_watchdog.get("reason"),
            }
        return {key: value for key, value in compact.items() if value is not None}

    def _git_context(self, safety: dict[str, Any]) -> dict[str, Any]:
        git_preflight = safety.get("git_preflight", {})
        file_scope_guard = safety.get("file_scope_guard", {})
        context: dict[str, Any] = {
            "is_repository": False,
            "root": None,
            "branch": None,
            "head": None,
            "short_head": None,
            "dirty_before_paths": git_preflight.get("dirty_before_paths", []),
            "dirty_after_paths": file_scope_guard.get("dirty_after_paths", []),
            "dirty_before_out_of_scope_paths": git_preflight.get("dirty_before_out_of_scope_paths", []),
            "file_scope_violations": file_scope_guard.get("violations", []),
            "status_short": "",
        }
        if not self._is_git_repo():
            return context

        root = self._git(["rev-parse", "--show-toplevel"])
        head = self._git(["rev-parse", "HEAD"])
        short_head = self._git(["rev-parse", "--short", "HEAD"])
        status = self._git(["status", "--porcelain"])
        context.update(
            {
                "is_repository": True,
                "root": root["stdout"].strip() if root["returncode"] == 0 else None,
                "branch": self._current_branch(),
                "head": head["stdout"].strip() if head["returncode"] == 0 else None,
                "short_head": short_head["stdout"].strip() if short_head["returncode"] == 0 else None,
                "status_short": status["stdout"] if status["returncode"] == 0 else "",
            }
        )
        context["refs"] = {
            "head": context["head"],
            "short_head": context["short_head"],
            "branch": context["branch"],
        }
        return context

    def _write_report(
        self,
        report_path: Path,
        task: HarnessTask,
        started_at: str,
        finished_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
        *,
        safety: dict[str, Any] | None = None,
        policy_decisions: list[dict[str, Any]] | None = None,
        policy_decision_summary: dict[str, Any] | None = None,
        safety_audit: dict[str, Any] | None = None,
        failure_isolation: dict[str, Any] | None = None,
        approval_queue: dict[str, Any] | None = None,
        replay_guard: dict[str, Any] | None = None,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Task Report: {task.id}",
            "",
            f"- Status: `{status}`",
            f"- Project: `{self.roadmap.get('project', self.project_root.name)}`",
            f"- Milestone: `{task.milestone_id}` {task.milestone_title}",
            f"- Task: {redact(task.title)}",
            f"- Started: {started_at}",
            f"- Finished: {finished_at}",
            f"- Message: {redact(message)}",
            "",
        ]
        phase_commands = {
            "implementation": task.implementation,
            "repair": task.repair,
            "acceptance": task.acceptance,
            "e2e": task.e2e,
        }
        command_spec_refs = [
            (phase, command)
            for phase, commands in phase_commands.items()
            for command in commands
            if command.spec_refs
        ]
        if task.spec_refs or command_spec_refs:
            lines.extend(
                [
                    "## Spec Traceability",
                    "",
                    f"- Task spec refs: `{json.dumps(list(task.spec_refs))}`",
                ]
            )
            for phase, command in command_spec_refs:
                lines.append(
                    f"- {phase} `{command.name}` spec refs: `{json.dumps(list(command.spec_refs))}`"
                )
            lines.append("")
        lines.extend(
            [
                "## Task Runs",
                "",
            ]
        )
        if not runs:
            lines.append("No task commands were executed.")
        for run in runs:
            run_metadata = self._configured_command_metadata(task, run)
            executor_metadata = (
                run.executor_metadata
                or run_metadata.get("executor_metadata")
                or self.executor_registry.metadata_for(run_metadata["executor"])
            )
            requested_capabilities = run_metadata.get("requested_capabilities", [])
            user_experience_gate = run_metadata.get("user_experience_gate", {})
            executor_capabilities = executor_metadata.get("capabilities", []) if isinstance(executor_metadata, dict) else []
            context_pack = run.context_pack
            lines.extend(
                [
                    f"### {run.phase}: {run.name}",
                    "",
                    f"- Status: `{run.status}`",
                    f"- Return code: `{run.returncode}`",
                    f"- Executor: `{run_metadata.get('executor')}`",
                    f"- Requested capabilities: `{json.dumps(requested_capabilities)}`",
                    f"- User-experience gate: `{str(bool(user_experience_gate)).lower()}`",
                    f"- Executor capabilities: `{json.dumps(executor_capabilities)}`",
                    f"- Context pack: `{context_pack.get('path')}`" if isinstance(context_pack, dict) and context_pack.get("path") else "- Context pack: `none`",
                    "",
                    "```bash",
                    redact(run.command),
                    "```",
                    "",
                ]
            )
            if run.stdout:
                lines.extend(["Stdout:", "", "```text", redact(run.stdout), "```", ""])
            if run.stderr:
                lines.extend(["Stderr:", "", "```text", redact(run.stderr), "```", ""])
        if safety:
            git_preflight = safety.get("git_preflight", {})
            file_scope_guard = safety.get("file_scope_guard", {})
            lines.extend(
                [
                    "## Safety",
                    "",
                    f"- Git preflight: `{git_preflight.get('status', 'unknown')}` - {git_preflight.get('message', '')}",
                    f"- Dirty before: `{len(git_preflight.get('dirty_before_paths', []))}`",
                    f"- File-scope guard: `{file_scope_guard.get('status', 'unknown')}` - {file_scope_guard.get('message', '')}",
                    f"- New dirty paths: `{len(file_scope_guard.get('new_dirty_paths', []))}`",
                    f"- Changed pre-existing dirty paths: `{len(file_scope_guard.get('changed_preexisting_dirty_paths', []))}`",
                    f"- File-scope violations: `{len(file_scope_guard.get('violations', []))}`",
                    "",
                ]
            )
            violations = file_scope_guard.get("violations", [])
            if violations:
                lines.extend(["Violations:", ""])
                lines.extend(f"- `{path}`" for path in violations[:40])
                lines.append("")
        if approval_queue is not None:
            lines.extend(
                [
                    "## Approval Leases",
                    "",
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
                    "```json",
                    json.dumps(redact_evidence(approval_queue), indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        if replay_guard is not None:
            reused_phases = replay_guard.get("reused_phases", [])
            lines.extend(
                [
                    "## Phase Replay Guard",
                    "",
                    f"- Status: `{replay_guard.get('status')}`",
                    f"- Reused phases: `{len(reused_phases) if isinstance(reused_phases, list) else 0}`",
                    "",
                    "```json",
                    json.dumps(redact_evidence(replay_guard), indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        if safety_audit is not None:
            lines.extend(
                [
                    "## Safety Audit",
                    "",
                    f"- Unsafe decisions: `{safety_audit.get('unsafe_decision_count', 0)}`",
                    f"- Unsafe classes: `{json.dumps(safety_audit.get('unsafe_classes', []), sort_keys=True)}`",
                    "",
                    "```json",
                    json.dumps(redact_evidence(safety_audit), indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        if failure_isolation is not None:
            lines.extend(
                [
                    "## Failure Isolation",
                    "",
                    f"- Task: `{failure_isolation.get('task_id')}`",
                    f"- Phase: `{failure_isolation.get('phase')}`",
                    f"- Failure kind: `{failure_isolation.get('failure_kind')}`",
                    f"- Retry exhausted: `{str(bool(failure_isolation.get('retry_exhausted'))).lower()}`",
                    f"- Local next action: {failure_isolation.get('local_next_action')}",
                    "",
                    "```json",
                    json.dumps(redact_evidence(failure_isolation), indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        if policy_decisions is not None:
            summary = policy_decision_summary or self._policy_decision_summary(policy_decisions)
            lines.extend(
                [
                    "## Policy Decisions",
                    "",
                    f"- Total decisions: `{summary.get('total', len(policy_decisions))}`",
                    f"- Outcomes: `{json.dumps(summary.get('by_outcome', {}), sort_keys=True)}`",
                    f"- Effects: `{json.dumps(summary.get('by_effect', {}), sort_keys=True)}`",
                    f"- Blocking decisions: `{len(summary.get('blocking', []))}`",
                    "",
                ]
            )
            blocking = summary.get("blocking", [])
            if blocking:
                lines.extend(["Blocking decisions:", ""])
                for decision in blocking[:40]:
                    lines.append(
                        "- "
                        f"`{decision.get('kind', 'unknown')}` "
                        f"`{decision.get('outcome', 'unknown')}`: {decision.get('reason', '')}"
                    )
                lines.append("")
            lines.extend(
                [
                    "```json",
                    json.dumps(
                        redact_evidence({
                            "policy_decision_summary": summary,
                            "policy_decisions": policy_decisions,
                        }),
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                    "",
                ]
            )
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

from __future__ import annotations

from copy import deepcopy
from typing import Any


BASE_BLOCKED_PATTERNS = [
    "rm -rf /",
    "git reset --hard",
    "git checkout --",
    "git clean -fd",
    "PRIVATE_KEY=",
    "MNEMONIC=",
    "OPENAI_API_KEY=",
    "ANTHROPIC_API_KEY=",
]


PROFILES: dict[str, dict[str, Any]] = {
    "evm-protocol": {
        "description": "Solidity protocol projects using Foundry, Hardhat, or npm scripts.",
        "allowed_prefixes": [
            "npm ",
            "npx ",
            "forge ",
            "cast call ",
            "cd contracts && npm ",
            "cd contracts && forge ",
            "python3 ",
            "bash ",
        ],
        "requires_live_flag_patterns": ["--broadcast", "cast send", "deploy:mainnet", "verify:mainnet"],
        "tasks": [
            {"id": "compile", "title": "Compile contracts", "command": "npm run compile"},
            {"id": "test", "title": "Run protocol tests", "command": "npm run test"},
        ],
    },
    "node-frontend": {
        "description": "Node/TypeScript frontend projects.",
        "allowed_prefixes": ["npm ", "npx ", "node ", "python3 "],
        "requires_live_flag_patterns": ["deploy", "vercel --prod", "netlify deploy --prod"],
        "tasks": [
            {"id": "lint", "title": "Run frontend lint", "command": "npm run lint"},
            {"id": "build", "title": "Build frontend", "command": "npm run build"},
        ],
    },
    "python-agent": {
        "description": "Python AI/agent/research runtime projects.",
        "allowed_prefixes": ["python3 ", "pytest ", "bash scripts/"],
        "requires_live_flag_patterns": ["--live", "--spend", "--publish"],
        "tasks": [
            {"id": "tests", "title": "Run Python test suite", "command": "python3 -m pytest -q"},
        ],
    },
    "agent-monorepo": {
        "description": "Mixed agent systems with runtime, UI, scripts, and tests.",
        "allowed_prefixes": ["python3 ", "pytest ", "npm ", "npx ", "node ", "bash ", "cd "],
        "requires_live_flag_patterns": ["--live", "--broadcast", "deploy", "cast send"],
        "tasks": [
            {"id": "python-tests", "title": "Run Python tests", "command": "python3 -m pytest -q"},
            {"id": "frontend-build", "title": "Build frontend if present", "command": "npm run build", "required": False},
        ],
    },
    "evm-security-research": {
        "description": "DeFi security research with replay tests and analysis scripts.",
        "allowed_prefixes": ["forge ", "python3 ", "bash ", "cd "],
        "requires_live_flag_patterns": ["cast send", "--broadcast", "--rpc-url mainnet"],
        "tasks": [
            {"id": "forge-tests", "title": "Run Foundry tests", "command": "forge test"},
        ],
    },
    "trading-research": {
        "description": "Trading, backtesting, MEV, and market research projects.",
        "allowed_prefixes": ["python3 ", "node ", "npm ", "bash "],
        "blocked_patterns": ["place_order", "create_order", "send_order", "trade_live", "withdraw", "PRIVATE_KEY="],
        "requires_live_flag_patterns": ["--live", "--real", "--trade", "auto_trade.py"],
        "tasks": [
            {"id": "smoke", "title": "Run smoke checks", "command": "python3 -m pytest -q", "required": False},
        ],
    },
    "lean-formalization": {
        "description": "Lean/math formalization projects.",
        "allowed_prefixes": ["lake ", "lean ", "python3 ", "bash "],
        "requires_live_flag_patterns": [],
        "tasks": [
            {"id": "lake-build", "title": "Build Lean project", "command": "lake build"},
        ],
    },
}


def list_profiles() -> list[dict[str, str]]:
    return [
        {"id": profile_id, "description": str(profile.get("description", ""))}
        for profile_id, profile in sorted(PROFILES.items())
    ]


def get_profile(profile_id: str) -> dict[str, Any]:
    if profile_id not in PROFILES:
        raise KeyError(f"Unknown profile: {profile_id}")
    profile = deepcopy(PROFILES[profile_id])
    profile["blocked_patterns"] = list(dict.fromkeys(BASE_BLOCKED_PATTERNS + profile.get("blocked_patterns", [])))
    return profile


def default_roadmap(project_name: str, profile_id: str) -> dict[str, Any]:
    profile = get_profile(profile_id)
    tasks = []
    for item in profile.get("tasks", []):
        tasks.append(
            {
                "id": item["id"],
                "title": item["title"],
                "status": "pending",
                "max_attempts": 2,
                "file_scope": ["**"],
                "acceptance": [
                    {
                        "name": item["title"],
                        "command": item["command"],
                        "required": bool(item.get("required", True)),
                        "timeout_seconds": int(item.get("timeout_seconds", 600)),
                    }
                ],
            }
        )
    return {
        "version": 1,
        "project": project_name,
        "profile": profile_id,
        "default_timeout_seconds": 1800,
        "state_path": ".engineering/state/harness-state.json",
        "decision_log_path": ".engineering/state/decision-log.jsonl",
        "report_dir": ".engineering/reports/tasks",
        "milestones": [
            {
                "id": "baseline",
                "title": "Project Baseline",
                "status": "active",
                "objective": "Verify the project can run its baseline checks under the shared Engineering Orchestrator.",
                "tasks": tasks,
            }
        ],
    }


def command_policy(profile_id: str) -> dict[str, Any]:
    profile = get_profile(profile_id)
    return {
        "version": 1,
        "profile": profile_id,
        "allowed_prefixes": profile.get("allowed_prefixes", []),
        "blocked_patterns": profile.get("blocked_patterns", []),
        "requires_live_flag_patterns": profile.get("requires_live_flag_patterns", []),
    }

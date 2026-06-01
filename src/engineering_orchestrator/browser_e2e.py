from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


BROWSER_USER_EXPERIENCE_SCHEMA_VERSION = 1
BROWSER_USER_EXPERIENCE_GATE_KIND = "engineering-harness.browser-user-experience"
BROWSER_USER_EXPERIENCE_FAILURE_MARKER = "USER_EXPERIENCE_GATE_FAILED"
BROWSER_EXPERIENCE_KINDS = frozenset(
    {
        "app-specific",
        "dashboard",
        "multi-role-app",
        "submission-review",
    }
)
BROWSER_E2E_EVIDENCE_DIR = "artifacts/browser-e2e"
JOURNEY_DECLARATION_CANDIDATES = (
    "tests/e2e/{slug}.journey.json",
    "tests/e2e/{slug}.browser.json",
    ".engineering/browser-e2e/{slug}.json",
    "docs/e2e/{slug}.json",
)
PLAYWRIGHT_SPEC_CANDIDATES = (
    "tests/e2e/{slug}.spec.ts",
    "tests/e2e/{slug}.spec.js",
    "tests/e2e/{slug}.test.ts",
    "tests/e2e/{slug}.test.js",
    "e2e/{slug}.spec.ts",
    "e2e/{slug}.spec.js",
)
PLAYWRIGHT_CONFIG_CANDIDATES = (
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "playwright.config.cjs",
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "journey"


def is_browser_experience_kind(kind: str | None) -> bool:
    return str(kind or "").strip() in BROWSER_EXPERIENCE_KINDS


def browser_user_experience_command(
    journey_id: str,
    *,
    project_root: str = ".",
    config_path: str | None = None,
) -> str:
    args = [
        "python3",
        "-m",
        "engineering_orchestrator.browser_e2e",
        "--project-root",
        project_root,
        "--journey-id",
        journey_id,
    ]
    if config_path:
        args.extend(["--config", config_path])
    return " ".join(shlex.quote(item) for item in args)


def journey_declaration_candidates(journey_id: str) -> list[str]:
    slug = slugify(journey_id)
    return [template.format(slug=slug) for template in JOURNEY_DECLARATION_CANDIDATES]


def browser_evidence_paths(journey_id: str) -> dict[str, str]:
    slug = slugify(journey_id)
    base = f"{BROWSER_E2E_EVIDENCE_DIR}/{slug}"
    return {
        "dom": f"{base}/dom-evidence.json",
        "dom_snapshot": f"{base}/dom-snapshot.txt",
        "screenshot": f"{base}/screenshot.png",
    }


def browser_user_experience_gate(
    project_root: Path,
    *,
    experience: dict[str, Any] | None = None,
    journey: dict[str, Any] | None = None,
    journey_id: str | None = None,
    persona: str | None = None,
    goal: str | None = None,
) -> dict[str, Any]:
    journey_payload = journey or {}
    resolved_journey_id = str(journey_id or journey_payload.get("id") or "primary-browser-journey")
    resolved_persona = str(persona or journey_payload.get("persona") or "user")
    resolved_goal = str(goal or journey_payload.get("goal") or "Complete the primary browser workflow.")
    evidence_paths = browser_evidence_paths(resolved_journey_id)
    playwright = detect_playwright_support(project_root, journey_id=resolved_journey_id)
    fallback_command = browser_user_experience_command(resolved_journey_id)
    return {
        "schema_version": BROWSER_USER_EXPERIENCE_SCHEMA_VERSION,
        "kind": BROWSER_USER_EXPERIENCE_GATE_KIND,
        "journey": {
            "id": resolved_journey_id,
            "persona": resolved_persona,
            "goal": resolved_goal,
        },
        "experience_kind": (experience or {}).get("kind"),
        "route_form_role_declarations": journey_declaration_candidates(resolved_journey_id),
        "evidence_paths": evidence_paths,
        "runner": {
            "selected": "playwright" if playwright["runnable"] else "static-html-smoke",
            "playwright": playwright,
            "fallback": {
                "kind": "static-html-smoke",
                "command": fallback_command,
                "requires_external_services": False,
                "evidence_paths": evidence_paths,
            },
        },
        "commands": {
            "fallback_static_html_smoke": fallback_command,
            "playwright_template": playwright.get("command_template"),
            "selected": playwright.get("command_template") if playwright["runnable"] else fallback_command,
        },
    }


def detect_playwright_support(project_root: Path, *, journey_id: str | None = None) -> dict[str, Any]:
    project_root = project_root.resolve()
    package_path = project_root / "package.json"
    package_payload: dict[str, Any] = {}
    if package_path.exists():
        try:
            package_payload = json.loads(package_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_payload = {}
    scripts = package_payload.get("scripts") if isinstance(package_payload.get("scripts"), dict) else {}
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        value = package_payload.get(key)
        if isinstance(value, dict):
            deps.update(value)
    declared = any(name in deps for name in ("@playwright/test", "playwright"))
    config_files = [candidate for candidate in PLAYWRIGHT_CONFIG_CANDIDATES if (project_root / candidate).exists()]
    executable_path = project_root / "node_modules" / ".bin" / "playwright"
    local_executable = executable_path.exists()
    slug = slugify(journey_id or "")
    spec_candidates = [
        template.format(slug=slug)
        for template in PLAYWRIGHT_SPEC_CANDIDATES
        if slug
    ]
    existing_specs = [candidate for candidate in spec_candidates if (project_root / candidate).exists()]
    script_name = "e2e" if "e2e" in scripts else "test:e2e" if "test:e2e" in scripts else None
    if script_name:
        command_template = f"npm run {script_name}"
        if journey_id:
            command_template = f"{command_template} -- --grep {shlex.quote(journey_id)}"
    elif existing_specs:
        command_template = f"node_modules/.bin/playwright test {shlex.quote(existing_specs[0])}"
    else:
        command_template = "node_modules/.bin/playwright test"
    runnable = bool(local_executable and existing_specs)
    if runnable:
        status = "runnable"
    elif declared or config_files or script_name:
        status = "template_available"
    else:
        status = "not_configured"
    return {
        "status": status,
        "declared_dependency": declared,
        "local_executable": local_executable,
        "config_files": config_files,
        "script": script_name,
        "spec_candidates": spec_candidates,
        "existing_specs": existing_specs,
        "runnable": runnable,
        "command_template": command_template,
        "requires_external_services": False,
    }


@dataclass
class StaticRouteModel:
    path: str
    title: str | None
    text: str
    text_sha256: str
    roles: list[str]
    forms: list[dict[str, Any]]


@dataclass
class StaticHtmlParser(HTMLParser):
    text_parts: list[str] = field(default_factory=list)
    roles: set[str] = field(default_factory=set)
    forms: list[dict[str, Any]] = field(default_factory=list)
    title_parts: list[str] = field(default_factory=list)
    _tag_stack: list[str] = field(default_factory=list)
    _form_stack: list[int] = field(default_factory=list)
    _button_stack: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        self._tag_stack.append(tag)
        role = attributes.get("role") or native_role(tag, attributes)
        if role:
            self.roles.add(role)
        if tag == "form":
            form = {
                "id": attributes.get("id"),
                "name": attributes.get("name"),
                "action": attributes.get("action"),
                "method": attributes.get("method", "get").lower(),
                "fields": [],
                "buttons": [],
            }
            self.forms.append(form)
            self._form_stack.append(len(self.forms) - 1)
        if tag in {"input", "select", "textarea"}:
            field_info = {
                "tag": tag,
                "type": attributes.get("type", "text").lower(),
                "name": attributes.get("name"),
                "id": attributes.get("id"),
                "label": attributes.get("aria-label")
                or attributes.get("placeholder")
                or attributes.get("name")
                or attributes.get("id")
                or attributes.get("value"),
                "required": "required" in attributes,
            }
            if tag == "input" and field_info["type"] in {"submit", "button", "reset"}:
                self._current_form_buttons().append(
                    {
                        "tag": tag,
                        "type": field_info["type"],
                        "text": attributes.get("value") or field_info["type"],
                        "id": attributes.get("id"),
                        "name": attributes.get("name"),
                    }
                )
            else:
                self._current_form_fields().append(field_info)
        if tag == "button":
            button = {
                "tag": tag,
                "type": attributes.get("type", "submit").lower(),
                "text": "",
                "id": attributes.get("id"),
                "name": attributes.get("name"),
            }
            self._current_form_buttons().append(button)
            self._button_stack.append(button)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form" and self._form_stack:
            self._form_stack.pop()
        if tag == "button" and self._button_stack:
            self._button_stack.pop()
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index] == tag:
                del self._tag_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        self.text_parts.append(text)
        if self._tag_stack and self._tag_stack[-1] == "title":
            self.title_parts.append(text)
        if self._button_stack:
            current = self._button_stack[-1]
            current["text"] = " ".join(str(current.get("text", "") + " " + text).split())

    def _current_form_fields(self) -> list[dict[str, Any]]:
        if not self._form_stack:
            return []
        return self.forms[self._form_stack[-1]]["fields"]

    def _current_form_buttons(self) -> list[dict[str, Any]]:
        if not self._form_stack:
            return []
        return self.forms[self._form_stack[-1]]["buttons"]


def native_role(tag: str, attrs: dict[str, str]) -> str | None:
    if tag in {"main", "nav", "form", "button"}:
        return tag
    if tag == "a" and attrs.get("href"):
        return "link"
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return "heading"
    if tag == "select":
        return "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        input_type = attrs.get("type", "text").lower()
        if input_type in {"button", "submit", "reset"}:
            return "button"
        if input_type == "checkbox":
            return "checkbox"
        if input_type == "radio":
            return "radio"
        if input_type in {"email", "password", "search", "tel", "text", "url", ""}:
            return "textbox"
    return None


def parse_static_route(path: Path, *, route_path: str) -> StaticRouteModel:
    parser = StaticHtmlParser()
    text = path.read_text(encoding="utf-8", errors="ignore")
    parser.feed(text)
    body_text = " ".join(parser.text_parts)
    return StaticRouteModel(
        path=route_path,
        title=" ".join(parser.title_parts) or None,
        text=body_text,
        text_sha256=hashlib.sha256(body_text.encode("utf-8")).hexdigest() if body_text else "",
        roles=sorted(parser.roles),
        forms=deepcopy(parser.forms),
    )


def route_file(project_root: Path, route: dict[str, Any]) -> Path:
    route_path = str(route.get("path") or route.get("route") or "/").strip() or "/"
    candidates: list[Path] = []
    if route_path == "/":
        candidates.append(project_root / "index.html")
    elif route_path.startswith("/"):
        stripped = route_path.strip("/")
        candidates.extend(
            [
                project_root / stripped,
                project_root / stripped / "index.html",
                project_root / f"{stripped}.html",
            ]
        )
    else:
        candidates.append(project_root / route_path)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return candidates[0]


def find_journey_declaration(project_root: Path, journey_id: str, explicit: str | None = None) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = [explicit] if explicit else journey_declaration_candidates(journey_id)
    for candidate in candidates:
        if not candidate:
            continue
        path = project_root / candidate
        if not path.exists():
            continue
        return path, json.loads(path.read_text(encoding="utf-8"))
    return None, None


def normalize_declaration(raw: dict[str, Any] | None, *, journey_id: str) -> dict[str, Any]:
    payload = deepcopy(raw or {})
    payload.setdefault("schema_version", BROWSER_USER_EXPERIENCE_SCHEMA_VERSION)
    payload.setdefault("kind", f"{BROWSER_USER_EXPERIENCE_GATE_KIND}.journey")
    payload["journey_id"] = str(payload.get("journey_id") or payload.get("id") or journey_id)
    payload.setdefault("persona", "user")
    routes = payload.get("routes")
    if not isinstance(routes, list):
        routes = []
    payload["routes"] = [route for route in routes if isinstance(route, dict)]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    default_evidence = browser_evidence_paths(payload["journey_id"])
    payload["evidence"] = {**default_evidence, **{key: str(value) for key, value in evidence.items() if value}}
    return payload


def expected_strings(route: dict[str, Any], *field_names: str) -> list[str]:
    values: list[str] = []
    for field_name in field_names:
        value = route.get(field_name)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
    return [item for item in dict.fromkeys(values) if item.strip()]


def form_matches(form: dict[str, Any], expectation: dict[str, Any]) -> bool:
    selector = str(expectation.get("selector") or "").strip()
    if selector.startswith("#") and form.get("id") != selector[1:]:
        return False
    expected_id = str(expectation.get("id") or "").strip()
    if expected_id and str(form.get("id") or "") != expected_id:
        return False
    expected_name = str(expectation.get("name") or "").strip()
    if expected_name and str(form.get("name") or "") != expected_name:
        return False
    return True


def evaluate_route(route: dict[str, Any], model: StaticRouteModel) -> list[str]:
    failures: list[str] = []
    lowered_text = model.text.lower()
    for text in expected_strings(route, "expect_text", "expected_text", "must_contain", "text"):
        if text.lower() not in lowered_text:
            failures.append(f"{model.path}: missing expected text `{text}`")
    expected_roles = expected_strings(route, "expect_roles", "roles")
    for role in expected_roles:
        if role not in model.roles:
            failures.append(f"{model.path}: missing expected role `{role}`")
    expected_forms = route.get("expect_forms", route.get("forms", []))
    if isinstance(expected_forms, dict):
        expected_forms = [expected_forms]
    if not isinstance(expected_forms, list):
        expected_forms = []
    for index, expectation in enumerate(item for item in expected_forms if isinstance(item, dict)):
        candidate_forms = [form for form in model.forms if form_matches(form, expectation)]
        if not candidate_forms:
            failures.append(f"{model.path}: missing expected form {index + 1}")
            continue
        fields = expected_strings(expectation, "fields", "field_names", "required_fields")
        buttons = expected_strings(expectation, "submit_text", "button_text", "submit")
        matched = False
        for form in candidate_forms:
            labels = {
                str(value).lower()
                for field in form.get("fields", [])
                for value in (field.get("name"), field.get("id"), field.get("label"))
                if value
            }
            button_text = " ".join(
                str(button.get("text") or "").lower()
                for button in form.get("buttons", [])
            )
            missing_fields = [field for field in fields if field.lower() not in labels]
            missing_buttons = [button for button in buttons if button.lower() not in button_text]
            if not missing_fields and not missing_buttons:
                matched = True
                break
        if not matched:
            details = []
            if fields:
                details.append("fields=" + ", ".join(fields))
            if buttons:
                details.append("submit=" + ", ".join(buttons))
            failures.append(f"{model.path}: form {index + 1} did not match expected {'; '.join(details)}")
    if not model.text.strip():
        failures.append(f"{model.path}: route has no visible text")
    return failures


def run_static_html_smoke(project_root: Path, declaration: dict[str, Any]) -> dict[str, Any]:
    journey_id = str(declaration.get("journey_id") or "journey")
    evidence_paths = declaration.get("evidence") if isinstance(declaration.get("evidence"), dict) else {}
    routes = declaration.get("routes") if isinstance(declaration.get("routes"), list) else []
    failures: list[str] = []
    route_results: list[dict[str, Any]] = []
    if not routes:
        failures.append(
            "missing browser journey route declarations; add routes/forms/roles to one of "
            + ", ".join(journey_declaration_candidates(journey_id))
        )
    for route in routes:
        if not isinstance(route, dict):
            continue
        route_path = str(route.get("path") or route.get("route") or "/")
        path = route_file(project_root, route)
        if not path.exists():
            failures.append(f"{route_path}: route file not found at {path.relative_to(project_root)}")
            continue
        model = parse_static_route(path, route_path=route_path)
        route_failures = evaluate_route(route, model)
        failures.extend(route_failures)
        route_results.append(
            {
                "path": model.path,
                "file": str(path.relative_to(project_root)),
                "title": model.title,
                "text_sha256": model.text_sha256,
                "roles": model.roles,
                "forms": model.forms,
                "status": "failed" if route_failures else "passed",
                "failures": route_failures,
            }
        )
    status = "failed" if failures else "passed"
    payload = {
        "schema_version": BROWSER_USER_EXPERIENCE_SCHEMA_VERSION,
        "kind": BROWSER_USER_EXPERIENCE_GATE_KIND,
        "runner": "static-html-smoke",
        "status": status,
        "journey_id": journey_id,
        "persona": declaration.get("persona"),
        "routes": route_results,
        "failures": failures,
        "evidence_paths": evidence_paths,
    }
    write_browser_evidence(project_root, payload, evidence_paths)
    return payload


def write_browser_evidence(project_root: Path, payload: dict[str, Any], evidence_paths: dict[str, Any]) -> None:
    dom_path = project_root / str(evidence_paths.get("dom") or browser_evidence_paths(str(payload.get("journey_id")))["dom"])
    dom_path.parent.mkdir(parents=True, exist_ok=True)
    dom_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    snapshot_path = evidence_paths.get("dom_snapshot")
    if snapshot_path:
        lines = [
            f"journey: {payload.get('journey_id')}",
            f"status: {payload.get('status')}",
            f"runner: {payload.get('runner')}",
        ]
        for route in payload.get("routes", []):
            if not isinstance(route, dict):
                continue
            lines.append(f"route: {route.get('path')} roles={','.join(route.get('roles', []))}")
            for form in route.get("forms", []):
                if isinstance(form, dict):
                    fields = ",".join(str(field.get("name") or field.get("id") or field.get("label")) for field in form.get("fields", []))
                    lines.append(f"form: id={form.get('id')} name={form.get('name')} fields={fields}")
        path = project_root / str(snapshot_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_playwright(project_root: Path, detection: dict[str, Any], *, timeout_seconds: int = 120) -> dict[str, Any]:
    specs = detection.get("existing_specs") if isinstance(detection.get("existing_specs"), list) else []
    executable = project_root / "node_modules" / ".bin" / "playwright"
    if not executable.exists() or not specs:
        return {"status": "skipped", "reason": "playwright_not_runnable", "detection": detection}
    command = [str(executable), "test", str(specs[0])]
    completed = subprocess.run(
        command,
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "command": " ".join(shlex.quote(item) for item in command),
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
        "detection": detection,
    }


def run_browser_user_experience_gate(
    project_root: Path,
    *,
    journey_id: str,
    config_path: str | None = None,
    allow_playwright: bool = True,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    config_source, raw = find_journey_declaration(project_root, journey_id, explicit=config_path)
    declaration = normalize_declaration(raw, journey_id=journey_id)
    declaration["config_path"] = str(config_source.relative_to(project_root)) if config_source else None
    detection = detect_playwright_support(project_root, journey_id=journey_id)
    if allow_playwright and detection["runnable"]:
        result = run_playwright(project_root, detection)
        if result["status"] in {"passed", "failed"}:
            payload = {
                "schema_version": BROWSER_USER_EXPERIENCE_SCHEMA_VERSION,
                "kind": BROWSER_USER_EXPERIENCE_GATE_KIND,
                "runner": "playwright",
                "status": result["status"],
                "journey_id": journey_id,
                "persona": declaration.get("persona"),
                "config_path": declaration.get("config_path"),
                "playwright": result,
                "failures": [] if result["status"] == "passed" else [result.get("stderr") or "Playwright journey failed"],
                "evidence_paths": declaration["evidence"],
            }
            write_browser_evidence(project_root, payload, declaration["evidence"])
            return payload
    payload = run_static_html_smoke(project_root, declaration)
    payload["config_path"] = declaration.get("config_path")
    payload["playwright"] = detection
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local browser-style user-experience E2E gate")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--journey-id", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-playwright", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = run_browser_user_experience_gate(
            args.project_root,
            journey_id=args.journey_id,
            config_path=args.config,
            allow_playwright=not args.no_playwright,
        )
    except Exception as exc:
        payload = {
            "schema_version": BROWSER_USER_EXPERIENCE_SCHEMA_VERSION,
            "kind": BROWSER_USER_EXPERIENCE_GATE_KIND,
            "runner": "browser-user-experience",
            "status": "failed",
            "journey_id": args.journey_id,
            "failures": [str(exc)],
        }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        evidence = payload.get("evidence_paths") if isinstance(payload.get("evidence_paths"), dict) else {}
        print(
            f"BROWSER_USER_EXPERIENCE_GATE_{str(payload.get('status', 'failed')).upper()} "
            f"journey={payload.get('journey_id')} runner={payload.get('runner')} "
            f"dom={evidence.get('dom')}"
        )
    if payload.get("status") != "passed":
        failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
        print(
            f"{BROWSER_USER_EXPERIENCE_FAILURE_MARKER}: "
            + "; ".join(str(item) for item in failures[:8]),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
